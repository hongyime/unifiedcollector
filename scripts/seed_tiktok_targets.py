"""Bootstrap TikTok collection_targets from authenticated user's following list.

Uses TikTok's web API at /api/user/list/?scene=21 with msToken/cookie auth
(no X-Bogus signing required when correct params are sent). Iterates pages
via minCursor and inserts each unique username into collection_targets.

Run inside the collector container:
    docker exec unifiedcollector_collector python /app/scripts/seed_tiktok_targets.py
"""
from __future__ import annotations
import asyncio
import logging
import os
import sys
import time
from typing import Iterable

import httpx

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("seed_tiktok")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def load_cookies(path: str) -> dict:
    cookies = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies[parts[5]] = parts[6]
    return cookies


def get_account_info(cookies: dict) -> dict:
    r = httpx.get(
        "https://www.tiktok.com/passport/web/account/info/",
        cookies=cookies,
        headers={"User-Agent": UA, "Referer": "https://www.tiktok.com/"},
        timeout=20,
        follow_redirects=True,
    )
    r.raise_for_status()
    return r.json().get("data", {})


def fetch_following(cookies: dict, sec_uid: str, *, page_size: int = 30, max_pages: int = 200) -> list[dict]:
    """Iterate /api/user/list/ scene=21 (Following) until hasMore is False."""
    base_url = "https://www.tiktok.com/api/user/list/"
    headers = {
        "User-Agent": UA,
        "Referer": "https://www.tiktok.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    base_params = {
        "WebIdLastTime": 1700000000,
        "aid": 1988,
        "app_language": "en",
        "app_name": "tiktok_web",
        "browser_language": "en-US",
        "browser_name": "Mozilla",
        "browser_online": "true",
        "browser_platform": "Win32",
        "browser_version": "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "channel": "tiktok_web",
        "cookie_enabled": "true",
        "count": page_size,
        "data_collection_enabled": "true",
        "device_id": "7300000000000000000",
        "device_platform": "web_pc",
        "focus_state": "true",
        "from_page": "user",
        "history_len": 2,
        "is_fullscreen": "false",
        "is_page_visible": "true",
        "odinId": cookies.get("odin_tt", ""),
        "os": "windows",
        "priority_region": "",
        "referer": "",
        "region": "US",
        "scene": 21,  # Following list
        "screen_height": 1080,
        "screen_width": 1920,
        "secUid": sec_uid,
        "tz_name": "America/Los_Angeles",
        "user_is_login": "true",
        "webcast_language": "en",
        "msToken": cookies.get("msToken", ""),
    }

    all_users: list[dict] = []
    seen_ids: set[str] = set()
    min_cursor = 0
    max_cursor = 0

    for page in range(max_pages):
        p = dict(base_params)
        p["minCursor"] = min_cursor
        p["maxCursor"] = max_cursor
        try:
            r = httpx.get(base_url, params=p, cookies=cookies, headers=headers, timeout=25, follow_redirects=True)
        except Exception as e:
            log.warning("page %d httpx error: %s", page, e)
            break
        if r.status_code != 200:
            log.warning("page %d HTTP %d body=%r", page, r.status_code, r.text[:200])
            break
        try:
            data = r.json()
        except Exception:
            log.warning("page %d non-JSON body=%r", page, r.text[:200])
            break

        users = data.get("userList") or []
        new_count = 0
        for u in users:
            user = u.get("user") or {}
            uname = user.get("uniqueId")
            sec = user.get("secUid", "")
            uid = sec or uname
            if not uname or uid in seen_ids:
                continue
            seen_ids.add(uid)
            all_users.append({
                "username": uname,
                "nickname": user.get("nickname") or "",
                "sec_uid": sec,
                "user_id": str(user.get("id") or ""),
            })
            new_count += 1

        has_more = bool(data.get("hasMore"))
        min_cursor_new = data.get("minCursor", 0)
        max_cursor_new = data.get("maxCursor", 0)
        log.info(
            "page %d users=%d new=%d total_so_far=%d hasMore=%s minCursor=%s",
            page, len(users), new_count, len(all_users), has_more, min_cursor_new,
        )
        if not has_more or new_count == 0:
            break
        # advance — TikTok uses minCursor for next page
        if min_cursor_new == min_cursor:
            log.info("cursor did not advance, stopping")
            break
        min_cursor = min_cursor_new
        max_cursor = max_cursor_new
        time.sleep(0.7)

    return all_users


