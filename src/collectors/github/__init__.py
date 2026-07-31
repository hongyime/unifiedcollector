"""GitHub collector — unified ingest of users, repos, commits, issues, releases,
contributors, follower graph, and profile photo change tracking.

This module subsumes ``githubtoolkit/`` (a prior standalone toolkit) and routes
all cross-cutting concerns through Wave 0 cores so that GitHub work shares
infrastructure with the other 11 collectors.

ABSORBED FROM ``githubtoolkit/``
--------------------------------
* ``main.py``                   — entry-point shim (replaced by unified scheduler).
* ``src/github_client.py``      — REST v3 wrapper with PAT rotation. Replaced by
                                   ``_api_get`` + ``_paginate`` here, layered over
                                   a single httpx.AsyncClient and the PAT pool.
* ``src/spider.py``             — social-graph BFS (followers/following). Two
                                   paths supported: legacy ``github_spider_queue``
                                   (back-compat) AND Wave 0 ``SpiderDiscover``
                                   via the embedded ``GithubEdgeFetcher``.
* ``src/contribution_spider.py``— co-contributor / fork edges. Mapped onto the
                                   spider queue: contributors of any collected
                                   repo are enqueued at priority+1 for follow-up
                                   user-level collection. Co-contributor pair
                                   edges land in ``github_spider_queue``.
* ``src/avatar_downloader.py``  — bulk sequential-id avatar fetcher. Disk-first
                                   dedup + lazy DB sync re-implemented as
                                   ``download_avatars_by_id_range`` using
                                   ``src.core.media_download`` for the actual
                                   I/O and ``src.core.dedupe_hash`` for hashing.
* ``src/profile_photo_tracker.py`` — pHash + URL change detection. Forwarded
                                   to the canonical ``src.core.profile_photo_tracker``.
* ``src/pat_manager.py``        — multi-PAT rotation. Reduced to a per-token
                                   index + ``account_quota`` accounting (5000/h
                                   per token authenticated) — the toolkit's
                                   ``.env`` mutator is dropped (we never write
                                   to ``.env`` from a collector).
* ``src/reconciler.py``         — re-download missing avatars / verify integrity.
                                   Re-implemented as ``reconcile_avatars``.
* ``src/config.py``             — env tunables. Folded into the env-var reads
                                   inside ``__init__``.

DROPPED (NOT PORTED)
--------------------
* ``src/database.py`` + migrations — toolkit had its own SQLite. We use the
  unified Postgres pool exclusively.
* ``src/web/``                    — toolkit's standalone Flask dashboard. The
                                   unified dashboard (src/dashboard) covers it.
* ``src/cli.py``                  — interactive menu CLI. Replaced by the
                                   unified scheduler.
* ``follow_user`` / ``unfollow_user`` / ``get_authenticated_user`` mutator
                                   endpoints. We are READ-ONLY ingest.

DEFERRED
--------
* Tor circuit rotation hook on GitHub rate-limit hit (NEWNYM). Plumbing for
  the proxied client is in place; firing ``tor_proxy.new_circuit()`` after a
  403 is a 1-line follow-up.

ENVIRONMENT VARS
----------------
GITHUB_TOKEN                 comma-separated PATs (rotated on rate-limit).
GITHUB_USE_TOR               '1' to route via the Tor SOCKS5 sidecar.
GITHUB_MAX_CONCURRENT        per-host API parallelism (default 5).
GITHUB_TARGET_CONCURRENCY    parallel target iteration (default 4).
GITHUB_SPIDER_DEPTH          BFS hop ceiling (default 4 — toolkit was 3).
GITHUB_SPIDER_BATCH_SIZE     queue rows drained per scheduler tick (default 20).
GITHUB_SPIDER_USER_DELAY     between-user politeness delay (default 2.0s).
GITHUB_SPIDER_CONCURRENCY    spider drain workers (default 4).
GITHUB_SPIDER_ENABLED        master-switch for queue draining (default true).
GITHUB_API_DELAY             between-API-call polite delay (default 0.1s).
GITHUB_DOWNLOAD_DELAY        between-asset-download delay (default 0.5s).
GITHUB_AVATAR_SIZE           ?s= query param value (default 460).
GITHUB_MAX_COMMITS_PER_REPO  cap commit pull; 0/none/unlimited means all.
GITHUB_MAX_ISSUES_PER_REPO   cap issue/PR pull; 0/none/unlimited means all.
GITHUB_MAX_CONTRIBUTORS_PER_REPO cap contributors; 0/none/unlimited means all.
GITHUB_MAX_BRANCHES_PER_REPO cap branch refs; 0/none/unlimited means all.
GITHUB_MAX_WEAK_FANOUT_PER_REPO cap forks/stargazers/watchers (default 500).
GITHUB_RATE_LIMIT_BUFFER     rotate PAT when remaining < this (default 10).
GITHUB_PROFILE_PHOTO_BLOB_MAX_SIZE_MB  pHash blob storage cap (default 5000).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse

import httpx

from src.core.account_quota import AccountQuotaTracker, QuotaConfig
from src.core.base_collector import BaseCollector
from src.core.rate_limit_events import record_rate_limit_event
from src.core.scrape_pacing import sleep_rate_limit
from src.collectors.github.parse import (
    get_pat_display as _parse_get_pat_display,
    validate_pat_format as _parse_validate_pat_format,
)
from src.core.dedupe_hash import sha256_bytes as _sha256_bytes
from src.core.file_naming import sanitize_name
from src.core.proximity import refresh_account_proximity_cache
from src.core.profile_photo_tracker import ProfilePhotoTracker
from src.core.spider_discover import Edge, EdgeType, SpiderDiscover
from src.core.vault import VAULT_ROOT, write_atomic_artifact
from src.core.user_change_tracker import (
    UserChangeTracker,
    GITHUB_TRACKED_FIELDS,
)
from src.core import tor_proxy

logger = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
PER_PAGE = 100
AVATAR_CDN_BASE = "https://avatars.githubusercontent.com/u"
MENTION_RE = re.compile(r"(?<![\w/-])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)")


def _parse_iso(ts: Any) -> Optional[datetime]:
    """Tolerant ISO-8601 parser; returns None on falsy / unparsable input."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except (ValueError, AttributeError):
        return None


# ----------------------------------------------------------------------------
# EdgeFetcher: lets ``SpiderDiscover`` walk the github follower/following graph
# without the collector having to care about queue plumbing.
# ----------------------------------------------------------------------------


class GithubEdgeFetcher:
    """Adapter exposing GitHub's follower/following graph to ``SpiderDiscover``.

    Held as an inner class because it needs the live collector for httpx +
    PAT rotation. ``supported_edge_types`` is fixed to ``(FOLLOWER, FOLLOWING)``
    — the contributor / star / fork graphs are walked through the legacy
    ``github_spider_queue`` to preserve back-compat with existing rows in
    production. New deployments may opt into widening this tuple.
    """

    supported_edge_types: tuple[EdgeType, ...] = (EdgeType.FOLLOWER, EdgeType.FOLLOWING)

    def __init__(self, collector: "GithubCollector") -> None:
        self._c = collector

    async def fetch_edges(
        self, node_id: str, edge_type: EdgeType
    ) -> AsyncIterator[Edge]:
        if edge_type not in self.supported_edge_types:
            raise NotImplementedError(f"unsupported edge type: {edge_type}")
        endpoint = "followers" if edge_type == EdgeType.FOLLOWER else "following"
        async with self._c._make_client() as client:
            users = await self._c._paginate(
                client, f"{API_BASE}/users/{node_id}/{endpoint}"
            )
        for u in users:
            login = (u or {}).get("login")
            if login:
                yield Edge(source=node_id, target=login, edge_type=edge_type)


# ----------------------------------------------------------------------------
# Main collector
# ----------------------------------------------------------------------------


