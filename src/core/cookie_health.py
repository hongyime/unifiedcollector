"""Active cookie-validity checker -> cookie_status.

The dashboard was showing cookies as 'untested' because only the collector that
happens to USE a cookie persisted its status. This runs a light, paced, live check
of EVERY cookie so the Accounts panel always reflects reality.

Method: load each cookie file, GET a per-platform "logged-in only" URL with
follow_redirects=False. 200 = ok; a redirect to a login/signin page (or 401/403)
= dead. One request per cookie, spaced out.

Instagram is GATED OFF by default (COOKIE_CHECK_INSTAGRAM) — it's the ban-sensitive
account and its own collector already tests + persists as it rotates. The other
platforms (strava/youtube/tiktok) are low-risk to probe gently.
"""
from __future__ import annotations

import asyncio
import glob
import http.cookiejar
import logging
import os
import random
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# platform -> (test_url, session_cookie_names)
# test_url must 200 ONLY when logged in and redirect to login otherwise.
CHECKS = {
    "strava":    ("https://www.strava.com/settings/profile", ("_strava4_session",)),
    "youtube":   ("https://www.youtube.com/account_advanced", ("__Secure-3PSID", "SID", "LOGIN_INFO")),
    "tiktok":    ("https://www.tiktok.com/setting", ("sessionid", "sessionid_ss")),
    "instagram": ("https://www.instagram.com/accounts/edit/", ("sessionid",)),
}
_LOGIN_HINTS = ("/login", "/signin", "/accounts/login", "passport", "/auth")


def _parse_cookies(path: str) -> dict:
    jar = http.cookiejar.MozillaCookieJar()
    try:
        jar.load(path, ignore_discard=True, ignore_expires=True)
    except Exception:
        return {}
    return {c.name: c.value for c in jar}


async def _record(pool, platform: str, account: str, status: str, reason):
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO cookie_status (platform, account, status, reason, checked_at) "
                "VALUES ($1,$2,$3,$4, now()) "
                "ON CONFLICT (platform, account) DO UPDATE SET "
                "status=EXCLUDED.status, reason=EXCLUDED.reason, checked_at=now()",
                platform, account, status, reason)
    except Exception:
        logger.debug("cookie_status write failed for %s/%s", platform, account, exc_info=True)


async def _check_one(client, platform: str, url: str, cookies: dict) -> tuple[str, str | None]:
    """Return (status, reason). ok | dead | error."""
    keys = CHECKS[platform][1]
    if not any(cookies.get(k) for k in keys):
        return "dead", "no session cookie"
    try:
        import httpx  # local import
        jar = httpx.Cookies()
        for k, v in cookies.items():
            jar.set(k, v)
        r = await client.get(url, cookies=jar, follow_redirects=False, timeout=20)
    except Exception as e:
        return "error", f"{type(e).__name__}"
    if r.status_code == 200:
        return "ok", None
    if r.status_code in (301, 302, 303, 307, 308):
        loc = (r.headers.get("location") or "").lower()
        if any(h in loc for h in _LOGIN_HINTS):
            return "dead", "redirected to login"
        return "ok", None  # some non-login redirect; treat as alive
    if r.status_code in (401, 403):
        return "dead", f"HTTP {r.status_code}"
    return "ok", None  # anything else non-fatal — don't false-flag


async def check_all_cookies(pool, *, cred_dir: str | None = None) -> dict:
    """Test every cookie once (paced). Returns {checked, ok, dead}."""
    cred_dir = cred_dir or os.getenv("COLLECTOR_CREDENTIALS_DIR", "/app/credentials")
    delay = (float(os.getenv("COOKIE_CHECK_DELAY_MIN", "3")),
             float(os.getenv("COOKIE_CHECK_DELAY_MAX", "8")))
    ig_enabled = os.getenv("COOKIE_CHECK_INSTAGRAM", "0").lower() in ("1", "true")
    out = {"checked": 0, "ok": 0, "dead": 0}
    import httpx
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    async with httpx.AsyncClient(headers={"User-Agent": ua}) as client:
        for platform, (url, _keys) in CHECKS.items():
            if platform == "instagram" and not ig_enabled:
                continue  # collector-driven (ban-sensitive)
            base = Path(cred_dir) / platform
            for f in sorted(glob.glob(str(base / "*.txt"))):
                if Path(f).name.lower() == "readme.txt":
                    continue
                acct = Path(f).stem
                if acct.startswith(platform + "_"):
                    acct = acct[len(platform) + 1:]
                cookies = _parse_cookies(f)
                status, reason = await _check_one(client, platform, url, cookies)
                if status in ("ok", "dead"):
                    await _record(pool, platform, acct, status, reason)
                    out["checked"] += 1
                    out[status] = out.get(status, 0) + 1
                await asyncio.sleep(random.uniform(*delay))
    logger.info("cookie health: checked %d (ok=%d dead=%d)", out["checked"], out["ok"], out["dead"])
    return out
