"""Explore scraper — discovers new athletes via explore pages, segment leaderboards, and roster spider."""
from __future__ import annotations

import re
import sqlite3
from collections import deque
from dataclasses import dataclass
from threading import Event


from ingestion.config import now_utc_iso
from ingestion.core.delays import wait_for_internet
from ingestion.core.explore_rate_limiter import AdaptiveRateLimiter
from ingestion.db.queries.explore import list_explore_segments, save_explore_segment
from ingestion.logging_config import get_logger
from ingestion.session import StravaSession

logger = get_logger(__name__)

_ATHLETE_ID_RE = re.compile(r"/athletes/(\d+)")
_SEGMENT_ID_RE = re.compile(r"/segments/(\d+)")

_EXPLORE_URLS = [
    "/explore/activities",
    "/explore/running",
    "/explore/cycling",
]

_SEGMENT_LEADERBOARD_LIMIT = 10   # max segment leaderboards per run
_SPIDER_SEED_LIMIT = 10           # how many tracked athletes to use as spider seeds
_SPIDER_MAX_REQUESTS = 30         # hard cap on total spider HTTP requests per run
_SPIDER_MAX_DEPTH = 2             # BFS depth from seed set


@dataclass
class ExploreResult:
    explore_page_ids: int = 0
    segment_ids_found: int = 0
    segment_leaderboard_ids: int = 0
    spider_ids: int = 0
    total_discovered: int = 0
    added: int = 0
    pages_fetched: int = 0
    errors: int = 0


def _extract_athlete_ids(html: str) -> set[int]:
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        ids: set[int] = set()
        for tag in soup.find_all("a", href=True):
            m = _ATHLETE_ID_RE.search(tag["href"])
            if m:
                ids.add(int(m.group(1)))
        return ids
    except ImportError:
        return {int(m) for m in _ATHLETE_ID_RE.findall(html)}


def _extract_segment_ids(html: str) -> set[int]:
    return {int(m) for m in _SEGMENT_ID_RE.findall(html)}