class GithubCollector(BaseCollector):
    SOURCE_NAME = "github"

    # ---- construction --------------------------------------------------

    def __init__(self):
        super().__init__()
        self._pats: list[str] = self._load_pats()
        self._pat_idx: int = 0
        self._sem = asyncio.Semaphore(int(os.getenv("GITHUB_MAX_CONCURRENT", "5")))
        self._batch_sem = asyncio.Semaphore(10)
        self._spider_depth = int(os.getenv("GITHUB_SPIDER_DEPTH", "4"))
        self._spider_batch_size = int(os.getenv("GITHUB_SPIDER_BATCH_SIZE", "20"))
        self._spider_user_delay = float(os.getenv("GITHUB_SPIDER_USER_DELAY", "2.0"))
        self._api_delay = float(os.getenv("GITHUB_API_DELAY", "0.1"))
        self._download_delay = float(os.getenv("GITHUB_DOWNLOAD_DELAY", "0.5"))
        # FAMOUS-FILTER (Bryan): skip repos at/above this star count and (optionally)
        # users at/above it (by follower count). 0 disables. Overrides the seed.
        self._famous_star_cap = int(os.getenv("GITHUB_FAMOUS_STAR_CAP", "0") or "0")
        self._famous_filter_users = os.getenv("GITHUB_FAMOUS_FILTER_USERS", "false").lower() == "true"
        self._avatar_size = int(os.getenv("GITHUB_AVATAR_SIZE", "460"))
        self._rate_limit_buffer = int(os.getenv("GITHUB_RATE_LIMIT_BUFFER", "10"))
        self._photo_tracker = ProfilePhotoTracker(
            blob_max_size_mb=int(
                os.getenv("GITHUB_PROFILE_PHOTO_BLOB_MAX_SIZE_MB", "5000")
            )
        )
        self._blob_enabled = (
            os.getenv("GITHUB_PROFILE_PHOTO_BLOB_ENABLED", "false").lower() == "true"
        )
        # Tor opt-in. ``GITHUB_USE_TOR=1`` turns on routing for this collector
        # only (we additionally honour ``TOR_PROXY_ENABLED`` to be friendly with
        # the global tor_proxy switch).
        self._use_tor = (
            os.getenv("GITHUB_USE_TOR", "0") == "1" or tor_proxy.is_enabled()
        )
        # account_quota: register the hourly-5k cap once per process. consume()
        # on a missing config is a no-op so this stays safe even if the same
        # tracker is imported from another collector first.
        self._quota = AccountQuotaTracker()
        try:
            self._quota.register(
                "github", QuotaConfig(daily_limit=0, hourly_limit=5000)
            )
        except Exception:  # noqa: BLE001 — defensive, registration is local-only
            logger.debug("github: quota registration skipped", exc_info=True)
        # Live rate-limit headers (mirrors github_client.GitHubAPIClient).
        self.rate_limit_remaining: Optional[int] = None
        self.rate_limit_reset: Optional[int] = None
        self.rate_limit_limit: Optional[int] = None
        self.requests_made = 0
        self._spider_visited: set[str] = set()
        self._db_avatar_ids: set[int] = set()

    def _tick_progress(self, amount: int = 1) -> None:
        """Advance worker liveness for long API/metadata-only crawls."""
        self._progress_count += max(1, int(amount))

    def _owner_accounts(self) -> set[str]:
        raw = os.getenv("GITHUB_OWNER_ACCOUNTS", "").strip()
        if not raw:
            raw = os.getenv("GITHUB_SPIDER_SEED", "bryanseah234")
        return {p.strip().lstrip("@").lower() for p in raw.split(",") if p.strip()}

    @staticmethod
    def _env_limit(name: str, default: str = "0") -> int | None:
        raw = (os.getenv(name, default) or "").strip().lower()
        if raw in {"", "0", "none", "all", "unlimited", "-1"}:
            return None
        try:
            return max(0, int(raw))
        except ValueError:
            return None

    @staticmethod
    def _is_human_login(login: str | None, payload: dict | None = None) -> bool:
        if not login:
            return False
        value = login.strip()
        if not value:
            return False
        if value.endswith("[bot]") or value.lower().endswith("-bot"):
            return False
        if (payload or {}).get("type") == "Bot":
            return False
        return True

    @staticmethod
    def _extract_mentions(*texts: Any) -> set[str]:
        mentions: set[str] = set()
        for text in texts:
            if not isinstance(text, str) or "@" not in text:
                continue
            for match in MENTION_RE.finditer(text):
                login = match.group(1)
                if GithubCollector._is_human_login(login):
                    mentions.add(login)
        return mentions

    @staticmethod
    def _repo_owner(full_name: str | None) -> str | None:
        if not full_name or "/" not in full_name:
            return None
        return full_name.split("/", 1)[0]

    # ---- pool wiring ----------------------------------------------------

    def set_pool(self, pool):
        super().set_pool(pool)
        self._photo_tracker.set_pool(pool)
        # Re-bind the quota tracker to the live pool. The default constructor
        # operated in in-memory mode (pool=None); now we want DB persistence.
        self._quota._pool = pool

    # ---- PAT pool -------------------------------------------------------

    def _load_pats(self) -> list[str]:
        # GitHub PAT env var: ``GITHUB_TOKEN`` (unified) and ``GITHUB_PAT`` (toolkit)
        # are both accepted, comma-separated.
        raw = os.getenv("GITHUB_TOKEN", "") or os.getenv("GITHUB_PAT", "")
        return [t.strip() for t in raw.split(",") if t.strip()]

    def _current_pat(self) -> Optional[str]:
        return self._pats[self._pat_idx] if self._pats else None

    def _pat_account_name(self) -> str:
        """Stable identifier for the active PAT used as the account_quota key.

        We don't want the raw token in DB rows (security + log redaction), so
        we use ``token_<idx>`` — same scheme as the on-disk media bucket.
        """
        return f"token_{self._pat_idx + 1}"

    def _headers(self) -> dict[str, str]:
        return self._headers_for_pat(self._current_pat())

    def _headers_for_pat(self, pat: str | None) -> dict[str, str]:
        h = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": self.user_agents.get_for_domain("github.com"),
        }
        if pat:
            h["Authorization"] = f"token {pat}"
        return h

    def _select_pat_for_request(self) -> tuple[str | None, str]:
        """Pick the PAT for one request and return its safe account label.

        Previously the collector stayed on token_1 until it was nearly empty,
        so a 3-PAT setup only used one quota bucket most of the hour. Rotating
        per request spreads load while the captured account label keeps quota
        accounting and rate-limit events tied to the token that actually made
        the request.
        """
        if not self._pats:
            return None, "anonymous"
        idx = self._pat_idx
        pat = self._pats[idx]
        account = f"token_{idx + 1}"
        if len(self._pats) > 1:
            self._pat_idx = (idx + 1) % len(self._pats)
        return pat, account

    def _rotate_pat(self) -> bool:
        """Rotate to the next PAT. Returns True if a different one is selected."""
        if len(self._pats) > 1:
            self._pat_idx = (self._pat_idx + 1) % len(self._pats)
            self.rate_limit_remaining = None
            logger.info("Rotated to PAT index %d", self._pat_idx)
            return True
        return False

    @staticmethod
    def _rate_limit_scope(url: str) -> str:
        """Stable, low-cardinality-ish scope for rate-limit dashboards."""
        parsed = urlparse(url)
        path = parsed.path.strip("/") or "root"
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 4 and parts[0] == "repos":
            return "/".join(parts[:4])
        if len(parts) >= 3 and parts[0] == "users":
            return "/".join(parts[:3])
        return "/".join(parts[:2]) if len(parts) >= 2 else parts[0]

    async def _record_api_rate_limit(
        self,
        *,
        url: str,
        resp: httpx.Response,
        cooldown_seconds: int,
        reason: str,
        account: str | None = None,
    ) -> None:
        await record_rate_limit_event(
            self.pool,
            source="github",
            account=account or self._pat_account_name(),
            scope=self._rate_limit_scope(url),
            # GitHub API quota exhaustion is reported as HTTP 403. Store it as
            # a rate-limit event for dashboard grouping, with the real status in
            # metadata for audit/debug.
            status_code=429,
            cooldown_seconds=cooldown_seconds,
            reason=reason,
            metadata={
                "http_status_code": resp.status_code,
                "url": url,
                "rate_limit_remaining": self.rate_limit_remaining,
                "rate_limit_reset": self.rate_limit_reset,
                "rate_limit_limit": self.rate_limit_limit,
                "retry_after": resp.headers.get("Retry-After"),
            },
        )

    @staticmethod
    def get_pat_display(pat: str) -> str:
        """Mask a PAT for safe display: ``ghp_xxxx****...****yyyy``."""
        return _parse_get_pat_display(pat)

    @staticmethod
    def validate_pat_format(pat: str) -> bool:
        """Sanity-check a PAT looks like a real GitHub token."""
        return _parse_validate_pat_format(pat)

    # ---- media path ----------------------------------------------------

    @property
    def account_media_dir(self) -> Path:
        # Keep token-isolated media trees so repeated migrations between PAT
        # pools don't cross-contaminate downloads.
        path = self.media_dir / self._pat_account_name()
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ---- httpx client factory ------------------------------------------

    def _make_client(self) -> httpx.AsyncClient:
        """Return a context-managed httpx.AsyncClient.

        Routed through the Tor SOCKS5 proxy iff Tor is enabled for this
        consumer. Falls through to a plain client otherwise. Wrapping happens
        here (rather than in ``collect()``) so all entrypoints — discover,
        track_avatar_changes, etc. — share the same proxy decision.
        """
        if self._use_tor:
            try:
                proxied = tor_proxy.get_proxied_client("github", timeout=30.0)
                # Return the underlying httpx.AsyncClient — context-manager-able.
                return proxied.httpx_client
            except Exception:  # noqa: BLE001
                logger.warning("tor proxy unavailable; falling back to direct", exc_info=True)
        return httpx.AsyncClient(timeout=30, follow_redirects=True)

    # ---- low-level API -------------------------------------------------

    async def _api_get(
        self, client: httpx.AsyncClient, url: str
    ) -> httpx.Response | None:
        """One authenticated API GET with rate-limit accounting + PAT rotation.

        Returns None on 404 / rate-limited / exhausted transport retry. Raises
        on 5xx after retry. Mirrors the toolkit's ``_request`` but
        delegates persistence to ``account_quota`` for cross-collector
        observability.
        """
        async with self._sem:
            await asyncio.sleep(self._api_delay)
            try:
                transport_retries = int(os.getenv("GITHUB_API_TRANSPORT_RETRIES", "2"))
            except ValueError:
                transport_retries = 2
            transport_retries = max(0, min(transport_retries, 5))
            attempts = transport_retries + 1
            for attempt in range(1, attempts + 1):
                pat, pat_account = self._select_pat_for_request()
                try:
                    resp = await client.get(url, headers=self._headers_for_pat(pat))
                    break
                except httpx.TransportError as exc:
                    if attempt >= attempts:
                        logger.warning(
                            "GitHub API transport error on %s (%s); exhausted %d attempt(s), skipping endpoint",
                            url,
                            type(exc).__name__,
                            attempts,
                        )
                        self._tick_progress()
                        return None
                    delay = min(10.0, float(2 ** (attempt - 1)))
                    logger.warning(
                        "GitHub API transport error on %s (%s); retry %d/%d after %.1fs",
                        url,
                        type(exc).__name__,
                        attempt,
                        transport_retries,
                        delay,
                    )
                    await sleep_rate_limit(delay)
            self.requests_made += 1
            self._tick_progress()
            self._update_rate_limit(resp.headers)
            # Best-effort quota bookkeeping (no-op when no PAT registered).
            try:
                await self._quota.consume("github", pat_account, 1)
            except Exception:  # noqa: BLE001
                logger.debug("quota.consume swallowed", exc_info=True)

            retry_after = None
            try:
                retry_after_raw = resp.headers.get("Retry-After")
                retry_after = int(float(retry_after_raw)) if retry_after_raw else None
            except (TypeError, ValueError):
                retry_after = None
            body = str(getattr(resp, "text", "") or "").lower()
            is_rate_limited = (
                resp.status_code == 429
                or (
                    resp.status_code == 403
                    and (
                        (self.rate_limit_remaining is not None and self.rate_limit_remaining <= 0)
                        or "rate limit" in body
                        or "abuse detection" in body
                    )
                )
            )
            if is_rate_limited:
                reset_wait = max(0, (self.rate_limit_reset or 0) - int(time.time()))
                wait = max(retry_after or 0, reset_wait) + 5
                if wait <= 5:
                    wait = int(os.getenv("GITHUB_RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS", "300"))
                await self._record_api_rate_limit(
                    url=url,
                    resp=resp,
                    cooldown_seconds=int(wait),
                    reason="github_api_rate_limit",
                    account=pat_account,
                )
                if len(self._pats) > 1:
                    # _select_pat_for_request already advanced to the next
                    # token before this response was processed. Do not rotate a
                    # second time or a 2-token setup lands back on the limited
                    # PAT.
                    rotated = True
                else:
                    rotated = self._rotate_pat()
                if not rotated:
                    max_sleep = int(os.getenv("GITHUB_RATE_LIMIT_MAX_SLEEP_SECONDS", "300"))
                    sleep_for = min(wait, max(0, max_sleep))
                    logger.warning(
                        "GitHub rate limit hit for %s; sleeping %.0fs (cooldown %ds)",
                        self._pat_account_name(),
                        sleep_for,
                        int(wait),
                    )
                    await sleep_rate_limit(sleep_for)
                return None
            if (
                self.rate_limit_remaining is not None
                and self.rate_limit_remaining < self._rate_limit_buffer
                and self._pats
            ):
                self._rotate_pat()
            if resp.status_code in (404, 409, 410, 422, 451):
                if resp.status_code == 409:
                    logger.debug("GitHub endpoint returned 409 empty/conflict for %s; skipping", url)
                return None
            if resp.status_code == 401:
                logger.error("GitHub auth failed (401) for %s", url)
                return None
            resp.raise_for_status()
            return resp

    def _update_rate_limit(self, headers) -> None:
        try:
            if "X-RateLimit-Remaining" in headers:
                self.rate_limit_remaining = int(headers["X-RateLimit-Remaining"])
            if "X-RateLimit-Reset" in headers:
                self.rate_limit_reset = int(headers["X-RateLimit-Reset"])
            if "X-RateLimit-Limit" in headers:
                self.rate_limit_limit = int(headers["X-RateLimit-Limit"])
        except (TypeError, ValueError):
            pass

    def get_rate_limit_status(self) -> dict[str, Any]:
        """Snapshot of the live rate-limit state (toolkit-compat helper)."""
        return {
            "remaining": self.rate_limit_remaining,
            "limit": self.rate_limit_limit,
            "reset": self.rate_limit_reset,
            "reset_time": (
                datetime.fromtimestamp(self.rate_limit_reset).isoformat()
                if self.rate_limit_reset
                else None
            ),
            "requests_made": self.requests_made,
            "active_pat_idx": self._pat_idx,
            "pat_count": len(self._pats),
        }

    async def _paginate(
        self,
        client: httpx.AsyncClient,
        url: str,
        max_items: int | None = None,
    ) -> list[dict]:
        results: list[dict] = []
        page = 1
        while True:
            if self._stop.is_set():
                break
            if max_items is not None and len(results) >= max_items:
                break
            sep = "&" if "?" in url else "?"
            page_url = f"{url}{sep}per_page={PER_PAGE}&page={page}"
            resp = await self._api_get(client, page_url)
            if resp is None:
                break
            batch = resp.json()
            if not batch:
                break
            results.extend(batch)
            if len(batch) < PER_PAGE:
                break
            page += 1
        return results[:max_items] if max_items is not None else results

    # ===================================================================
    # Toolkit-compatible high-level API
    # ===================================================================

    async def get_user(self, username: str) -> Optional[dict]:
        """Fetch the public profile for ``username``. Returns None on 404."""
        async with self._make_client() as client:
            resp = await self._api_get(client, f"{API_BASE}/users/{username}")
            return resp.json() if resp is not None else None

    async def get_user_by_id(self, user_id: int) -> Optional[dict]:
        """Fetch a user by numeric GitHub id. Mirrors toolkit's get_user_by_id."""
        async with self._make_client() as client:
            resp = await self._api_get(client, f"{API_BASE}/user/{user_id}")
            return resp.json() if resp is not None else None

    async def get_followers(self, username: str) -> list[dict]:
        async with self._make_client() as client:
            return await self._paginate(client, f"{API_BASE}/users/{username}/followers")

    async def get_following(self, username: str) -> list[dict]:
        async with self._make_client() as client:
            return await self._paginate(client, f"{API_BASE}/users/{username}/following")

    async def get_user_repos(self, username: str) -> list[dict]:
        async with self._make_client() as client:
            return await self._paginate(
                client,
                f"{API_BASE}/users/{username}/repos?sort=pushed&type=owner",
            )

    async def get_repo_contributors(self, full_name: str, max_items: int = 100) -> list[dict]:
        async with self._make_client() as client:
            return await self._paginate(
                client, f"{API_BASE}/repos/{full_name}/contributors", max_items=max_items
            )

    # ---- High-level entry points required by the task spec --------------

    async def discover_users(self, seed: Optional[str] = None) -> int:
        """Seed (or reseed) the spider queue and drain one batch.

        Uses the legacy ``github_spider_queue`` for back-compat with existing
        production rows. Returns the number of users newly enqueued at hop 1.
        """
        seed = seed or os.getenv("GITHUB_SPIDER_SEED", "bryanseah234")
        added = 0
        try:
            async with self._make_client() as client:
                added = await self._enqueue_neighbors(client, seed, depth=1)
            await self._process_spider_queue()
        except Exception as e:
            logger.error("discover_users failed: %s", e, exc_info=True)
        return added

    async def collect_user_metadata(self, username: str) -> Optional[dict]:
        """Fetch + persist user profile + repos. Returns the raw user JSON."""
        async with self._make_client() as client:
            resp = await self._api_get(client, f"{API_BASE}/users/{username}")
            if resp is None:
                return None
            user = resp.json()
            await self._upsert_user(user)
            repos = await self._paginate(client, f"{API_BASE}/users/{username}/repos")
            for repo in repos:
                if self._stop.is_set():
                    break
                await self._upsert_repo(repo)
            return user

    async def track_avatar_changes(self, username: str) -> tuple[bool, Optional[Path]]:
        """Download the avatar and detect a change against the URL+pHash baseline.

        Returns ``(changed, path)``. ``changed=True`` means a genuinely new
        photo was saved; ``False`` covers both "no change" and "CDN rotation
        only" (same image, different signed URL).
        """
        user = await self.get_user(username)
        if not user or not user.get("avatar_url"):
            return False, None
        avatar_url = user["avatar_url"]
        if self._avatar_size:
            sep = "&" if "?" in avatar_url else "?"
            avatar_url = f"{avatar_url}{sep}s={self._avatar_size}"
        uid = str(user["id"])
        dest_dir = self.account_media_dir / "profiles"
        dest_dir.mkdir(parents=True, exist_ok=True)
        changed, path = await self._photo_tracker.check_and_download(
            avatar_url, uid, "github", dest_dir
        )
        if changed and path is not None:
            try:
                metadata = {"raw": user}
                artifact_meta = self._photo_tracker.last_artifact_metadata()
                if artifact_meta:
                    metadata["vault_artifact"] = artifact_meta
                await self.insert_media_item(
                    entity_id=uid,
                    entity_name=user.get("login", username),
                    content_type="profile_photo",
                    content_id=f"avatar_{uid}",
                    filename=path.name,
                    file_path=str(path),
                    file_size=path.stat().st_size,
                    sha256=_sha256_bytes(path.read_bytes()),
                    metadata=metadata,
                    source_url=self._build_github_source_url(user.get("login", username)),
                )
            except Exception as e:
                logger.warning("insert_media_item failed for %s: %s", uid, e)
        return changed, path

    async def collect_contributions(
        self, username: str, year: Optional[int] = None
    ) -> dict[str, int]:
        """Fetch ``username``'s repos + co-contributors, persist edges to the
        legacy spider queue.

        ``year`` is currently advisory — the GitHub REST API does not expose a
        per-year contribution graph (that's GraphQL only). When set, it's
        attached as ``priority`` so older years drain after newer.

        Returns counts: ``{"repos": N, "contributors_enqueued": M}``.
        """
        repos_n = 0
        contrib_n = 0
        priority = (year - 2000) if year and year > 2000 else 2
        async with self._make_client() as client:
            repos = await self._paginate(
                client, f"{API_BASE}/users/{username}/repos?type=owner&sort=pushed"
            )
            for repo in repos:
                if self._stop.is_set():
                    break
                await self._upsert_repo(repo)
                repos_n += 1
                full_name = repo.get("full_name")
                if not full_name:
                    continue
                # forked? enqueue parent owner (toolkit "forked" edge)
                if repo.get("fork") and (parent := repo.get("parent")):
                    p_owner = ((parent.get("owner") or {}).get("login")) if parent else None
                    if p_owner and p_owner != username:
                        if await self._enqueue_user(p_owner, priority):
                            contrib_n += 1
                # co-contributor edges
                try:
                    contributors = await self._paginate(
                        client,
                        f"{API_BASE}/repos/{full_name}/contributors",
                        max_items=self._env_limit("GITHUB_MAX_CONTRIBUTORS_PER_REPO"),
                    )
                    for c in contributors:
                        login = (c or {}).get("login")
                        if login and login != username and (c or {}).get("type") != "Bot":
                            await self._upsert_edge(
                                source_login=username,
                                target_login=login,
                                repo_full_name=full_name,
                                edge_type="repo_contributor",
                                strength=75,
                                evidence_url=f"https://github.com/{full_name}/graphs/contributors",
                                evidence_id=f"{full_name}:contributor:{login}",
                                raw_payload=c,
                                queue_priority=priority,
                            )
                            if await self._enqueue_user(login, priority, source="contributors"):
                                contrib_n += 1
                except Exception as e:
                    logger.debug("contributor fetch failed for %s: %s", full_name, e)
        return {"repos": repos_n, "contributors_enqueued": contrib_n}

    async def _enqueue_user(self, login: str, priority: int, source: str = "discovered") -> bool:
        """Insert a single user into the legacy spider queue.

        Returns True if the row was newly inserted (asyncpg's INSERT command
        tag ends in '1' on insert, '0' on conflict).
        """
        login = (login or "").strip().lstrip("@").lower()
        if not self._is_human_login(login):
            return False
        try:
            async with self.pool.acquire() as conn:
                result = await conn.execute(
                    "INSERT INTO github_spider_queue "
                    "(target_type, target_identifier, source, status, priority, collected_at) "
                    "VALUES ('user', $1, $2, 'pending', $3, NOW()) "
                    "ON CONFLICT (target_type, target_identifier) DO NOTHING",
                    login, source, priority,
                )
                return bool(result and result.endswith(" 1"))
        except Exception as e:
            logger.debug("enqueue_user failed for %s: %s", login, e)
            return False

    async def _upsert_edge(
        self,
        *,
        source_login: str | None,
        target_login: str | None,
        repo_full_name: str | None,
        edge_type: str,
        strength: int,
        evidence_url: str | None,
        evidence_id: str | None,
        raw_payload: dict | None = None,
        queue_priority: int = 2,
    ) -> bool:
        source = (source_login or "").strip().lstrip("@").lower()
        target = (target_login or "").strip().lstrip("@").lower()
        repo_key = (repo_full_name or "").strip()
        if not self._is_human_login(source) or not self._is_human_login(target):
            return False
        if source.lower() == target.lower():
            return False
        eid = evidence_id or f"{edge_type}:{source}:{target}:{repo_full_name or ''}"
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO github_edges (
                        source_login, target_login, repo_full_name, edge_type,
                        strength, evidence_url, evidence_id, raw_payload,
                        first_seen, last_seen, collected_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, NOW(), NOW(), NOW())
                    ON CONFLICT (source_login, target_login, repo_full_name, edge_type, evidence_id)
                    DO UPDATE SET
                        strength = GREATEST(github_edges.strength, EXCLUDED.strength),
                        evidence_url = COALESCE(EXCLUDED.evidence_url, github_edges.evidence_url),
                        raw_payload = COALESCE(EXCLUDED.raw_payload, github_edges.raw_payload),
                        last_seen = NOW(),
                        collected_at = NOW()
                    """,
                    source,
                    target,
                    repo_key,
                    edge_type,
                    max(0, min(100, int(strength))),
                    evidence_url,
                    eid,
                    json.dumps(raw_payload or {}, default=str),
                )
            await self._enqueue_user(target, queue_priority, source=edge_type)
            return True
        except Exception as e:
            logger.debug("github edge upsert failed %s -> %s (%s): %s", source, target, edge_type, e)
            return False

    async def _record_mentions(
        self,
        *,
        source_login: str | None,
        repo_full_name: str | None,
        edge_type: str,
        evidence_url: str | None,
        evidence_id: str,
        raw_payload: dict | None,
        texts: tuple[Any, ...],
        strength: int = 55,
    ) -> int:
        count = 0
        for mention in self._extract_mentions(*texts):
            if await self._upsert_edge(
                source_login=source_login,
                target_login=mention,
                repo_full_name=repo_full_name,
                edge_type=edge_type,
                strength=strength,
                evidence_url=evidence_url,
                evidence_id=f"{evidence_id}:mention:{mention.lower()}",
                raw_payload=raw_payload,
                queue_priority=2,
            ):
                count += 1
        return count

    # ===================================================================
    # Bulk avatar download by sequential id (toolkit avatar_downloader.py)
    # ===================================================================

    async def download_avatars_by_id_range(
        self, start_id: int, end_id: int, *, concurrency: int = 10, delay: float = 0.5,
    ) -> dict[str, int]:
        """Sequentially fetch ``avatars.githubusercontent.com/u/<id>?s=<size>``
        for every id in ``[start_id, end_id]``.

        Disk-first dedup: if ``account_media_dir/avatars/<id>.jpg`` already
        exists we skip the network call. Counters returned mirror the toolkit
        shape so callers can drop in for ``AvatarDownloader.download_range``.
        """
        save_dir = self.account_media_dir / "avatars"
        save_dir.mkdir(parents=True, exist_ok=True)
        # Disk-first scan — cheap directory listing, dedup before any network
        # I/O.
        existing: set[int] = set()
        if save_dir.exists():
            for p in save_dir.iterdir():
                if p.stem.isdigit():
                    existing.add(int(p.stem))

        sem = asyncio.Semaphore(concurrency)
        counters = {"downloaded": 0, "skipped_on_disk": 0, "errors": 0}

        async def _one(uid: int):
            if uid in existing:
                counters["skipped_on_disk"] += 1
                return
            url = f"{AVATAR_CDN_BASE}/{uid}?s={self._avatar_size}"
            async with sem:
                try:
                    async with self._make_client() as client:
                        resp = await client.get(url)
                    if resp.status_code != 200:
                        counters["errors"] += 1
                        return
                    data = resp.content
                    dest = save_dir / f"{uid}.jpg"
                    sha = self.sha256_bytes(data)
                    artifact = write_atomic_artifact(
                        source=self.SOURCE_NAME,
                        artifact_id=f"bulk_avatar/{uid}",
                        artifact_kind="media_blob",
                        data=data,
                        extension="jpg",
                        expected_sha256=sha,
                        metadata={
                            "entity_id": str(uid),
                            "entity_name": str(uid),
                            "content_type": "profile_photo",
                            "content_id": f"avatar_{uid}",
                            "filename": dest.name,
                            "source_url": url,
                            "request_url": url,
                            "legacy_path": str(dest),
                            "bulk_avatar_range": True,
                            "rebuild_target_tables": ["media_items"],
                        },
                        root=VAULT_ROOT,
                    )
                    if not artifact.path:
                        counters["errors"] += 1
                        try:
                            await self.send_to_dlq(str(uid), f"avatar_{uid}", f"vault artifact write failed: {artifact.error}")
                        except Exception:
                            pass
                        return
                    if artifact.partial:
                        try:
                            await self.send_to_dlq(str(uid), f"avatar_{uid}", f"vault artifact partial: {artifact.error}")
                        except Exception:
                            pass
                    counters["downloaded"] += 1
                except Exception as e:
                    logger.debug("avatar_by_id %d failed: %s", uid, e)
                    counters["errors"] += 1

        # Process in concurrency-sized chunks so we can apply the toolkit's
        # between-batch politeness delay.
        ids = list(range(start_id, end_id + 1))
        for i in range(0, len(ids), concurrency):
            batch = ids[i : i + concurrency]
            await asyncio.gather(*[_one(u) for u in batch])
            if delay > 0 and i + concurrency < len(ids):
                await asyncio.sleep(delay)
        return counters

    # ===================================================================
    # Reconciler — re-download missing avatars (toolkit reconciler.py)
    # ===================================================================

    async def reconcile_avatars(self) -> dict[str, int]:
        """Walk media_items rows of content_type='profile_photo' and re-fetch
        any whose on-disk file vanished.

        Returns ``{"total": N, "missing": M, "redownloaded": R, "errors": E}``.
        """
        stats = {"total": 0, "missing": 0, "redownloaded": 0, "errors": 0}
        if self.pool is None:
            return stats
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT entity_id, entity_name, file_path, source_url, metadata "
                "FROM media_items "
                "WHERE source = 'github' AND content_type = 'profile_photo' "
                "  AND file_path IS NOT NULL AND file_path <> ''"
            )
        for row in rows:
            stats["total"] += 1
            file_path = Path(row["file_path"])
            if file_path.exists():
                continue
            stats["missing"] += 1
            entity_name = row["entity_name"]
            if not entity_name:
                continue
            try:
                changed, _ = await self.track_avatar_changes(entity_name)
                if changed:
                    stats["redownloaded"] += 1
            except Exception:
                stats["errors"] += 1
        return stats

    # ===================================================================
    # Spider queue drain (legacy + Wave 0 SpiderDiscover wrapper)
    # ===================================================================

    def make_edge_fetcher(self) -> GithubEdgeFetcher:
        """Build a Wave 0 ``EdgeFetcher`` over this collector. Useful when a
        downstream wants to plug GitHub into ``SpiderDiscover`` directly."""
        return GithubEdgeFetcher(self)

    def make_spider_discover(self, *, max_hops: Optional[int] = None) -> SpiderDiscover:
        """Build a ``SpiderDiscover`` instance ready to run against the unified
        ``spider_queue`` table. The legacy ``github_spider_queue`` drain is
        still preferred by ``collect()``; this is the new path."""
        return SpiderDiscover(
            platform="github",
            fetcher=self.make_edge_fetcher(),
            pool=self.pool,
            max_hops=max_hops if max_hops is not None else self._spider_depth,
            concurrency=int(os.getenv("GITHUB_SPIDER_CONCURRENCY", "4")),
        )

    # ===================================================================
    # Original collector interface — preserved
    # ===================================================================

    async def collect(self, targets: list[str]):
        target_concurrency = int(os.getenv("GITHUB_TARGET_CONCURRENCY", "4"))
        target_sem = asyncio.Semaphore(target_concurrency)
        owner_accounts = self._owner_accounts()

        async def _process_one(client: httpx.AsyncClient, target: str):
            async with target_sem:
                if self._stop.is_set():
                    return
                logger.info("Collecting github/%s", target)
                try:
                    if "/" in target:
                        await self._collect_repo(client, target)
                    else:
                        await self._collect_user(client, target)
                        if target.strip().lstrip("@").lower() in owner_accounts:
                            await self._record_owner_follow_graph(client, target)
                        if self._spider_depth > 0:
                            await self._spider_social_graph(client, target)
                except Exception as e:
                    logger.error("Failed github/%s: %s", target, e, exc_info=True)
                    await self.send_to_dlq(target, target, str(e))

        async with self._make_client() as client:
            await asyncio.gather(
                *[_process_one(client, t) for t in targets], return_exceptions=True
            )

        if os.getenv("GITHUB_SPIDER_ENABLED", "true").lower() == "true":
            await self._process_spider_queue()

    async def _process_spider_queue(self):
        """Drain N pending rows from ``github_spider_queue`` per tick.

        Parallelised via ``GITHUB_SPIDER_CONCURRENCY`` workers racing on
        ``FOR UPDATE SKIP LOCKED``. Each worker pops one row, processes it,
        sleeps the per-user delay, then loops.
        """
        await refresh_account_proximity_cache(self.pool)
        spider_concurrency = int(os.getenv("GITHUB_SPIDER_CONCURRENCY", "4"))
        processed_counter = {"n": 0}

        async def _drain_worker(worker_id: int, client: httpx.AsyncClient):
            while (
                processed_counter["n"] < self._spider_batch_size
                and not self._stop.is_set()
            ):
                async with self.pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        UPDATE github_spider_queue
                        SET status = 'processing'
                        WHERE id = (
                            SELECT q.id
                            FROM github_spider_queue q
                            LEFT JOIN LATERAL (
                                SELECT MIN(ap.tier) AS proximity_tier
                                FROM account_proximity_cache ap
                                WHERE ap.platform = 'github'
                                  AND q.target_type = 'user'
                                  AND ap.account_id = lower(q.target_identifier)
                            ) prox ON TRUE
                            WHERE q.status = 'pending' AND q.priority <= $1
                            ORDER BY
                                CASE
                                    WHEN prox.proximity_tier IN (1, 2) THEN 2
                                    WHEN prox.proximity_tier = 3 THEN 1
                                    ELSE 0
                                END DESC,
                                q.priority ASC,
                                q.collected_at ASC
                            FOR UPDATE SKIP LOCKED
                            LIMIT 1
                        )
                        RETURNING id, target_type, target_identifier, priority
                        """,
                        self._spider_depth,
                    )
                if not row:
                    logger.info(
                        "Spider drain worker=%d: queue empty or depth-exhausted",
                        worker_id,
                    )
                    return
                processed_counter["n"] += 1
                slot = processed_counter["n"]
                qid = row["id"]
                ttype = row["target_type"]
                tid = row["target_identifier"]
                depth = row["priority"] or 1
                logger.info(
                    "Spider drain w=%d: processing %s/%s (depth=%d, %d/%d)",
                    worker_id, ttype, tid, depth,
                    slot, self._spider_batch_size,
                )
                try:
                    if ttype == "user":
                        await self._collect_user(client, tid)
                        if depth < self._spider_depth:
                            await self._enqueue_neighbors(client, tid, depth + 1)
                    else:
                        await self._collect_repo(client, tid)
                    async with self.pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE github_spider_queue SET status='done' WHERE id=$1",
                            qid,
                        )
                except Exception as e:
                    # GitHub returns 403 (rate limit), 404/410 (gone), 409 (empty
                    # repo), 451 (DMCA/unavailable), 422 routinely while spidering —
                    # these are EXPECTED, not faults. Logging them at WARNING flooded
                    # the logs and tripped the worker's fatal-spin self-heal into
                    # needless ~45-min restarts. Demote expected HTTP errors to debug;
                    # only warn on genuinely unexpected failures.
                    status = getattr(getattr(e, "response", None), "status_code", None)
                    if status in (401, 403, 404, 409, 410, 422, 451, 502, 503):
                        logger.debug(
                            "Spider drain w=%d skip %s/%s (HTTP %s)",
                            worker_id, ttype, tid, status,
                        )
                    else:
                        logger.warning(
                            "Spider drain w=%d failed for %s/%s: %s",
                            worker_id, ttype, tid, e,
                        )
                    async with self.pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE github_spider_queue SET status='failed' WHERE id=$1",
                            qid,
                        )
                await asyncio.sleep(self._spider_user_delay)

        async with self._make_client() as client:
            await asyncio.gather(
                *[_drain_worker(i, client) for i in range(spider_concurrency)],
                return_exceptions=True,
            )
            logger.info(
                "Spider drain tick complete: processed=%d (workers=%d)",
                processed_counter["n"], spider_concurrency,
            )

    async def _enqueue_neighbors(
        self, client: httpx.AsyncClient, username: str, depth: int
    ) -> int:
        if depth > self._spider_depth:
            return 0
        added = 0
        for endpoint in ("followers", "following"):
            users = await self._paginate(
                client, f"{API_BASE}/users/{username}/{endpoint}"
            )
            edge_type = "follower" if endpoint == "followers" else "following"
            for u in users:
                login = (u or {}).get("login", "")
                if not login:
                    continue
                await self._upsert_edge(
                    source_login=username,
                    target_login=login,
                    repo_full_name=None,
                    edge_type=edge_type,
                    strength=70 if endpoint == "following" else 65,
                    evidence_url=f"https://github.com/{username}?tab={endpoint}",
                    evidence_id=f"{username}:{endpoint}:{login}",
                    raw_payload=u,
                    queue_priority=depth,
                )
                if await self._enqueue_user(login, depth, source=endpoint):
                    added += 1
        logger.info(
            "Spider neighbors of %s: enqueued %d new at depth %d",
            username, added, depth,
        )
        return added

    async def _record_owner_follow_graph(
        self, client: httpx.AsyncClient, owner_login: str
    ) -> dict[str, int]:
        """Persist the owner's direct GitHub graph into follow_edges."""
        owner = (owner_login or "").strip().lstrip("@")
        if not owner:
            return {"follower": 0, "following": 0}
        counts = {"follower": 0, "following": 0}
        for endpoint, direction in (("followers", "follower"), ("following", "following")):
            users = await self._paginate(client, f"{API_BASE}/users/{owner}/{endpoint}")
            if not users:
                continue
            async with self.pool.acquire() as conn:
                for u in users:
                    if not isinstance(u, dict):
                        continue
                    uid = u.get("id")
                    login = (u.get("login") or "").strip().lstrip("@")
                    if uid is None or not login:
                        continue
                    uid_s = str(uid)
                    await conn.execute(
                        """
                        INSERT INTO github_users
                            (platform_user_id, login, collected_at)
                        VALUES ($1, $2, NOW())
                        ON CONFLICT (platform_user_id) DO UPDATE SET
                            login = COALESCE(NULLIF(EXCLUDED.login, ''), github_users.login),
                            collected_at = NOW()
                        """,
                        int(uid), login,
                    )
                    await conn.execute(
                        """
                        INSERT INTO social_users
                            (platform, uid, platform_user_id, username, display_name,
                             profile_photo_url, contexts, first_seen, last_seen, times_seen)
                        VALUES ('github', $1, $1, $2, $2, $3, ARRAY[$4], now(), now(), 1)
                        ON CONFLICT (platform, uid) DO UPDATE SET
                            last_seen = now(),
                            times_seen = social_users.times_seen + 1,
                            username = COALESCE(EXCLUDED.username, social_users.username),
                            display_name = COALESCE(social_users.display_name, EXCLUDED.display_name),
                            profile_photo_url = COALESCE(EXCLUDED.profile_photo_url, social_users.profile_photo_url),
                            contexts = (SELECT array_agg(DISTINCT c) FROM unnest(social_users.contexts || EXCLUDED.contexts) AS c)
                        """,
                        uid_s, login, u.get("avatar_url"), direction,
                    )
                    await conn.execute(
                        """
                        INSERT INTO follow_edges
                            (platform, owner_account, target_uid, direction,
                             target_username, first_seen, last_seen)
                        VALUES ('github', $1, $2, $3, $4, now(), now())
                        ON CONFLICT (platform, owner_account, target_uid, direction)
                        DO UPDATE SET
                            last_seen = now(),
                            target_username = COALESCE(EXCLUDED.target_username, follow_edges.target_username)
                        """,
                        owner, uid_s, direction, login,
                    )
                    await self._upsert_edge(
                        source_login=owner,
                        target_login=login,
                        repo_full_name=None,
                        edge_type=direction,
                        strength=80 if direction == "following" else 70,
                        evidence_url=f"https://github.com/{owner}?tab={endpoint}",
                        evidence_id=f"{owner}:{endpoint}:{login}",
                        raw_payload=u,
                        queue_priority=1,
                    )
                    counts[direction] += 1
        logger.info(
            "github owner graph[%s]: followers=%d following=%d",
            owner, counts["follower"], counts["following"],
        )
        return counts

    async def _collect_user(self, client: httpx.AsyncClient, username: str):
        resp = await self._api_get(client, f"{API_BASE}/users/{username}")
        if resp is None:
            return
        user = resp.json()
        uid = str(user["id"])
        login = user["login"]

        # FAMOUS-FILTER: optionally skip users at/above the cap (by followers).
        # Bryan's own account is below the cap so it always collects.
        if (self._famous_star_cap and self._famous_filter_users
                and int(user.get("followers", 0) or 0) >= self._famous_star_cap):
            logger.info("github: skipping famous user %s (%s followers >= cap %d)",
                        login, user.get("followers"), self._famous_star_cap)
            return

        await self._upsert_user(user)

        if user.get("avatar_url"):
            avatar_url = user["avatar_url"]
            if self._avatar_size:
                sep = "&" if "?" in avatar_url else "?"
                avatar_url = f"{avatar_url}{sep}s={self._avatar_size}"
            dest_dir = self.account_media_dir / "profiles"
            dest_dir.mkdir(parents=True, exist_ok=True)
            changed, path = await self._photo_tracker.check_and_download(
                avatar_url, uid, "github", dest_dir
            )
            if changed and path:
                metadata = {"raw": user}
                artifact_meta = self._photo_tracker.last_artifact_metadata()
                if artifact_meta:
                    metadata["vault_artifact"] = artifact_meta
                await self.insert_media_item(
                    entity_id=uid,
                    entity_name=login,
                    content_type="profile_photo",
                    content_id=f"avatar_{uid}",
                    filename=path.name,
                    file_path=str(path),
                    file_size=path.stat().st_size,
                    sha256=self.sha256_bytes(path.read_bytes()),
                    metadata=metadata,
                    source_url=self._build_github_source_url(login),
                )

        repos = await self._paginate(client, f"{API_BASE}/users/{username}/repos")
        for repo in repos:
            if self._stop.is_set():
                break
            await self._upsert_repo(repo)
            # FAMOUS-FILTER: upsert the repo row (metadata) but collect NO content
            # from repos at/above the star cap.
            if self._famous_star_cap and int(repo.get("stargazers_count", 0) or 0) >= self._famous_star_cap:
                logger.info("github: skipping famous repo %s (%s stars >= cap %d)",
                            repo.get("full_name"), repo.get("stargazers_count"), self._famous_star_cap)
                continue
            await self._collect_repo_content(
                client, repo["full_name"], uid, login, repo.get("id"))

        await self.checkpoint.save_progress(username)

    async def _collect_repo(self, client: httpx.AsyncClient, full_name: str):
        resp = await self._api_get(client, f"{API_BASE}/repos/{full_name}")
        if resp is None:
            return
        repo = resp.json()
        await self._upsert_repo(repo)
        # FAMOUS-FILTER: skip content collection for repos at/above the star cap.
        if self._famous_star_cap and int(repo.get("stargazers_count", 0) or 0) >= self._famous_star_cap:
            logger.info("github: skipping famous repo %s (%s stars >= cap %d)",
                        full_name, repo.get("stargazers_count"), self._famous_star_cap)
            await self.checkpoint.save_progress(full_name)
            return
        await self._collect_repo_content(
            client, full_name,
            str(repo["owner"]["id"]), repo["owner"]["login"], repo.get("id"),
        )
        await self.checkpoint.save_progress(full_name)

    async def _store_commit_and_edges(
        self,
        repo_uuid,
        full_name: str,
        repo_owner: str | None,
        commit: dict,
        *,
        edge_prefix: str = "commit",
    ) -> None:
        await self._upsert_commit(repo_uuid, commit)
        self._tick_progress()
        commit_block = commit.get("commit") or {}
        commit_author = (commit.get("author") or {}).get("login")
        committer = (commit.get("committer") or {}).get("login")
        html_url = commit.get("html_url")
        sha = commit.get("sha") or ""
        if self._is_human_login(commit_author, commit.get("author")):
            await self._upsert_edge(
                source_login=repo_owner,
                target_login=commit_author,
                repo_full_name=full_name,
                edge_type=f"{edge_prefix}_author",
                strength=80,
                evidence_url=html_url,
                evidence_id=f"{full_name}:{edge_prefix}:{sha}:author",
                raw_payload=commit,
                queue_priority=1,
            )
        if self._is_human_login(committer, commit.get("committer")):
            await self._upsert_edge(
                source_login=repo_owner,
                target_login=committer,
                repo_full_name=full_name,
                edge_type=f"{edge_prefix}_committer",
                strength=70,
                evidence_url=html_url,
                evidence_id=f"{full_name}:{edge_prefix}:{sha}:committer",
                raw_payload=commit,
                queue_priority=2,
            )
        await self._record_mentions(
            source_login=commit_author or committer or repo_owner,
            repo_full_name=full_name,
            edge_type=f"{edge_prefix}_mention",
            strength=55,
            evidence_url=html_url,
            evidence_id=f"{full_name}:{edge_prefix}:{sha}",
            raw_payload=commit,
            texts=(commit_block.get("message"),),
        )

    async def _collect_repo_content(
        self, client: httpx.AsyncClient, full_name: str, uid: str, login: str,
        repo_pid: int | None = None,
    ):
        max_commits = self._env_limit("GITHUB_MAX_COMMITS_PER_REPO")
        max_issues = self._env_limit("GITHUB_MAX_ISSUES_PER_REPO")
        max_contributors = self._env_limit("GITHUB_MAX_CONTRIBUTORS_PER_REPO")
        weak_fanout_limit = self._env_limit("GITHUB_MAX_WEAK_FANOUT_PER_REPO", "500")
        repo_owner = self._repo_owner(full_name) or login

        # Resolve the internal repo UUID for foreign-key linking. Prefer the
        # STABLE platform_repo_id — looking up by full_name orphaned commits
        # whenever a repo had been renamed (ON CONFLICT DO UPDATE didn't refresh
        # full_name), which is how ~862k commits landed with NULL repo_id.
        repo_uuid = None
        try:
            async with self.pool.acquire() as conn:
                row = None
                if repo_pid is not None:
                    row = await conn.fetchrow(
                        "SELECT id FROM github_repos WHERE platform_repo_id = $1",
                        repo_pid,
                    )
                if row is None:
                    row = await conn.fetchrow(
                        "SELECT id FROM github_repos WHERE full_name = $1", full_name
                    )
                if row:
                    repo_uuid = row["id"]
        except Exception:
            logger.debug("repo UUID lookup failed for %s", full_name)

        if repo_uuid is None:
            # Never insert commits with a NULL repo_id (orphans them from the
            # dashboard). Skip content for this repo; it'll be retried next cycle
            # once the repo row exists.
            logger.warning(
                "github: no repo UUID for %s (pid=%s) — skipping commit/content "
                "collection to avoid NULL repo_id orphans", full_name, repo_pid)
            return

        # 1. README
        readme_resp = await self._api_get(client, f"{API_BASE}/repos/{full_name}/readme")
        if readme_resp:
            readme = readme_resp.json()
            content = base64.b64decode(readme.get("content", "")).decode(
                "utf-8", "ignore"
            )
            await self._upsert_readme(
                readme.get("repository_id") or 0,
                content,
                readme.get("sha"),
                readme.get("size"),
            )

        # 2. Commits (unbounded when GITHUB_MAX_COMMITS_PER_REPO=0/unset)
        commits = await self._paginate(
            client, f"{API_BASE}/repos/{full_name}/commits", max_items=max_commits
        )
        for c in commits:
            await self._store_commit_and_edges(repo_uuid, full_name, repo_owner, c)

        if os.getenv("GITHUB_COLLECT_BRANCH_COMMITS", "true").lower() == "true":
            branches = await self._paginate(
                client,
                f"{API_BASE}/repos/{full_name}/branches",
                max_items=self._env_limit("GITHUB_MAX_BRANCHES_PER_REPO"),
            )
            for branch in branches:
                branch_name = (branch or {}).get("name")
                if not branch_name:
                    continue
                branch_commits = await self._paginate(
                    client,
                    f"{API_BASE}/repos/{full_name}/commits?sha={quote(branch_name, safe='')}",
                    max_items=max_commits,
                )
                for c in branch_commits:
                    await self._store_commit_and_edges(
                        repo_uuid,
                        full_name,
                        repo_owner,
                        c,
                        edge_prefix="branch_commit",
                    )

        # 3. Issues and PRs (unbounded when GITHUB_MAX_ISSUES_PER_REPO=0/unset)
        issues = await self._paginate(
            client, f"{API_BASE}/repos/{full_name}/issues?state=all", max_items=max_issues
        )
        for i in issues:
            issue_uuid = await self._upsert_issue(repo_uuid, i)
            issue_number = i.get("number")
            issue_author = (i.get("user") or {}).get("login")
            is_pr = bool(i.get("pull_request"))
            html_url = i.get("html_url")
            issue_edge_type = "pr_author" if is_pr else "issue_author"
            if self._is_human_login(issue_author, i.get("user")):
                await self._upsert_edge(
                    source_login=repo_owner,
                    target_login=issue_author,
                    repo_full_name=full_name,
                    edge_type=issue_edge_type,
                    strength=85 if is_pr else 75,
                    evidence_url=html_url,
                    evidence_id=f"{full_name}:issue:{issue_number}:author",
                    raw_payload=i,
                    queue_priority=1,
                )
            for assignee in i.get("assignees") or []:
                assignee_login = (assignee or {}).get("login")
                if self._is_human_login(assignee_login, assignee):
                    await self._upsert_edge(
                        source_login=issue_author or repo_owner,
                        target_login=assignee_login,
                        repo_full_name=full_name,
                        edge_type="issue_assignee",
                        strength=65,
                        evidence_url=html_url,
                        evidence_id=f"{full_name}:issue:{issue_number}:assignee:{assignee_login}",
                        raw_payload=assignee,
                        queue_priority=2,
                    )
            await self._record_mentions(
                source_login=issue_author or repo_owner,
                repo_full_name=full_name,
                edge_type="pr_mention" if is_pr else "issue_mention",
                strength=60,
                evidence_url=html_url,
                evidence_id=f"{full_name}:issue:{issue_number}",
                raw_payload=i,
                texts=(i.get("title"), i.get("body")),
            )
            if issue_number is not None:
                await self._collect_issue_comments(
                    client,
                    full_name,
                    repo_uuid,
                    issue_uuid,
                    int(issue_number),
                    source_login=issue_author or repo_owner,
                    is_pr=is_pr,
                )
                if is_pr:
                    await self._collect_pr_reviews(
                        client,
                        full_name,
                        repo_uuid,
                        int(issue_number),
                        source_login=issue_author or repo_owner,
                    )
                    await self._collect_pr_review_comments(
                        client,
                        full_name,
                        repo_uuid,
                        int(issue_number),
                        source_login=issue_author or repo_owner,
                    )

        # 4. Releases / assets — skip for repos we don't own (too large)
        _own_logins = {login.lower()} if login else set()
        _skip_assets = full_name.lower() not in {f"{login.lower()}/{r}" for r in []} and login.lower() not in {
            os.getenv("GITHUB_ASSET_OWNER_LOGIN", "bryanseah234").lower()
        }
        releases = await self._paginate(
            client, f"{API_BASE}/repos/{full_name}/releases"
        )
        for release in releases:
            for asset in release.get("assets", []):
                if _skip_assets:
                    continue  # only download assets from our own repos
                if self.is_known(str(asset["id"])):
                    continue
                await self.download_media({
                    "entity_id": uid, "entity_name": login,
                    "content_type": "release", "content_id": str(asset["id"]),
                    "url": asset["browser_download_url"],
                    "extension": Path(asset["name"]).suffix.lstrip(".") or "bin",
                    "source_url": asset["browser_download_url"], "raw": asset,
                })

        # 5. Contributors -> edges + spider queue. max_contributors=None means all pages.
        if max_contributors != 0 and self._spider_depth > 0:
            try:
                contributors = await self._paginate(
                    client,
                    f"{API_BASE}/repos/{full_name}/contributors",
                    max_items=max_contributors,
                )
                added = 0
                for c in contributors:
                    contrib_login = (c or {}).get("login")
                    if not contrib_login or (c or {}).get("type") == "Bot":
                        continue
                    await self._upsert_edge(
                        source_login=repo_owner,
                        target_login=contrib_login,
                        repo_full_name=full_name,
                        edge_type="repo_contributor",
                        strength=75,
                        evidence_url=f"https://github.com/{full_name}/graphs/contributors",
                        evidence_id=f"{full_name}:contributor:{contrib_login}",
                        raw_payload=c,
                        queue_priority=2,
                    )
                    if await self._enqueue_user(contrib_login, 2, source="contributors"):
                        added += 1
                if added:
                    logger.info(
                        "Spider contributors of %s: enqueued %d new",
                        full_name, added,
                    )
            except Exception as e:
                logger.debug("contributor fetch failed for %s: %s", full_name, e)

        await self._collect_weak_repo_fanout(client, full_name, repo_owner, weak_fanout_limit)

    async def _collect_issue_comments(
        self,
        client: httpx.AsyncClient,
        full_name: str,
        repo_uuid,
        issue_uuid,
        issue_number: int,
        *,
        source_login: str,
        is_pr: bool,
    ) -> int:
        comments = await self._paginate(
            client,
            f"{API_BASE}/repos/{full_name}/issues/{issue_number}/comments",
        )
        count = 0
        for comment in comments:
            await self._upsert_issue_comment(repo_uuid, issue_uuid, issue_number, comment)
            self._tick_progress()
            author = (comment.get("user") or {}).get("login")
            if self._is_human_login(author, comment.get("user")):
                await self._upsert_edge(
                    source_login=source_login,
                    target_login=author,
                    repo_full_name=full_name,
                    edge_type="pr_issue_commenter" if is_pr else "issue_commenter",
                    strength=75 if is_pr else 65,
                    evidence_url=comment.get("html_url"),
                    evidence_id=f"{full_name}:issue:{issue_number}:comment:{comment.get('id')}",
                    raw_payload=comment,
                    queue_priority=1 if is_pr else 2,
                )
                count += 1
            await self._record_mentions(
                source_login=author or source_login,
                repo_full_name=full_name,
                edge_type="pr_comment_mention" if is_pr else "issue_comment_mention",
                strength=60,
                evidence_url=comment.get("html_url"),
                evidence_id=f"{full_name}:issue:{issue_number}:comment:{comment.get('id')}",
                raw_payload=comment,
                texts=(comment.get("body"),),
            )
        return count

    async def _collect_pr_reviews(
        self,
        client: httpx.AsyncClient,
        full_name: str,
        repo_uuid,
        pr_number: int,
        *,
        source_login: str,
    ) -> int:
        reviews = await self._paginate(
            client,
            f"{API_BASE}/repos/{full_name}/pulls/{pr_number}/reviews",
        )
        count = 0
        for review in reviews:
            await self._upsert_pr_review(repo_uuid, pr_number, review)
            self._tick_progress()
            reviewer = (review.get("user") or {}).get("login")
            if self._is_human_login(reviewer, review.get("user")):
                await self._upsert_edge(
                    source_login=source_login,
                    target_login=reviewer,
                    repo_full_name=full_name,
                    edge_type="pr_reviewer",
                    strength=90,
                    evidence_url=review.get("html_url"),
                    evidence_id=f"{full_name}:pr:{pr_number}:review:{review.get('id')}",
                    raw_payload=review,
                    queue_priority=1,
                )
                count += 1
            await self._record_mentions(
                source_login=reviewer or source_login,
                repo_full_name=full_name,
                edge_type="pr_review_mention",
                strength=65,
                evidence_url=review.get("html_url"),
                evidence_id=f"{full_name}:pr:{pr_number}:review:{review.get('id')}",
                raw_payload=review,
                texts=(review.get("body"),),
            )
        return count

    async def _collect_pr_review_comments(
        self,
        client: httpx.AsyncClient,
        full_name: str,
        repo_uuid,
        pr_number: int,
        *,
        source_login: str,
    ) -> int:
        comments = await self._paginate(
            client,
            f"{API_BASE}/repos/{full_name}/pulls/{pr_number}/comments",
        )
        count = 0
        for comment in comments:
            await self._upsert_pr_review_comment(repo_uuid, pr_number, comment)
            self._tick_progress()
            commenter = (comment.get("user") or {}).get("login")
            if self._is_human_login(commenter, comment.get("user")):
                await self._upsert_edge(
                    source_login=source_login,
                    target_login=commenter,
                    repo_full_name=full_name,
                    edge_type="pr_review_commenter",
                    strength=85,
                    evidence_url=comment.get("html_url"),
                    evidence_id=f"{full_name}:pr:{pr_number}:review_comment:{comment.get('id')}",
                    raw_payload=comment,
                    queue_priority=1,
                )
                count += 1
            await self._record_mentions(
                source_login=commenter or source_login,
                repo_full_name=full_name,
                edge_type="pr_review_comment_mention",
                strength=65,
                evidence_url=comment.get("html_url"),
                evidence_id=f"{full_name}:pr:{pr_number}:review_comment:{comment.get('id')}",
                raw_payload=comment,
                texts=(comment.get("body"),),
            )
        return count

    async def _collect_weak_repo_fanout(
        self,
        client: httpx.AsyncClient,
        full_name: str,
        repo_owner: str | None,
        max_items: int | None,
    ) -> dict[str, int]:
        if not repo_owner:
            return {"fork_owner": 0, "stargazer": 0, "watcher": 0}
        endpoints = (
            ("forks", "fork_owner", 45),
            ("stargazers", "stargazer", 30),
            ("subscribers", "watcher", 25),
        )
        counts = {"fork_owner": 0, "stargazer": 0, "watcher": 0}
        for endpoint, edge_type, strength in endpoints:
            try:
                rows = await self._paginate(
                    client,
                    f"{API_BASE}/repos/{full_name}/{endpoint}",
                    max_items=max_items,
                )
            except Exception as e:
                logger.debug("github weak fanout %s failed for %s: %s", endpoint, full_name, e)
                continue
            for row in rows:
                user = row.get("owner") if edge_type == "fork_owner" else row
                login = (user or {}).get("login")
                if not self._is_human_login(login, user):
                    continue
                await self._upsert_edge(
                    source_login=repo_owner,
                    target_login=login,
                    repo_full_name=full_name,
                    edge_type=edge_type,
                    strength=strength,
                    evidence_url=(row.get("html_url") if edge_type == "fork_owner" else f"https://github.com/{full_name}/{endpoint}"),
                    evidence_id=f"{full_name}:{edge_type}:{login}",
                    raw_payload=row,
                    queue_priority=3,
                )
                self._tick_progress()
                counts[edge_type] += 1
        return counts

    # ---- DB upserts -----------------------------------------------------

    async def _upsert_user(self, user_data: dict):
        # ── User-intelligence diff (Tier 4): snapshot the row BEFORE upserting
        # so UserChangeTracker can compare old → new and emit one row per
        # changed field into github_user_changes. Wrapped in try/except so any
        # failure (DB, schema drift, etc.) is non-fatal to ingestion.
        prev_row = None
        try:
            async with self.pool.acquire() as conn:
                prev_row = await conn.fetchrow(
                    "SELECT login, name, company, blog, location, bio, "
                    "public_repos_count, followers_count, following_count "
                    "FROM github_users WHERE platform_user_id = $1",
                    user_data.get("id"),
                )
        except Exception as exc:
            logger.debug("user_change_tracker[github]: prev-row fetch failed: %s", exc)

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO github_users (
                    platform_user_id, login, name, company, blog, location,
                    email, bio, public_repos_count, followers_count,
                    following_count, platform_created_at, collected_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NOW())
                ON CONFLICT (platform_user_id) DO UPDATE SET
                    login = EXCLUDED.login,
                    name = EXCLUDED.name,
                    company = EXCLUDED.company,
                    blog = EXCLUDED.blog,
                    location = EXCLUDED.location,
                    email = EXCLUDED.email,
                    bio = EXCLUDED.bio,
                    public_repos_count = EXCLUDED.public_repos_count,
                    followers_count = EXCLUDED.followers_count,
                    following_count = EXCLUDED.following_count,
                    collected_at = NOW()
                """,
                user_data.get("id"), user_data.get("login"), user_data.get("name"),
                user_data.get("company"), user_data.get("blog"),
                user_data.get("location"),
                None if (user_data.get("email") or "").endswith("noreply.github.com") else user_data.get("email"),
                user_data.get("bio"), user_data.get("public_repos"),
                user_data.get("followers"), user_data.get("following"),
                _parse_iso(user_data.get("created_at")),
            )

        # ── Change-log write (non-fatal). Field names match github_users
        # column names, so prev_row passes through unmodified.
        try:
            tracker = UserChangeTracker(self.pool)
            new_snapshot = {
                "login":              user_data.get("login"),
                "name":               user_data.get("name"),
                "company":            user_data.get("company"),
                "blog":               user_data.get("blog"),
                "location":           user_data.get("location"),
                "bio":                user_data.get("bio"),
                "public_repos_count": user_data.get("public_repos"),
                "followers_count":    user_data.get("followers"),
                "following_count":    user_data.get("following"),
            }
            await tracker.detect_and_log(
                table="github_user_changes",
                pk_col="user_id",
                pk_val=int(user_data["id"]),
                current_row=dict(prev_row) if prev_row is not None else None,
                new_row=new_snapshot,
                fields=GITHUB_TRACKED_FIELDS,
            )
        except Exception as exc:
            logger.debug("user_change_tracker[github]: detect_and_log failed: %s", exc)

    async def _upsert_repo(self, repo_data: dict):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO github_repos (
                    platform_repo_id, name, full_name, description, homepage,
                    language, stargazers_count, watchers_count, forks_count,
                    open_issues_count, topics, license, platform_created_at,
                    platform_updated_at, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                ON CONFLICT (platform_repo_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    full_name = EXCLUDED.full_name,
                    stargazers_count = EXCLUDED.stargazers_count,
                    forks_count = EXCLUDED.forks_count,
                    platform_updated_at = EXCLUDED.platform_updated_at,
                    metadata = EXCLUDED.metadata
                """,
                repo_data.get("id"), repo_data.get("name"),
                repo_data.get("full_name"), repo_data.get("description"),
                repo_data.get("homepage"), repo_data.get("language"),
                repo_data.get("stargazers_count"), repo_data.get("watchers_count"),
                repo_data.get("forks_count"), repo_data.get("open_issues_count"),
                repo_data.get("topics"),
                (repo_data.get("license") or {}).get("name") if repo_data.get("license") else None,
                _parse_iso(repo_data.get("created_at")),
                _parse_iso(repo_data.get("updated_at")),
                json.dumps(repo_data, default=str),
            )

    async def _upsert_readme(self, repo_id: int, content: str, sha: str, size: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM github_repos WHERE platform_repo_id = $1", repo_id
            )
            if row:
                await conn.execute(
                    "INSERT INTO github_readmes (repo_id, content, sha, size, collected_at) "
                    "VALUES ($1, $2, $3, $4, NOW())",
                    row["id"], content, sha, size,
                )

    async def _upsert_commit(self, repo_id, commit: dict):
        # GitHub returns null for "commit", "commit.author", and top-level
        # "author" when commits are imported, signed without a GH account, or
        # authored by deleted users. dict.get(k, default) returns None when
        # the key exists with a None value, so we coalesce explicitly.
        c = commit.get("commit") or {}
        author = c.get("author") or {}
        gh_author = commit.get("author") or {}
        commit_date = _parse_iso(author.get("date"))
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO github_commits (
                    sha, repo_id, author_name, author_email, author_login,
                    message, date, collected_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                ON CONFLICT (sha, repo_id) DO NOTHING
                """,
                commit.get("sha"), repo_id, author.get("name"),
                None if (author.get("email") or "").endswith("noreply.github.com") else author.get("email"),
                gh_author.get("login"), c.get("message"), commit_date,
            )

    async def _upsert_issue(self, repo_id, issue: dict):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO github_issues (
                    repo_id, platform_issue_id, number, title, body, state,
                    is_pull_request, labels, comments_count, created_at, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (platform_issue_id) DO UPDATE SET
                    repo_id = COALESCE(EXCLUDED.repo_id, github_issues.repo_id),
                    state = EXCLUDED.state, updated_at = EXCLUDED.updated_at
                RETURNING id
                """,
                repo_id, issue.get("id"), issue.get("number"), issue.get("title"),
                issue.get("body"), issue.get("state"),
                "pull_request" in issue,
                [l.get("name") for l in issue.get("labels", [])],
                issue.get("comments"),
                _parse_iso(issue.get("created_at")),
                _parse_iso(issue.get("updated_at")),
            )
            return row["id"] if row else None

    async def _upsert_issue_comment(self, repo_id, issue_id, issue_number: int, comment: dict):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO github_issue_comments (
                    repo_id, issue_id, platform_comment_id, issue_number,
                    author_login, body, html_url, platform_created_at,
                    platform_updated_at, metadata, collected_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, NOW())
                ON CONFLICT (platform_comment_id) DO UPDATE SET
                    repo_id = COALESCE(EXCLUDED.repo_id, github_issue_comments.repo_id),
                    issue_id = COALESCE(EXCLUDED.issue_id, github_issue_comments.issue_id),
                    author_login = EXCLUDED.author_login,
                    body = EXCLUDED.body,
                    html_url = EXCLUDED.html_url,
                    platform_updated_at = EXCLUDED.platform_updated_at,
                    metadata = EXCLUDED.metadata,
                    collected_at = NOW()
                """,
                repo_id,
                issue_id,
                comment.get("id"),
                issue_number,
                (comment.get("user") or {}).get("login"),
                comment.get("body"),
                comment.get("html_url"),
                _parse_iso(comment.get("created_at")),
                _parse_iso(comment.get("updated_at")),
                json.dumps(comment, default=str),
            )

    async def _upsert_pr_review(self, repo_id, pr_number: int, review: dict):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO github_pr_reviews (
                    repo_id, platform_review_id, pr_number, author_login,
                    state, body, html_url, platform_submitted_at, metadata, collected_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, NOW())
                ON CONFLICT (platform_review_id) DO UPDATE SET
                    repo_id = COALESCE(EXCLUDED.repo_id, github_pr_reviews.repo_id),
                    author_login = EXCLUDED.author_login,
                    state = EXCLUDED.state,
                    body = EXCLUDED.body,
                    html_url = EXCLUDED.html_url,
                    platform_submitted_at = EXCLUDED.platform_submitted_at,
                    metadata = EXCLUDED.metadata,
                    collected_at = NOW()
                """,
                repo_id,
                review.get("id"),
                pr_number,
                (review.get("user") or {}).get("login"),
                review.get("state"),
                review.get("body"),
                review.get("html_url"),
                _parse_iso(review.get("submitted_at")),
                json.dumps(review, default=str),
            )

    async def _upsert_pr_review_comment(self, repo_id, pr_number: int, comment: dict):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO github_pr_review_comments (
                    repo_id, platform_comment_id, pr_number, author_login,
                    body, html_url, path, position, platform_created_at,
                    platform_updated_at, metadata, collected_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, NOW())
                ON CONFLICT (platform_comment_id) DO UPDATE SET
                    repo_id = COALESCE(EXCLUDED.repo_id, github_pr_review_comments.repo_id),
                    author_login = EXCLUDED.author_login,
                    body = EXCLUDED.body,
                    html_url = EXCLUDED.html_url,
                    path = EXCLUDED.path,
                    position = EXCLUDED.position,
                    platform_updated_at = EXCLUDED.platform_updated_at,
                    metadata = EXCLUDED.metadata,
                    collected_at = NOW()
                """,
                repo_id,
                comment.get("id"),
                pr_number,
                (comment.get("user") or {}).get("login"),
                comment.get("body"),
                comment.get("html_url"),
                comment.get("path"),
                comment.get("position"),
                _parse_iso(comment.get("created_at")),
                _parse_iso(comment.get("updated_at")),
                json.dumps(comment, default=str),
            )

    # ---- media download (release assets) -------------------------------

    @staticmethod
    def _build_github_source_url(login_or_entity_name: str | None) -> str | None:
        """Canonical GitHub profile URL for media_items.source_url. Every
        media row this collector writes is a profile avatar keyed by
        github login, so the source page for the file is simply the user's
        profile at https://github.com/<login>. Returns None on missing
        login."""
        login = (login_or_entity_name or "").strip().lstrip("@")
        if not login:
            return None
        return f"https://github.com/{login}"

    async def download_media(self, item: dict):
        cid = item["content_id"]
        if self.is_known(cid):
            return
        filename = self.build_filename(
            item["entity_id"], item["entity_name"], item["content_type"], cid,
            extension=item.get("extension", "jpg"),
        )
        try:
            await asyncio.sleep(self._download_delay)
            async with self._make_client() as client:
                resp = await client.get(item["url"])
                resp.raise_for_status()
                data = resp.content
            sha = self.sha256_bytes(data)
            source_url = item.get("source_url") or self._build_github_source_url(item.get("entity_name"))
            metadata = {
                "entity_id": item["entity_id"], "entity_name": item["entity_name"],
                "content_type": item["content_type"], "content_id": cid,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "raw": item.get("raw", {}),
                "rebuild_target_tables": ["media_items", "github_releases", "github_repositories"],
            }
            artifact = write_atomic_artifact(
                source=self.SOURCE_NAME,
                artifact_id=cid,
                artifact_kind="media_blob",
                data=data,
                extension=item.get("extension", "jpg"),
                expected_sha256=sha,
                metadata={
                    **metadata,
                    "filename": filename,
                    "source_url": source_url,
                    "request_url": item.get("url"),
                },
                root=VAULT_ROOT,
            )
            if not artifact.path:
                raise RuntimeError(f"vault artifact write failed: {artifact.error}")
            metadata["vault_artifact"] = {
                "ok": artifact.ok,
                "partial": artifact.partial,
                "path": artifact.relative_path,
                "blob_path": artifact.blob_relative_path,
                "sidecar_path": artifact.sidecar.relative_path if artifact.sidecar else None,
                "duplicate_blob": artifact.duplicate_blob,
                "error": artifact.error,
            }
            await self.insert_media_item(
                entity_id=item["entity_id"], entity_name=item["entity_name"],
                content_type=item["content_type"], content_id=cid,
                filename=filename, file_path=str(artifact.path),
                file_size=artifact.file_size, sha256=artifact.sha256,
                metadata=metadata, source_url=source_url,
            )
            if artifact.partial:
                await self.send_to_dlq(item["entity_id"], cid, f"vault artifact partial: {artifact.error}")
            self._known_ids.add(cid)
        except Exception as e:
            logger.error("Download failed %s: %s", cid, e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))

    async def _spider_social_graph(self, client: httpx.AsyncClient, seed_username: str):
        """Seed the spider queue with the seed user's direct followers/following.

        Deeper traversal is performed by ``_process_spider_queue`` which pops
        users from the queue in batches and enqueues their neighbors at
        depth+1. This avoids unbounded in-memory BFS that previously starved
        the queue worker.
        """
        await self._enqueue_neighbors(client, seed_username, depth=1)

    async def cleanup(self):
        """Optional periodic maintenance hook — runs the avatar reconciler.

        Wired off by default; the unified scheduler may invoke
        ``reconcile_avatars`` directly when desired.
        """
        return None