async def insert_targets(users: Iterable[dict]):
    import asyncpg
    db_url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if not db_url:
        host = os.getenv("POSTGRES_HOST", "postgres")
        port = os.getenv("POSTGRES_PORT", "5432")
        user = os.getenv("POSTGRES_USER", "collector")
        pw = os.getenv("POSTGRES_PASSWORD", "")
        db = os.getenv("POSTGRES_DB", "unifiedcollector")
        db_url = f"postgresql://{user}:{pw}@{host}:{port}/{db}"
    log.info("connecting to db host=%s", db_url.split("@")[-1].split("/")[0])
    conn = await asyncpg.connect(db_url)
    inserted = 0
    skipped = 0
    try:
        for u in users:
            uname = u["username"].lstrip("@")
            if not uname:
                continue
            res = await conn.execute(
                """
                INSERT INTO collection_targets
                    (source, target_id, target_name, target_type, status, priority)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (source, target_id) DO NOTHING
                """,
                "tiktok", uname, u.get("nickname") or uname, "user", "pending", 5,
            )
            if res.endswith(" 1"):
                inserted += 1
            else:
                skipped += 1
    finally:
        await conn.close()
    return inserted, skipped


def _discover_tiktok_cookies() -> str:
    """Find the best per-username cookie file — mirrors
    src/collectors/tiktok/__init__.py::TiktokCollector._discover_cookie_file:
    skip empty/legacy stubs (< 1 KB or the literal tiktok_cookies.txt when
    named siblings exist), pick the largest remaining candidate. Empty
    string if nothing qualifies. Kept as a helper so this script mirrors
    the collector's decision — otherwise the seed script and the collector
    could disagree on which cookie file to use."""
    import glob
    candidates: list[str] = []
    for d in ("/app/credentials/tiktok", "credentials/tiktok"):
        candidates.extend(glob.glob(os.path.join(d, "tiktok_*.txt")))
    if not candidates:
        return ""
    has_named = any(
        os.path.basename(p) != "tiktok_cookies.txt" for p in candidates
    )
    scored: list[tuple[int, str]] = []
    for p in candidates:
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        if size < 1024:
            continue
        if has_named and os.path.basename(p) == "tiktok_cookies.txt":
            continue
        scored.append((size, p))
    if not scored:
        return ""
    scored.sort(key=lambda t: (-t[0], t[1]))
    return scored[0][1]


def main():
    # Prefer the explicit env var; fall back to auto-discovery so the
    # script works after Bryan renames cookies from tiktok_cookies.txt to
    # per-username tiktok_<user>.txt (matches the collector behavior).
    cookies_file = os.getenv("TIKTOK_COOKIES_FILE", "").strip() \
        or _discover_tiktok_cookies()
    if not cookies_file or not os.path.isfile(cookies_file):
        log.error(
            "Cookies file not found. Set TIKTOK_COOKIES_FILE or drop a "
            "credentials/tiktok/tiktok_<username>.txt file (>= 1 KB)."
        )
        sys.exit(2)
    log.info("using cookies from %s", cookies_file)
    cookies = load_cookies(cookies_file)
    if not cookies.get("sessionid"):
        log.error("sessionid not found in cookies file")
        sys.exit(2)
    log.info("Loaded %d cookies", len(cookies))

    info = get_account_info(cookies)
    sec_uid = info.get("sec_user_id")
    user_id = str(info.get("user_id_str") or info.get("user_id") or "")
    screen = info.get("screen_name")
    log.info("Authenticated as %s (user_id=%s)", screen, user_id)
    if not sec_uid:
        log.error("Could not determine sec_user_id")
        sys.exit(2)

    users = fetch_following(cookies, sec_uid)
    log.info("Discovered %d unique following users", len(users))
    if not users:
        log.error("No users returned")
        sys.exit(3)

    for u in users[:10]:
        log.info("  • @%s (%s)", u["username"], u.get("nickname"))

    inserted, skipped = asyncio.run(insert_targets(users))
    log.info("DB insert complete: inserted=%d skipped(existing)=%d total=%d", inserted, skipped, inserted + skipped)


if __name__ == "__main__":
    main()