def _get_spider_seeds(conn: sqlite3.Connection, limit: int) -> list[int]:
    """Return IDs of recently-active tracked athletes to use as spider starting points."""
    rows = conn.execute(
        """
        SELECT DISTINCT a.athlete_id
        FROM athletes a
        JOIN activities act ON act.athlete_id = a.athlete_id
        WHERE a.is_tracked = 1
          AND a.is_following = 1
        ORDER BY act.start_date_utc DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [int(row["athlete_id"]) for row in rows]


def _is_known(conn: sqlite3.Connection, athlete_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM athletes WHERE athlete_id = ?", (athlete_id,)
    ).fetchone() is not None


def _insert_stub(conn: sqlite3.Connection, athlete_id: int, source: str, now: str) -> bool:
    """Insert an explore/spider stub. Returns True if it was genuinely new."""
    existing = conn.execute(
        "SELECT athlete_id FROM athletes WHERE athlete_id = ?", (athlete_id,)
    ).fetchone()
    if existing:
        return False
    conn.execute(
        """
        INSERT OR IGNORE INTO athletes
          (athlete_id, name, is_private, is_following, is_tracked,
           first_seen_source, first_seen_at, last_seen_at, backfill_status)
        VALUES (?, ?, 0, 0, 0, ?, ?, ?, 'pending')
        """,
        (athlete_id, f"athlete_{athlete_id}", source, now, now),
    )
    return True


def _fetch_page(
    session: StravaSession,
    path: str,
    rate_limiter: AdaptiveRateLimiter,
    shutdown_event: Event,
    result: ExploreResult,
) -> str | None:
    """Fetch a Strava page with rate limiting. Returns HTML or None on failure."""
    url = f"https://www.strava.com{path}"
    rate_limiter.wait(url, shutdown_event)
    if shutdown_event.is_set():
        return None
    try:
        _, html = session.get_text(path)
        rate_limiter.record_success(url)
        result.pages_fetched += 1
        return html
    except Exception as exc:
        logger.warning(f"[explore] {path} failed: {exc}")
        rate_limiter.record_failure(url)
        result.errors += 1
        return None


def run_explore_scraper(
    session: StravaSession,
    conn: sqlite3.Connection,
    shutdown_event: Event,
    *,
    spider: bool = True,
) -> ExploreResult:
    """Discover new athletes from explore pages, segment leaderboards, and roster spider.

    Args:
        spider: If True, also BFS-spider from the tracked athlete roster.
    Returns:
        ExploreResult with counts of discovered and added athletes.
    """
    rate_limiter = AdaptiveRateLimiter(base_delay=5.0)
    discovered: set[int] = set()
    now = now_utc_iso()
    result = ExploreResult()

    # ── Phase 1: static explore pages ────────────────────────────────────────
    logger.info("[explore] Phase 1: scraping explore pages...")
    print("[explore] Phase 1: scraping explore pages...", flush=True)
    for path in _EXPLORE_URLS:
        if shutdown_event.is_set():
            break
        if not wait_for_internet(shutdown_event):
            break
        html = _fetch_page(session, path, rate_limiter, shutdown_event, result)
        if html is None:
            continue
        ids = _extract_athlete_ids(html)
        seg_ids = _extract_segment_ids(html)
        for sid in seg_ids:
            save_explore_segment(conn, sid)
        result.segment_ids_found += len(seg_ids)
        discovered.update(ids)
        result.explore_page_ids += len(ids)
        print(f"[explore]   {path}: {len(ids)} athlete IDs, {len(seg_ids)} segment IDs", flush=True)

    # ── Phase 2: segment leaderboards (real segment IDs from DB) ─────────────
    segment_ids = list_explore_segments(conn, limit=_SEGMENT_LEADERBOARD_LIMIT)
    if segment_ids:
        logger.info(f"[explore] Phase 2: scraping {len(segment_ids)} segment leaderboards...")
        print(f"[explore] Phase 2: scraping {len(segment_ids)} segment leaderboards...", flush=True)
    for seg_id in segment_ids:
        if shutdown_event.is_set():
            break
        if not wait_for_internet(shutdown_event):
            break
        path = f"/segments/{seg_id}/leaderboard"
        html = _fetch_page(session, path, rate_limiter, shutdown_event, result)
        if html is None:
            continue
        ids = _extract_athlete_ids(html)
        discovered.update(ids)
        result.segment_leaderboard_ids += len(ids)
        print(f"[explore]   segment/{seg_id}: {len(ids)} athlete IDs", flush=True)
    else:
        if not segment_ids:
            print("[explore] Phase 2: no segment IDs in DB yet — will populate on future runs", flush=True)

    # ── Phase 3: BFS spider from roster ──────────────────────────────────────
    if spider and not shutdown_event.is_set():
        seeds = _get_spider_seeds(conn, _SPIDER_SEED_LIMIT)
        logger.info(f"[explore] Phase 3: spidering from {len(seeds)} roster seeds (depth={_SPIDER_MAX_DEPTH})...")
        print(f"[explore] Phase 3: spidering from {len(seeds)} roster seeds...", flush=True)

        visited: set[int] = set(seeds)
        queue: deque[tuple[int, int]] = deque((seed, 0) for seed in seeds)
        spider_requests = 0

        while queue and not shutdown_event.is_set() and spider_requests < _SPIDER_MAX_REQUESTS:
            if not wait_for_internet(shutdown_event):
                break
            athlete_id, depth = queue.popleft()
            path = f"/athletes/{athlete_id}"
            html = _fetch_page(session, path, rate_limiter, shutdown_event, result)
            spider_requests += 1
            if html is None:
                continue

            ids = _extract_athlete_ids(html)
            seg_ids = _extract_segment_ids(html)
            for sid in seg_ids:
                save_explore_segment(conn, sid)
            result.segment_ids_found += len(seg_ids)

            new_on_page = 0
            for aid in ids:
                if aid in visited:
                    continue
                visited.add(aid)
                discovered.add(aid)
                new_on_page += 1
                if depth + 1 < _SPIDER_MAX_DEPTH and spider_requests < _SPIDER_MAX_REQUESTS:
                    queue.append((aid, depth + 1))

            result.spider_ids += new_on_page
            print(f"[explore]   spider /athletes/{athlete_id} (depth {depth}): {new_on_page} new IDs", flush=True)

        print(f"[explore] Spider complete: {spider_requests} pages fetched", flush=True)

    # ── Phase 4: insert new stubs ─────────────────────────────────────────────
    result.total_discovered = len(discovered)
    for ath_id in discovered:
        if shutdown_event.is_set():
            break
        source = "spider" if spider else "explore"
        if _insert_stub(conn, ath_id, source, now):
            result.added += 1

    logger.info(
        f"[explore] Done: {result.pages_fetched} pages fetched, "
        f"{result.total_discovered} unique IDs found, {result.added} new stubs added."
    )
    print(
        f"[explore] Done: {result.pages_fetched} pages, "
        f"{result.total_discovered} IDs found, {result.added} new stubs added.",
        flush=True,
    )
    return result
