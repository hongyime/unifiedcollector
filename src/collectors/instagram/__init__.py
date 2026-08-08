"""Instagram collector (Mode α=Graph/instaloader, Mode β=Playwright fallback).

⚠️ IMPORTANT — SHARED-IP COEXISTENCE RULE
   Instagram, TikTok and Lemon8 MUST NEVER run simultaneously when they
   share the same public IP / proxy egress.  All three platforms are owned
   by Meta-adjacent or ByteDance-adjacent groups that fingerprint requests
   across product lines, and overlapping traffic dramatically raises 429 /
   CAPTCHA / shadow-ban rates.  The scheduler is responsible for serialising
   these three collectors per-egress-IP; this module asserts nothing at
   runtime but assumes the contract holds.

This module is ported from instagramtoolkit/ (~53,746 LOC source).  It is
intentionally a thin port — write-side actions (post/comment/like/follow/DM/
story-reply/bulk-send) are *not* carried over, and CLI/setup/standalone-web
scripts are dropped.  See docs/wave_2_F_*.md for the absorbed/dropped/deferred
ledger.

Port responsibility split:
  • Agent F-A: AUTH + PROFILE side  (this file, sections so marked)
  • Agent F-B: posts + spider + Playwright
"""

import asyncio
import json
import logging
import math
import os
import random
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from src.core.base_collector import BaseCollector
from src.collectors.instagram.parse import (
    parse_browser_cookies as _parse_browser_cookies,
    extract_post_edges_from_payload as _parse_extract_post_edges,
)
from src.core.account_pool import AccountPool, Account, _build_fingerprint
from src.core.file_naming import sanitize_name  # F821 fix: used in account_media_dir
from src.core.human_rate_limiter import HumanLikeRateLimiter, OperationType
from src.core.sliding_window_limiter import SlidingWindowRateLimiter, WindowConfig
from src.core.proximity import refresh_account_proximity_cache
from src.core.profile_photo_tracker import ProfilePhotoTracker
from src.core.raw_archive import report_raw_archive_result
from src.core.rate_limit_events import record_rate_limit_event
from src.core.scrape_pacing import headless_dwell, sleep_before_pre_cooldown_retry
from src.core.vault import (
    VAULT_ROOT,
    assert_media_write_allowed,
    write_atomic_artifact,
    write_raw_payload,
)
from src.core.user_change_tracker import (
    UserChangeTracker,
    INSTAGRAM_TRACKED_FIELDS,
)

# Follow-aware account selector (Phase 0). Records which cookie-account can see
# which target so a later pass can route private targets to an account that
# actually follows them. Defensive import — collection still works without it.
try:  # pragma: no cover
    from src.core.profile_access import ProfileAccessRepository, SmartAccountSelector
except Exception:  # pragma: no cover
    ProfileAccessRepository = None  # type: ignore[assignment]
    SmartAccountSelector = None  # type: ignore[assignment]

# Profile analytics + per-account TLS fingerprint pinning. Both are
# defensive imports — collector still functions without them.
try:  # pragma: no cover
    from src.core.profile_analyzer import ProfileAnalyzer, analyze_profile_image
except Exception:  # pragma: no cover
    ProfileAnalyzer = None  # type: ignore[assignment]
    analyze_profile_image = None  # type: ignore[assignment]

try:  # pragma: no cover
    from src.core.tls_fingerprint import TLSFingerprintRotator
except Exception:  # pragma: no cover
    TLSFingerprintRotator = None  # type: ignore[assignment]

# === AUTH + PROFILE (Agent F-A) — extra core imports =======================
# These are imported lazily inside methods where possible to keep the
# top-of-module surface small and to avoid pulling in optional deps when only
# the post-side path is exercised in unit tests.
try:  # pragma: no cover — import-time only
    from src.core.auth_session import IgSession  # session capsule
except Exception:  # pragma: no cover
    IgSession = None  # type: ignore[assignment]

try:  # pragma: no cover
    from src.core.account_quota import (
        AccountQuotaTracker,
        QuotaExhaustedError,
        get_default_tracker as _get_default_quota_tracker,
    )
except Exception:  # pragma: no cover
    AccountQuotaTracker = None  # type: ignore[assignment]
    QuotaExhaustedError = Exception  # type: ignore[assignment, misc]

    def _get_default_quota_tracker():  # type: ignore[no-redef]
        return None

try:  # pragma: no cover
    from src.core.dedupe_hash import sha256_bytes as _dedupe_sha256
except Exception:  # pragma: no cover
    _dedupe_sha256 = None  # type: ignore[assignment]

# === END AUTH + PROFILE imports ============================================

logger = logging.getLogger(__name__)


class _RateLimitHandled(Exception):
    """Raised by _collect_user after handling a 429 internally.
    Propagates to _process_target's except path without triggering a second
    _handle_rate_limit call, preserving _consecutive_429s."""


# NOTE: web_profile_info is served by the mobile-API host i.instagram.com for
# anonymous/cookie profile fetches. The www.instagram.com/api/v1 host returns
# 403/429 for under-authenticated requests, which was a root cause of immediate
# 429s on the very first request. See collector_audit.md (Subagent-3 findings).
GRAPH_API = "https://i.instagram.com/api/v1"

NIGHT_HOURS = {23, 0, 1, 2, 3, 4, 5, 6}
RISKY_HOURS = {9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19}

MICRO_PAUSE_PROBABILITY = 0.7
MICRO_PAUSE_MIN = 0.5
MICRO_PAUSE_MAX = 3.0

ACCOUNT_SWITCH_DELAY_MIN = 180
ACCOUNT_SWITCH_DELAY_MAX = 300

DAILY_QUOTA_PROFILE_VIEWS = int(os.getenv("INSTA_DAILY_QUOTA_PROFILE_VIEWS", "180"))
DAILY_QUOTA_ACTIONS = int(os.getenv("INSTA_DAILY_QUOTA_ACTIONS", "6000"))

SLIDING_WINDOW_ENABLED = os.getenv("SLIDING_WINDOW_ENABLED", "true").lower() == "true"
CONTENT_AWARE_ENABLED = os.getenv("CONTENT_AWARE_ENABLED", "true").lower() == "true"

CONTENT_DELAYS = {
    "post": 3.0,
    "video": 6.0,
    "carousel": 4.0,
    "story": 2.0,
    "story_video": 2.5,
    "reel": 7.0,
    "highlight": 2.0,
    "highlight_video": 3.0,
    "profile_photo": 1.5,
}

# ---------------------------------------------------------------------------
# Playwright (Mode β) — hybrid fallback when instaloader/Graph endpoints fail.
#
# *** STRICT 1-AT-A-TIME GLOBAL SEMAPHORE ***
# WSL on this host is capped at 6GB and Docker has only 5.79GiB.  Each
# Chromium instance, even with --single-process, eats ~250-400MB.  We MUST NOT
# launch parallel browsers across account workers — doing so will OOM-kill
# the collector container.  Increase only if RAM is bumped.
# ---------------------------------------------------------------------------
PLAYWRIGHT_SEMAPHORE = asyncio.Semaphore(1)
PLAYWRIGHT_LAUNCH_ARGS = [
    "--single-process",       # CRITICAL for low-RAM hosts
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--js-flags=--max-old-space-size=512",
    "--disable-background-timer-throttling",
    "--renderer-process-limit=10",
    "--no-zygote",
]


def _tier1_raw_archives_enabled() -> bool:
    raw = os.getenv("COLLECTOR_TIER1_RAW_PAYLOADS_ENABLED", "1")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


class InstagramCollector(BaseCollector):
    SOURCE_NAME = "instagram"
    USE_HUMAN_RATE_LIMITER = True

    def __init__(self):
        super().__init__()
        self._max_followers = int(os.getenv("FILTER_MAX_FOLLOWERS", "0"))
        self._sem = asyncio.Semaphore(2)

        self.account_pool = AccountPool(
            default_cooldown=900.0,
            error_cooldown=1800.0,
            max_consecutive_errors=3,
        )
        self.account_pool.load_from_env("INSTA", ["NAME", "USER", "PASS"])

        self._session_dir = Path("sessions") / "instagram"
        self._session_dir.mkdir(parents=True, exist_ok=True)

        self._sliding_limiter = SlidingWindowRateLimiter(windows=[
            WindowConfig("1h", 3600, int(os.getenv("INSTA_WINDOW_1H", "180"))),
            WindowConfig("3h", 10800, int(os.getenv("INSTA_WINDOW_3H", "400"))),
            WindowConfig("5h", 18000, int(os.getenv("INSTA_WINDOW_5H", "600"))),
            WindowConfig("1d", 86400, int(os.getenv("INSTA_WINDOW_1D", "2000"))),
        ])
        self._photo_tracker = ProfilePhotoTracker()
        self._session_max_age_days = int(os.getenv("INSTA_SESSION_MAX_AGE_DAYS", "7"))
        self._warmed_up = False
        # Cookie accounts whose IG session returned 401/403 (dead) this process.
        # collect() skips these and rotates to a healthy account. Reset on restart.
        self._dead_cookie_accounts: set[str] = set()
        self._session_auth_dead = False
        self._daily_views: dict[str, int] = {}
        self._daily_actions: dict[str, int] = {}
        self._daily_quota_exhausted_keys: set[str] = set()
        self._daily_quota_warned_keys: set[str] = set()

        proxy_url = os.getenv("PROXY_URL", "")
        from src.core.env import env_bool
        insta_proxy_disabled = env_bool("INSTA_PROXY_DISABLED", default=False)
        self._global_proxy = proxy_url.strip() if (proxy_url and not insta_proxy_disabled) else None
        self._account_proxies: dict[str, str] = {}
        self._account_browser_cookies: dict[str, str] = self._auto_discover_cookies()
        self._account_username_aliases: dict[str, str] = self._load_account_username_aliases()
        self._account_priorities: dict[str, str] = {}
        for i in range(1, 20):
            name = os.getenv(f"INSTA_ACCOUNT_{i}_NAME", "")
            px = os.getenv(f"INSTA_ACCOUNT_{i}_PROXY", "")
            browser = os.getenv(f"INSTA_ACCOUNT_{i}_BROWSER", "")
            priority = os.getenv(f"INSTA_ACCOUNT_{i}_PRIORITY", os.getenv("INSTA_LOGIN_PRIORITY", "cookie"))
            if name:
                self._account_priorities[name] = priority
                if px:
                    self._account_proxies[name] = px.strip()
                if browser:
                    self._account_browser_cookies[name] = browser.strip()

        self._loader = None
        self._current_account = None
        self._consecutive_429s = 0
        self._consecutive_429s_by_account: dict[str, int] = {}
        self._restored_cooldown_accounts: set[str] = set()
        self._graphql_posts_disabled_until = 0.0
        self._graphql_posts_disable_seconds = int(
            os.getenv("INSTA_GRAPHQL_POSTS_DISABLE_SECONDS", "21600")
        )
        self._graphql_posts_disable_on_400 = (
            os.getenv("INSTA_GRAPHQL_POSTS_DISABLE_ON_400", "true").lower() == "true"
        )
        self._playwright_posts_zero_count = 0
        self._playwright_posts_disabled_until = 0.0
        self._playwright_posts_zero_threshold = max(
            1,
            int(os.getenv("INSTA_PLAYWRIGHT_POSTS_ZERO_THRESHOLD", "3")),
        )
        self._playwright_posts_zero_cooldown_seconds = max(
            300,
            int(os.getenv("INSTA_PLAYWRIGHT_POSTS_ZERO_COOLDOWN_SECONDS", "3600")),
        )
        # Follow-aware access tracker (lazy — needs self.pool, created on first use).
        # Records every profile fetch outcome into profile_access_{summary,attempts}
        # so SmartAccountSelector can later route a private target to a cookie
        # account that can actually see it. Enable/disable via INSTA_ACCESS_TRACKING.
        self._access_repo = None
        self._access_tracking = os.getenv("INSTA_ACCESS_TRACKING", "1") == "1"
        # Throttle the owner own-follow-graph scrape (per owner username) so it runs
        # at most once per INSTA_OWN_GRAPH_INTERVAL_HOURS instead of every cycle.
        self._last_own_graph: dict[str, float] = {}

        # Per-account TLS fingerprint rotators. Lazily created in
        # ``_get_tls_rotator`` so the collector still boots when the
        # ``curl_cffi`` extra is missing. Rotation is cooldown-gated and
        # only fires on 403 / 429 — see src/core/tls_fingerprint.py.
        self._tls_rotators: dict[str, "TLSFingerprintRotator"] = {}
        self._tls_cooldown_secs = int(os.getenv("INSTA_TLS_COOLDOWN_SECS", "600"))
        self._tls_pool: list[str] = [
            s.strip() for s in os.getenv(
                "INSTA_TLS_IMPERSONATE_POOL",
                "chrome120,chrome119,safari17_2,edge101",
            ).split(",") if s.strip()
        ]
        # Profile analyzer instance — cheap, stateless. Falls back to
        # None when the import failed (don't break the collector).
        self._profile_analyzer = ProfileAnalyzer() if ProfileAnalyzer else None

    def _archive_raw_payload(
        self,
        *,
        artifact_id: str,
        payload: dict | list,
        target_tables: list[str],
        metadata: dict | None = None,
    ) -> None:
        if not _tier1_raw_archives_enabled():
            return
        try:
            result = write_raw_payload(
                source=self.SOURCE_NAME,
                artifact_id=artifact_id,
                payload=payload,
                metadata=metadata or {},
                target_tables=target_tables,
                root=VAULT_ROOT,
            )
            report_raw_archive_result(
                self.pool,
                source=self.SOURCE_NAME,
                artifact_id=artifact_id,
                result=result,
                metadata=metadata,
                log=logger,
            )
        except Exception as exc:
            logger.debug("instagram raw archive failed for %s: %s", artifact_id, exc)
            report_raw_archive_result(
                self.pool,
                source=self.SOURCE_NAME,
                artifact_id=artifact_id,
                result=None,
                metadata=metadata,
                log=logger,
                error=str(exc),
            )

    def _auto_discover_cookies(self) -> dict:
        """Auto-discover cookie files for all accounts.

        Searches both credentials/instagram/cookies/ (legacy) and
        credentials/instagram/ (current layout). Files are mapped by:
          1. exact account NAME match (e.g. shotsbyseah234.txt -> shotsbyseah234)
          2. account_<N> pattern -> ACCOUNT_<N>
          3. fallback: stem of filename
        """
        import re
        discovered = {}
        cookie_dirs = [
            "credentials/instagram/cookies/",
            "credentials/instagram/",
        ]
        for cookie_dir in cookie_dirs:
            if not os.path.exists(cookie_dir):
                continue
            for filename in os.listdir(cookie_dir):
                full = os.path.join(cookie_dir, filename)
                if not os.path.isfile(full):
                    continue
                if not (filename.endswith('.txt') or filename.endswith('.json')):
                    continue
                match = re.search(r'account_(\d+)', filename)
                if match:
                    account_name = f"ACCOUNT_{match.group(1)}"
                else:
                    account_name = filename.rsplit('.', 1)[0]
                # Don't overwrite if already discovered from the more-specific cookies/ dir
                if account_name in discovered:
                    continue
                discovered[account_name] = full
                logger.info("Auto-discovered cookie file for %s: %s", account_name, full)
        return discovered

    @staticmethod
    def _clean_instagram_username(value: str | None) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        raw = raw.split("?", 1)[0].rstrip("/")
        if "instagram.com/" in raw.lower():
            raw = re.split(r"instagram\.com/", raw, maxsplit=1, flags=re.IGNORECASE)[1].split("/", 1)[0]
        raw = raw.lstrip("@").strip()
        if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", raw):
            return ""
        return raw

    def _load_account_username_aliases(self) -> dict[str, str]:
        """Map local account aliases to real Instagram usernames.

        INSTA_ACCOUNT_N_NAME is allowed to be a short local cookie/session label.
        INSTA_ACCOUNT_N_USER is the real handle. Keep the alias for session state,
        but never scrape the alias as if it were a public profile.
        """
        aliases: dict[str, str] = {}
        for i in range(1, 20):
            alias = self._clean_instagram_username(os.getenv(f"INSTA_ACCOUNT_{i}_NAME", ""))
            username = self._clean_instagram_username(os.getenv(f"INSTA_ACCOUNT_{i}_USER", ""))
            if alias and username and alias.lower() != username.lower():
                aliases[alias.lower()] = username
        return aliases

    def _canonical_instagram_username(self, value: str | None) -> str:
        username = self._clean_instagram_username(value)
        if not username:
            return ""
        return self._account_username_aliases.get(username.lower(), username)

    def _owned_instagram_usernames(self) -> set[str]:
        owners: set[str] = set()
        for name in (self._account_browser_cookies or {}).keys():
            canonical = self._canonical_instagram_username(name)
            if canonical:
                owners.add(canonical.lower())
        for username in (self._account_username_aliases or {}).values():
            canonical = self._canonical_instagram_username(username)
            if canonical:
                owners.add(canonical.lower())
        for i in range(1, 20):
            canonical = self._canonical_instagram_username(os.getenv(f"INSTA_ACCOUNT_{i}_USER", ""))
            if canonical:
                owners.add(canonical.lower())
        return owners

    def _is_owned_instagram_username(self, value: str | None) -> bool:
        canonical = self._canonical_instagram_username(value)
        return bool(canonical and canonical.lower() in self._owned_instagram_usernames())

    def _normalize_instagram_targets(self, targets: list[str] | None) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        remapped = 0
        dropped = 0
        for target in targets or []:
            raw = self._clean_instagram_username(target)
            if not raw:
                dropped += 1
                continue
            canonical = self._canonical_instagram_username(raw)
            if canonical.lower() != raw.lower():
                remapped += 1
            key = canonical.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(canonical)
        if remapped or dropped:
            logger.info(
                "instagram: normalized target list (%d alias remap(s), %d invalid target(s) dropped)",
                remapped,
                dropped,
            )
        return normalized

    def set_pool(self, pool):
        super().set_pool(pool)
        self._photo_tracker.set_pool(pool)

    @staticmethod
    def _rate_limit_cursor_service(account: str | None) -> str:
        account = (account or "").strip()
        return f"instagram_rate_limit:{account}" if account else "instagram_rate_limit"

    @staticmethod
    def _parse_rate_limit_cursor(raw: str | None) -> tuple[float, int]:
        if not raw:
            return 0.0, 0
        try:
            parts = str(raw).split(":", 1)
            expiry = float(parts[0])
            streak = int(float(parts[1])) if len(parts) == 2 and parts[1] else 0
            if expiry <= time.time():
                return 0.0, 0
            return expiry, max(0, streak)
        except (TypeError, ValueError):
            return 0.0, 0

    async def _restore_account_rate_limit_state(self, accounts: list[str] | None = None) -> None:
        """Hydrate active per-Instagram-account cooldowns from durable events/cursors."""
        if self.pool is None or not isinstance(self.rate_limiter, HumanLikeRateLimiter):
            return
        account_set = {str(a).strip() for a in (accounts or []) if str(a).strip()}
        if account_set and account_set.issubset(self._restored_cooldown_accounts):
            return
        where_accounts = list(account_set) if account_set else None
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    WITH active_events AS (
                        SELECT account,
                               MAX(created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second')) AS cooldown_until,
                               MAX(
                                   CASE
                                     WHEN metadata->>'streak' ~ '^[0-9]+$' THEN (metadata->>'streak')::int
                                     ELSE NULL
                                   END
                               ) AS streak
                        FROM rate_limit_events
                        WHERE source = 'instagram'
                          AND status_code = 429
                          AND account IS NOT NULL
                          AND COALESCE(cooldown_seconds, 0) > 0
                          AND created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second') > NOW()
                          AND ($1::text[] IS NULL OR account = ANY($1::text[]))
                        GROUP BY account
                    ),
                    active_cursors AS (
                        SELECT regexp_replace(service, '^instagram_rate_limit:', '') AS account,
                               last_processed_id
                        FROM service_cursors
                        WHERE service LIKE 'instagram_rate_limit:%'
                          AND ($1::text[] IS NULL OR regexp_replace(service, '^instagram_rate_limit:', '') = ANY($1::text[]))
                    )
                    SELECT COALESCE(e.account, c.account) AS account,
                           e.cooldown_until,
                           e.streak,
                           c.last_processed_id
                    FROM active_events e
                    FULL OUTER JOIN active_cursors c ON c.account = e.account
                    """,
                    where_accounts,
                )
        except Exception as exc:
            logger.debug("instagram: failed restoring per-account cooldowns: %s", exc)
            return

        restored = 0
        now_utc = datetime.now(timezone.utc)
        for row in rows:
            account = str(row["account"] or "").strip()
            if not account:
                continue
            event_until = row["cooldown_until"]
            event_remaining = 0.0
            if event_until is not None:
                if event_until.tzinfo is None:
                    event_until = event_until.replace(tzinfo=timezone.utc)
                event_remaining = max(0.0, (event_until - now_utc).total_seconds())
            cursor_expiry, cursor_streak = self._parse_rate_limit_cursor(row["last_processed_id"])
            cursor_remaining = max(0.0, cursor_expiry - time.time()) if cursor_expiry else 0.0
            remaining = max(event_remaining, cursor_remaining)
            if remaining <= 0:
                continue
            self.rate_limiter.set_cooldown_remaining(
                "instagram.com",
                remaining,
                account=account,
            )
            streak = max(int(row["streak"] or 0), cursor_streak)
            if streak:
                self._consecutive_429s_by_account[account] = streak
            restored += 1
            self._restored_cooldown_accounts.add(account)
        if restored:
            logger.info("instagram: restored %d active per-account cooldown(s)", restored)

    def _init_loader(self):
        try:
            import instaloader
            self._loader = instaloader.Instaloader(
                download_pictures=False,
                download_videos=False,
                download_video_thumbnails=False,
                download_geotags=os.getenv("INSTA_DOWNLOAD_GEOTAGS", "true").lower() == "true",
                download_comments=False,
                save_metadata=False,
                compress_json=False,
                quiet=True,
            )
        except ImportError:
            logger.warning("instaloader not installed — falling back to session cookie mode")
            self._loader = None

    def _login_account(self, account) -> bool:
        logger.debug("[DEBUG] _login_account ENTER for %s", account.name)
        if not self._loader:
            self._init_loader()
        if not self._loader:
            logger.debug("[DEBUG] _login_account: no loader, returning False")
            return False

        username = account.credentials.get("user", "")
        password = account.credentials.get("pass", "")
        if not username:
            logger.debug("[DEBUG] _login_account: no username, returning False")
            return False

        priority = self._account_priorities.get(account.name, os.getenv("INSTA_LOGIN_PRIORITY", "cookie"))
        logger.debug("[DEBUG] _login_account: priority=%s for %s", priority, username)

        if priority == "cookie":
            logger.debug("[DEBUG] _login_account: trying cookie login first")
            if self._try_cookie_login(account, username):
                logger.debug("[DEBUG] _login_account: cookie login succeeded, returning True")
                return True
            if password:
                return self._password_login(account, username, password)
            return False
        else:
            if password:
                if self._password_login(account, username, password):
                    return True
            if self._try_cookie_login(account, username):
                return True
            return False

    def _try_cookie_login(self, account, username: str) -> bool:
        if account.name in self._account_browser_cookies:
            cookie_path = self._account_browser_cookies[account.name]
            logger.debug("[DEBUG] _try_cookie_login calling _login_from_cookies for %s", username)
            result = self._login_from_cookies(username, cookie_path)
            logger.debug("[DEBUG] _login_from_cookies returned %s", result)
            if result:
                return True
            logger.info("Cookie login failed for %s", username)
        return False

    def _password_login(self, account, username: str, password: str) -> bool:
        session_file = self._session_dir / f"{username}.session"
        try:
            if session_file.exists() and self._check_session_age(username):
                self._loader.load_session_from_file(username, str(session_file))
                # Skip test_login() — it blocks for minutes on 401 errors.
                # Session file existence + age check is sufficient.
                # self._loader.test_login()
                logger.info("Resumed session for %s", username)
                return True
        except Exception:
            logger.debug("Stale session for %s, re-logging in", username)

        try:
            self._loader.login(username, password)
            self._loader.save_session_to_file(str(session_file))
            self._save_session_meta(username)
            logger.info("Logged in as %s", username)
            return True
        except Exception as e:
            # Detect 2FA challenge from instaloader.
            err_text = str(e).lower()
            cls_name = type(e).__name__
            needs_2fa = (
                cls_name == "TwoFactorAuthRequiredException"
                or "two-factor" in err_text
                or "two_factor" in err_text
                or "2fa" in err_text
            )
            if needs_2fa:
                code = self._resolve_2fa_code(username)
                if code:
                    try:
                        self._loader.two_factor_login(code)
                        self._loader.save_session_to_file(str(session_file))
                        self._save_session_meta(username)
                        logger.info("Logged in (2FA) as %s", username)
                        # Consume the drop-file once used so a stale code can't be reused.
                        self._consume_2fa_dropfile(username)
                        return True
                    except Exception as e2:
                        logger.error("2FA login failed for %s: %s", username, e2)
                        self.account_pool.record_error(account.name)
                        return False
                else:
                    logger.error(
                        "2FA required for %s but no code available. "
                        "Set INSTA_ACCOUNT_<N>_TOTP_SECRET in .env, "
                        "or drop a 6-digit code into credentials/instagram/2fa/%s.code",
                        username, username,
                    )
                    self.account_pool.record_error(account.name)
                    return False
            logger.error("Login failed for %s: %s", username, e)
            self.account_pool.record_error(account.name)
            return False

    def _resolve_2fa_code(self, username: str) -> str:
        """Resolve a 2FA code for `username` from (in order):
          1. INSTA_ACCOUNT_<N>_TOTP_SECRET env (per matching account index)
          2. credentials/instagram/2fa/<username>.code drop-file (one-shot)
        Returns empty string if none available.
        """
        # 1. TOTP env var
        for i in range(1, 20):
            name = os.getenv(f"INSTA_ACCOUNT_{i}_NAME", "")
            if name and name.lower() == username.lower():
                secret = os.getenv(f"INSTA_ACCOUNT_{i}_TOTP_SECRET", "").strip()
                if secret:
                    try:
                        import pyotp
                        return pyotp.TOTP(secret).now()
                    except ImportError:
                        logger.warning("pyotp not installed; cannot use TOTP secret for %s", username)
                    except Exception as e:
                        logger.warning("TOTP generation failed for %s: %s", username, e)
                break
        # 2. Drop-file
        drop = Path("credentials/instagram/2fa") / f"{username}.code"
        if drop.exists():
            try:
                code = drop.read_text(encoding="utf-8").strip()
                # Allow either bare 6 digits or first whitespace-separated token
                code = code.split()[0] if code else ""
                if code.isdigit() and 6 <= len(code) <= 8:
                    logger.info("Using 2FA drop-file code for %s", username)
                    return code
            except Exception as e:
                logger.warning("Failed to read 2FA drop-file for %s: %s", username, e)
        return ""

    def _consume_2fa_dropfile(self, username: str) -> None:
        drop = Path("credentials/instagram/2fa") / f"{username}.code"
        try:
            if drop.exists():
                drop.unlink()
                logger.info("Consumed 2FA drop-file for %s", username)
        except Exception:
            pass

    def _login_from_cookies(self, username: str, cookie_path: str) -> bool:
        if not os.path.exists(cookie_path):
            logger.warning("Cookie file not found: %s", cookie_path)
            return False
        try:
            cookies = self._parse_browser_cookies(cookie_path)
            if not cookies:
                logger.warning("No cookies parsed from %s", cookie_path)
                return False

            required = {"sessionid", "csrftoken"}
            found = required & set(cookies.keys())
            if not found:
                logger.warning("Cookie file missing required keys (sessionid/csrftoken)")
                return False

            session = self._loader.context._session
            for name, value in cookies.items():
                session.cookies.set(name, value, domain=".instagram.com")

            # Skip test_login() — it blocks for minutes on 401 errors.
            # Cookie presence + sessionid validation is sufficient.
            # self._loader.test_login()
            self._save_session_meta(username)
            logger.info("Logged in via browser cookies for %s (%d cookies loaded)",
                        username, len(cookies))
            logger.debug("[DEBUG] _login_from_cookies returning True")
            return True
        except Exception as e:
            logger.debug("Browser cookie login failed for %s: %s", username, e)
            return False

    @staticmethod
    def _parse_browser_cookies(filepath: str) -> dict[str, str]:
        return _parse_browser_cookies(filepath)

    def _load_cookies_for_account(self, account) -> dict[str, str]:
        """Load cookies directly from file, bypassing instaloader entirely."""
        if account.name in self._account_browser_cookies:
            cookie_path = self._account_browser_cookies[account.name]
            if os.path.exists(cookie_path):
                cookies = self._parse_browser_cookies(cookie_path)
                if cookies and "sessionid" in cookies:
                    return cookies
                logger.warning("Cookie file %s missing sessionid", cookie_path)
        return {}

    def _get_session_cookies(self) -> dict[str, str]:
        if self._loader and self._loader.context._session:
            jar = self._loader.context._session.cookies
            return {c.name: c.value for c in jar}
        return {}

    def _headers(self, account=None) -> dict[str, str]:
        ua = self.user_agents.get_for_domain("instagram.com")
        if account and account.fingerprint.get("user_agent"):
            ua = account.fingerprint["user_agent"]
        cookies = self._get_session_cookies()

        device_id = str(uuid.uuid4())
        if account and account.fingerprint.get("device_id"):
            device_id = account.fingerprint["device_id"]

        return {
            "User-Agent": ua,
            "X-IG-App-ID": "936619743392459",
            "X-IG-Device-ID": device_id,
            "X-IG-Android-ID": device_id.replace("-", "")[:16],
            "X-IG-Connection-Speed": f"{random.randint(1200, 8000)}kbps",
            "X-IG-Connection-Type": "WIFI",
            "X-IG-Capabilities": "3brTv10=",
            "X-Instagram-AJAX": "1",
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRFToken": cookies.get("csrftoken", ""),
            "Accept": "*/*",
            "Origin": "https://www.instagram.com",
            "Referer": "https://www.instagram.com/",
        }

    def _get_proxy(self, account=None) -> str | None:
        if account and account.name in self._account_proxies:
            return self._account_proxies[account.name]
        return self._global_proxy

    def _time_of_day_multiplier(self) -> float:
        hour = datetime.now(timezone.utc).hour
        if hour in NIGHT_HOURS:
            return random.uniform(2.5, 4.0)
        if hour in RISKY_HOURS:
            return 1.5
        return 1.0

    def _daily_quota_key(self, account_name: str) -> str:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return f"{account_name}:{today}"

    def _daily_quota_exhausted(self, account_name: str) -> bool:
        exhausted = getattr(self, "_daily_quota_exhausted_keys", set())
        return self._daily_quota_key(account_name) in exhausted

    def _seconds_until_next_utc_day(self) -> float:
        now = datetime.now(timezone.utc)
        elapsed = (
            now.hour * 3600
            + now.minute * 60
            + now.second
            + (now.microsecond / 1_000_000)
        )
        return max(60.0, 86400.0 - elapsed)

    def _mark_daily_quota_exhausted(
        self,
        account_name: str,
        *,
        quota_name: str,
        quota_limit: int,
    ) -> None:
        key = self._daily_quota_key(account_name)
        exhausted = getattr(self, "_daily_quota_exhausted_keys", set())
        exhausted.add(key)
        self._daily_quota_exhausted_keys = exhausted

        remaining = self._seconds_until_next_utc_day()
        cooldown_seconds = int(remaining)
        set_cooldown = getattr(self.rate_limiter, "set_cooldown_remaining", None)
        if callable(set_cooldown):
            set_cooldown("instagram.com", remaining, account=account_name)

        if self.pool is not None:
            try:
                asyncio.get_running_loop()
                asyncio.create_task(
                    self._persist_daily_quota_cooldown(
                        account_name=account_name,
                        quota_name=quota_name,
                        quota_limit=quota_limit,
                        cooldown_seconds=cooldown_seconds,
                    )
                )
            except RuntimeError:
                logger.debug("instagram: no running loop to persist daily quota cooldown")

        warned = getattr(self, "_daily_quota_warned_keys", set())
        warn_key = f"{key}:{quota_name}"
        if warn_key not in warned:
            warned.add(warn_key)
            self._daily_quota_warned_keys = warned
            logger.warning(
                "Daily %s quota (%d) hit for %s; cooling account for %.1fh",
                quota_name.replace("_", " "),
                quota_limit,
                account_name,
                remaining / 3600,
            )

    async def _persist_daily_quota_cooldown(
        self,
        *,
        account_name: str,
        quota_name: str,
        quota_limit: int,
        cooldown_seconds: int,
    ) -> None:
        if self.pool is None:
            return
        expires_at = time.time() + max(0, cooldown_seconds)
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO service_cursors (service, last_processed_id, last_processed_at, status)
                    VALUES ($1, $2, now(), 'blocked')
                    ON CONFLICT (service) DO UPDATE SET
                        last_processed_id = EXCLUDED.last_processed_id,
                        last_processed_at = EXCLUDED.last_processed_at,
                        status = EXCLUDED.status
                    """,
                    self._rate_limit_cursor_service(account_name),
                    f"{expires_at}:{quota_limit}",
                )
        except Exception:
            logger.debug("instagram: failed to persist daily quota cooldown cursor", exc_info=True)
        await self._record_rate_limit_event(
            scope=f"daily_{quota_name}_quota",
            status_code=None,
            cooldown_seconds=max(0, cooldown_seconds),
            reason=f"daily {quota_name} quota hit ({quota_limit})",
            metadata={
                "quota_name": quota_name,
                "quota_limit": quota_limit,
                "cooldown_kind": "local_daily_quota",
            },
        )

    def _check_daily_quota(self, account_name: str) -> bool:
        key = self._daily_quota_key(account_name)
        views = self._daily_views.get(key, 0)
        actions = self._daily_actions.get(key, 0)
        if DAILY_QUOTA_PROFILE_VIEWS and views >= DAILY_QUOTA_PROFILE_VIEWS:
            self._mark_daily_quota_exhausted(
                account_name,
                quota_name="profile_view",
                quota_limit=DAILY_QUOTA_PROFILE_VIEWS,
            )
            return False
        if DAILY_QUOTA_ACTIONS and actions >= DAILY_QUOTA_ACTIONS:
            self._mark_daily_quota_exhausted(
                account_name,
                quota_name="action",
                quota_limit=DAILY_QUOTA_ACTIONS,
            )
            return False
        return True

    def _record_daily_action(self, account_name: str, views: int = 0, actions: int = 1):
        key = self._daily_quota_key(account_name)
        self._daily_views[key] = self._daily_views.get(key, 0) + views
        self._daily_actions[key] = self._daily_actions.get(key, 0) + actions

    async def _micro_pause(self):
        if random.random() < MICRO_PAUSE_PROBABILITY:
            u = max(random.random(), 1e-10)
            mean = (MICRO_PAUSE_MIN + MICRO_PAUSE_MAX) / 2
            pause = max(MICRO_PAUSE_MIN, min(-mean * math.log(u), MICRO_PAUSE_MAX))
            await asyncio.sleep(pause)

    async def _content_aware_delay(self, content_type: str):
        if not CONTENT_AWARE_ENABLED:
            return
        base = CONTENT_DELAYS.get(content_type, 3.0)
        base *= self._time_of_day_multiplier()
        jitter = random.uniform(0.8, 1.3)
        delay = base * jitter
        await asyncio.sleep(delay)

    @property
    def account_media_dir(self) -> Path:
        if self._current_account:
            acc_name = sanitize_name(self._current_account.name)
            path = self.media_dir / f"account_{acc_name}"
        else:
            path = self.media_dir / "default"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _instagram_domain_delay_seconds(self) -> float:
        try:
            getter = getattr(self.rate_limiter, "get_delay", None)
            if getter is None:
                return 0.0
            return max(0.0, float(getter("instagram.com")))
        except Exception:
            return 0.0

    def _target_timeout_seconds(self) -> float:
        """Outer per-target watchdog sized to the current pacing delay.

        The rate limiter may intentionally stretch Instagram waits after 400/429
        pressure. A fixed 120s watchdog then kills targets after a successful
        Playwright profile fetch, because the intentional wait consumed most of
        the budget. Keep the watchdog bounded, but include the current domain
        delay so slow mode remains productive instead of noisy.
        """
        base = float(os.getenv("INSTA_TARGET_TIMEOUT_SECONDS", "180"))
        cap = float(os.getenv("INSTA_TARGET_TIMEOUT_MAX_SECONDS", "360"))
        domain_delay = self._instagram_domain_delay_seconds()
        adaptive = 90.0 + (domain_delay * 2.5)
        return max(30.0, min(cap, max(base, adaptive)))


    async def collect(self, targets: list[str]):
        """Collect Instagram profiles and posts.

        Runs the httpx-based collection logic with per-target timeouts so a
        rate-limited account never blocks the event loop forever.
        """
        import asyncio
        import time as _time

        self._intentional_idle_reason = None
        fresh_extension = await self._fresh_extension_activity()
        if fresh_extension:
            self._intentional_idle_reason = (
                "Instagram browser extension is fresh "
                f"({fresh_extension['events']} events, "
                f"{fresh_extension['observed']} observed, "
                f"{fresh_extension['stored']} stored, latest "
                f"{fresh_extension['latest_at']}; "
                f"{fresh_extension.get('recent_media_stored', 0)} recent media stored, "
                f"recent media latest {fresh_extension.get('recent_media_latest_at')}); "
                "skipped headless profile loop"
            )
            logger.info("instagram: %s", self._intentional_idle_reason)
            return

        targets = self._normalize_instagram_targets(targets)

        # OWN-GRAPH: ensure each logged-in account's OWN profile is in the target set
        # so _collect_own_follow_graph fires (who follows me / who I follow). The graph
        # scrape itself is throttled per-owner below, so this just makes the owner a
        # processed target; owners first, deduped.
        if os.getenv("INSTA_OWN_GRAPH_ENABLED", "true").lower() == "true":
            # Owners = the cookie accounts (this is a cookie/Playwright-auth collector;
            # account_pool._accounts is the env-password list, usually empty).
            _owners = [
                self._canonical_instagram_username(n)
                for n in (self._account_browser_cookies or {}).keys()
                if self._canonical_instagram_username(n)
            ]
            if _owners:
                targets = list(dict.fromkeys(_owners + list(targets or [])))

        # DB-persisted global rate-limit check: survives collector relaunches.
        # last_processed_id format: "{expiry_epoch}:{consecutive_429s}"
        # Older rows with just a float are also accepted for backward compat.
        #
        # SKIP this sleep entirely in Playwright-primary mode: the persisted
        # rate-limit reflects the raw-httpx web_profile_info throttle, which the
        # browser path bypasses. Otherwise a stale streak made instagram sleep up
        # to an hour on every relaunch and collect almost nothing (profiles stale,
        # posts=0). Mirrors the _process_target cooldown-gate bypass.
        _pw_primary = os.getenv("INSTA_PLAYWRIGHT_PRIMARY", "true").lower() == "true"
        if _pw_primary and self.pool is not None:
            # Restore the streak (for backoff bookkeeping) but do NOT sleep.
            try:
                async with self.pool.acquire() as _conn:
                    _row = await _conn.fetchrow(
                        "SELECT last_processed_id FROM service_cursors "
                        "WHERE service = 'instagram_rate_limit'",
                    )
                if _row and _row["last_processed_id"]:
                    _parts = _row["last_processed_id"].split(":", 1)
                    if len(_parts) == 2:
                        self._consecutive_429s = int(_parts[1])
            except Exception:
                pass
        if not _pw_primary and self.pool is not None:
            try:
                async with self.pool.acquire() as _conn:
                    _row = await _conn.fetchrow(
                        "SELECT last_processed_id FROM service_cursors "
                        "WHERE service = 'instagram_rate_limit'",
                    )
                if _row and _row["last_processed_id"]:
                    _raw = _row["last_processed_id"]
                    _parts = _raw.split(":", 1)
                    _expiry = float(_parts[0])
                    # Restore streak so doubling survives relaunches
                    if len(_parts) == 2:
                        self._consecutive_429s = int(_parts[1])
                    _remaining = _expiry - _time.time()
                    if _remaining > 30:
                        logger.info(
                            "instagram: DB-persisted rate limit active (%.0fs remaining, streak=%d) — sleeping",
                            _remaining, self._consecutive_429s,
                        )
                        _sleep_until = _time.time() + min(_remaining, 3600)
                        while _time.time() < _sleep_until and not self._stop.is_set():
                            await asyncio.sleep(min(30, _sleep_until - _time.time()))
                        # After sleeping, clear the DB entry so the next cycle runs normally
                        if not self._stop.is_set():
                            logger.info("instagram: rate-limit sleep done — clearing DB entry")
                            async with self.pool.acquire() as _conn2:
                                await _conn2.execute(
                                    "DELETE FROM service_cursors WHERE service = 'instagram_rate_limit'",
                                )
            except Exception as _e:
                logger.debug("instagram: rate-limit DB check failed: %s", _e)

        # Check if ALL accounts are in emergency cooldown — skip entire cycle
        # rather than burning 120s timeouts per target.
        # Only applies when account_pool has accounts (env-var based); cookie-only
        # setups have empty _accounts and must always proceed.
        if isinstance(self.rate_limiter, HumanLikeRateLimiter):
            accounts = getattr(self.account_pool, '_accounts', [])
            if accounts and all(
                self.rate_limiter.cooldown_remaining_seconds(
                    "instagram.com", account=acct.name
                ) > 30.0
                for acct in accounts
            ):
                min_remaining = min(
                    self.rate_limiter.cooldown_remaining_seconds(
                        "instagram.com", account=a.name
                    ) for a in accounts
                )
                logger.info(
                    "instagram: all %d accounts in cooldown (min %.0fs remaining) — skipping cycle",
                    len(accounts), min_remaining,
                )
                return
        # Iterate targets and collect each one.
        # Pick the first cookie account whose session is NOT known-dead. A 401 on
        # the web_profile_info fetch means that account's IG session expired; we
        # mark it dead (for this process) and rotate to the next configured account
        # rather than spinning on a dead session. NOTE: this uses the EXISTING
        # configured accounts — it does not change/rotate any credential value.
        # Hot-reload cookie files each cycle so newly-added or REFRESHED cookie
        # files are picked up WITHOUT a container restart. A changed cookie file
        # (by content hash) re-enables that account — so refreshing a dead 401
        # account's cookie clears it from the dead set automatically. Unchanged
        # dead accounts stay dead (no wasted re-probe every cycle).
        try:
            import hashlib as _hl
            self._account_browser_cookies = self._auto_discover_cookies()
            self._account_username_aliases = self._load_account_username_aliases()
            _hashes = getattr(self, "_cookie_file_hashes", {})
            for _name, _path in self._account_browser_cookies.items():
                try:
                    with open(_path, "rb") as _f:
                        _h = _hl.md5(_f.read()).hexdigest()
                except Exception:
                    continue
                if _hashes.get(_name) != _h:
                    if _name in self._dead_cookie_accounts:
                        self._dead_cookie_accounts.discard(_name)
                        # Refreshed cookie -> clear the persisted 'dead' flag so the
                        # dashboard stops showing "refresh needed" (re-tested next cycle).
                        await self._record_cookie_status(_name, "unknown", "cookie refreshed")
                        logger.info("instagram: cookie for %s changed — re-enabling account", _name)
                    elif _name not in _hashes:
                        logger.info("instagram: discovered new cookie account: %s", _name)
                    _hashes[_name] = _h
            self._cookie_file_hashes = _hashes
        except Exception as _e:
            logger.debug("instagram: cookie hot-reload failed (using cached): %s", _e)

        cookie_accounts = list(self._account_browser_cookies.keys())
        if not cookie_accounts:
            self._intentional_idle_reason = "no Instagram cookie accounts available"
            logger.warning("instagram: %s — skipping cycle", self._intentional_idle_reason)
            return

        await self._restore_account_rate_limit_state(cookie_accounts)

        _healthy = [a for a in cookie_accounts if a not in self._dead_cookie_accounts]
        if isinstance(self.rate_limiter, HumanLikeRateLimiter):
            cooling = {
                a: self.rate_limiter.cooldown_remaining_seconds("instagram.com", account=a)
                for a in _healthy
            }
            _healthy = [a for a in _healthy if cooling.get(a, 0.0) <= 30.0]
            if not _healthy and cooling:
                min_remaining = min(cooling.values())
                self._intentional_idle_reason = (
                    f"all {len(cooling)} healthy Instagram cookie account(s) are cooling down "
                    f"(min {min_remaining:.0f}s remaining)"
                )
                logger.info(
                    "instagram: %s; skipping cycle instead of probing a flagged account",
                    self._intentional_idle_reason,
                )
                return
        if not _healthy:
            logger.warning(
                "instagram: all %d cookie accounts have dead (401) sessions — "
                "cycle will likely fail until a session is refreshed", len(cookie_accounts),
            )
            _healthy = cookie_accounts  # try anyway
        _quota_healthy = [a for a in _healthy if not self._daily_quota_exhausted(a)]
        if not _quota_healthy:
            self._intentional_idle_reason = (
                f"all {len(_healthy)} healthy Instagram cookie account(s) hit local daily quota"
            )
            logger.info(
                "instagram: %s; skipping cycle until the quota window resets",
                self._intentional_idle_reason,
            )
            return
        _healthy = _quota_healthy
        # Follow-aware routing (Phase 0 step 2): among the healthy accounts, use
        # the one known to access the MOST of this cycle's targets (from the
        # profile_access data). Single-account-per-cycle model, so this picks the
        # best CYCLE account rather than switching per target. Falls back to
        # _healthy[0] when there's no access data yet (prior behaviour preserved).
        acct_name = await self._select_cycle_account(_healthy, targets)
        self._session_auth_dead = False
        # Wire a REAL Account (stable per-account fingerprint: UA + device_id)
        # and set it as the current account BEFORE the first request. Previously
        # this used a throwaway placeholder and left _current_account = None, so
        # _headers()/_warmup()/per-account cooldown were all dead code and the
        # first request went out as a bare 2-header cold call → immediate 429.
        # See collector_audit.md (Subagent-3 findings).
        # MULTI-ACCOUNT follow graph (default OFF): capture each owned account's own
        # followers/following via its own cookies, into follow_edges. Self-throttled
        # per account; runs before the single-account target loop below.
        try:
            await self._collect_all_account_graphs()
        except Exception as _e:
            logger.debug("instagram multi-graph pass failed: %s", _e)

        self._current_account = Account(
            name=acct_name,
            credentials={},
            fingerprint=_build_fingerprint(acct_name),
        )
        cookies = self._load_cookies_for_account(self._current_account)
        if not cookies:
            self._intentional_idle_reason = f"failed to load Instagram cookies for {acct_name}"
            logger.warning("instagram: %s — skipping cycle", self._intentional_idle_reason)
            return

        _streak_before = self._consecutive_429s_by_account.get(
            acct_name,
            getattr(self, "_consecutive_429s", 0),
        )
        # Build the full mobile-API header set tied to this account's fingerprint.
        # csrftoken lives in the browser cookie jar (instaloader loader is None in
        # cookie-only mode), so populate X-CSRFToken from the loaded cookies.
        _ig_headers = self._headers(self._current_account)
        if cookies.get("csrftoken"):
            _ig_headers["X-CSRFToken"] = cookies["csrftoken"]
        retry_with_next_account = False
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            cookies=cookies,
            headers=_ig_headers,
        ) as client:
            # Human-arrival warmup: load instagram.com once before hitting any
            # /api/v1 endpoint so request #1 isn't a cold direct API call.
            # Runs at most once per process (gated by self._warmed_up) and is
            # skippable via INSTA_WARMUP_ENABLED=false.
            try:
                await self._warmup(client)
            except Exception as _e:
                logger.debug("instagram: warmup skipped (non-fatal): %s", _e)
            for target in targets:
                target_timeout = self._target_timeout_seconds()
                try:
                    await asyncio.wait_for(
                        self._process_target(client, target),
                        timeout=target_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "instagram: _process_target timed out for %s (%.0fs; limiter delay %.1fs)",
                        target,
                        target_timeout,
                        self._instagram_domain_delay_seconds(),
                    )
                except Exception as e:
                    logger.error("instagram: unexpected error for %s: %s", target, e, exc_info=True)
                # If this account's session is dead (401), stop wasting the cycle on
                # it. Mark it dead and immediately retry this same cycle with the
                # next healthy cookie account instead of burning a full scheduler
                # interval on one expired session.
                if self._session_auth_dead:
                    self._dead_cookie_accounts.add(acct_name)
                    await self._record_cookie_status(acct_name, "dead", "401 session expired")
                    remaining = [
                        a for a in cookie_accounts
                        if a not in self._dead_cookie_accounts
                        and not self._daily_quota_exhausted(a)
                    ]
                    logger.warning(
                        "instagram: account %s session is dead (401) — marked dead, "
                        "rotating to another account%s. Healthy remaining: %s",
                        acct_name,
                        " in this cycle" if remaining else " next cycle",
                        remaining or "NONE",
                    )
                    retry_with_next_account = bool(remaining)
                    break
                if self._daily_quota_exhausted(acct_name):
                    logger.info(
                        "instagram: account %s daily quota exhausted; ending cycle "
                        "so the next cycle can route around it",
                        acct_name,
                    )
                    break
                # Survived a target without a 401 -> the cookie is healthy. Cheap
                # single-row upsert; the /accounts panel reads the latest.
                elif acct_name:
                    await self._record_cookie_status(acct_name, "ok", None)
                await asyncio.sleep(2)

        if retry_with_next_account and not self._stop.is_set():
            self._session_auth_dead = False
            await self.collect(targets)
            return

        # If the entire cycle completed without triggering any 429s, clear the
        # persisted rate-limit entry so the next cycle starts clean.
        if self._consecutive_429s == 0 and self.pool is not None:
            if _streak_before > 0:
                logger.info(
                    "instagram: clean cycle after streak=%d — clearing DB rate-limit entry for %s",
                    _streak_before,
                    acct_name,
                )
            try:
                async with self.pool.acquire() as _conn:
                    await _conn.execute(
                        "DELETE FROM service_cursors WHERE service = ANY($1::text[])",
                        [
                            self._rate_limit_cursor_service(acct_name),
                            # Legacy global cursor from older builds. Clear only
                            # after a clean cycle so stale global walls do not
                            # keep pausing every IG account.
                            "instagram_rate_limit",
                        ],
                    )
            except Exception as _e:
                logger.warning("instagram: failed to clear rate-limit from DB: %s", _e)

    async def _fresh_extension_activity(self) -> dict | None:
        if os.getenv("INSTA_SKIP_HEADLESS_WHEN_EXTENSION_FRESH", "true").lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return None
        if self.pool is None:
            return None
        try:
            seconds = int(os.getenv("INSTA_EXTENSION_FRESH_SKIP_SECONDS", "7200"))
        except ValueError:
            seconds = 7200
        seconds = max(300, min(seconds, 86400))
        try:
            media_seconds = int(os.getenv("INSTA_EXTENSION_FRESH_MEDIA_SECONDS", "900"))
        except ValueError:
            media_seconds = 900
        media_seconds = max(300, min(media_seconds, seconds))
        require_media = os.getenv("INSTA_EXTENSION_FRESH_REQUIRE_STORED_MEDIA", "true").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        try:
            async with self.pool.acquire() as conn:
                exists = await conn.fetchval(
                    "SELECT to_regclass('public.browser_ingest_events') IS NOT NULL",
                    timeout=5,
                )
                if not exists:
                    return None
                row = await conn.fetchrow(
                    """
                    SELECT max(created_at) AS latest_at,
                           count(*)::int AS events,
                           COALESCE(sum(observed_count), 0)::int AS observed,
                           COALESCE(sum(stored_count), 0)::int AS stored,
                           COALESCE(sum(stored_count) FILTER (
                               WHERE endpoint = 'media'
                                 AND created_at >= now() - ($3::int * interval '1 second')
                           ), 0)::int AS recent_media_stored,
                           max(created_at) FILTER (
                               WHERE endpoint = 'media'
                                 AND stored_count > 0
                                 AND created_at >= now() - ($3::int * interval '1 second')
                           ) AS recent_media_latest_at
                    FROM browser_ingest_events
                    WHERE platform = 'instagram'
                      AND endpoint = ANY($2::text[])
                      AND created_at >= now() - ($1::int * interval '1 second')
                    """,
                    seconds,
                    ["media", "posts", "profile", "comments"],
                    media_seconds,
                    timeout=8,
                )
        except Exception as exc:
            logger.debug("instagram: extension freshness check failed: %s", exc)
            return None
        if not row or not row.get("latest_at") or int(row.get("observed") or 0) <= 0:
            return None
        if require_media and int(row.get("recent_media_stored") or 0) <= 0:
            logger.info(
                "instagram: browser extension had recent events but no stored media "
                "inside %ss; allowing headless profile loop",
                media_seconds,
            )
            return None
        return {
            "latest_at": row["latest_at"],
            "events": int(row.get("events") or 0),
            "observed": int(row.get("observed") or 0),
            "stored": int(row.get("stored") or 0),
            "recent_media_stored": int(row.get("recent_media_stored") or 0),
            "recent_media_latest_at": row.get("recent_media_latest_at"),
        }


    async def _process_target(self, client: httpx.AsyncClient, username: str):
        if self._current_account and not self._check_daily_quota(self._current_account.name):
            return

        # §21 Hard gate: respect per-account emergency cooldown set by 429 responses.
        # Without this the worker keeps entering target work and then sleeps inside
        # rate_limiter.async_wait() until the 120s per-target timeout fires.
        if isinstance(self.rate_limiter, HumanLikeRateLimiter):
            acct_name = self._current_account.name if self._current_account else None
            remaining = self.rate_limiter.cooldown_remaining_seconds(
                "instagram.com", account=acct_name,
            )
            if remaining > 30.0:
                logger.warning(
                    "Skipping instagram/%s — per-account cooldown active for %s (%.0fs remaining)",
                    username, acct_name or "global", remaining,
                )
                return

        if SLIDING_WINDOW_ENABLED and not self._sliding_limiter.check("instagram.com"):
            wait = self._sliding_limiter.time_until_allowed("instagram.com")
            logger.warning("Sliding window limit hit, waiting %.0fs", wait)
            await asyncio.sleep(min(wait, 600))

        logger.info("Collecting instagram/%s", username)
        try:
            await self._collect_user(client, username)
            self._sliding_limiter.record("instagram.com")
            await self.checkpoint.save_progress(username)
            self._consecutive_429s = 0  # reset backoff on success
            if self._current_account:
                self._consecutive_429s_by_account[self._current_account.name] = 0
                self.account_pool.record_success(self._current_account.name)
                self._record_daily_action(self._current_account.name, views=1)
            await self._micro_pause()
        except _RateLimitHandled:
            pass  # already handled inside _collect_user; _consecutive_429s preserved
        except Exception as e:
            if "429" in str(e) or "rate" in str(e).lower():
                await self._handle_rate_limit(e)
            else:
                logger.error("Failed instagram/%s: %s", username, e)
                await self.send_to_dlq(username, username, str(e))

    async def _process_spider_queue(self, client: httpx.AsyncClient):
        """Claim and process jobs from the spider queue."""
        await refresh_account_proximity_cache(self.pool)
        while not self._stop.is_set():
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    UPDATE instagram_spider_queue
                    SET status = 'processing',
                        last_attempt = NOW(),
                        attempts = attempts + 1
                    WHERE id = (
                        SELECT q.id
                        FROM instagram_spider_queue q
                        LEFT JOIN LATERAL (
                            SELECT MIN(ap.tier) AS proximity_tier
                            FROM account_proximity_cache ap
                            WHERE ap.platform = 'instagram'
                              AND (
                                     ap.account_id = q.platform_user_id
                                  OR ap.account_id = lower(q.username)
                              )
                        ) prox ON TRUE
                        WHERE q.status = 'pending' AND q.attempts < 3
                        ORDER BY
                            CASE
                                WHEN prox.proximity_tier IN (1, 2) THEN 2
                                WHEN prox.proximity_tier = 3 THEN 1
                                ELSE 0
                            END DESC,
                            q.priority ASC,
                            q.collected_at ASC
                        LIMIT 1
                    )
                    RETURNING platform_user_id, username
                """)
            
            if not row:
                break
            
            target = row['username'] or row['platform_user_id']
            try:
                await self._process_target(client, target)
                async with self.pool.acquire() as conn:
                    await conn.execute("UPDATE instagram_spider_queue SET status = 'completed' WHERE platform_user_id = $1", row['platform_user_id'])
            except Exception as e:
                logger.error("Spider job failed for %s: %s", target, e)
                async with self.pool.acquire() as conn:
                    await conn.execute("UPDATE instagram_spider_queue SET status = 'failed', error_message = $1 WHERE platform_user_id = $2", str(e), row['platform_user_id'])

    async def _record_rate_limit_event(
        self,
        *,
        scope: str,
        status_code: int,
        cooldown_seconds: int | None = None,
        reason: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        acct_name = self._current_account.name if self._current_account else None
        await record_rate_limit_event(
            self.pool,
            source="instagram",
            account=acct_name,
            scope=scope,
            status_code=status_code,
            cooldown_seconds=cooldown_seconds,
            reason=reason,
            metadata=metadata,
        )

    async def _handle_rate_limit(
        self,
        error,
        *,
        scope: str = "profile_fetch",
        metadata: dict | None = None,
    ):
        # Per-account cooldown (§22): isolate this account's 429 from siblings.
        # Exponential backoff: each consecutive 429 doubles the cooldown up to 4h.
        acct_name = self._current_account.name if self._current_account else None
        import time as _time
        _now = _time.time()
        _existing_expiry = 0.0
        _existing_streak = 0
        _cursor_service = self._rate_limit_cursor_service(acct_name)
        if self.pool is not None:
            try:
                async with self.pool.acquire() as _conn:
                    _row = await _conn.fetchrow(
                        """
                        WITH cursor_state AS (
                            SELECT last_processed_id
                            FROM service_cursors
                            WHERE service = $1
                        ),
                        event_state AS (
                            SELECT
                                MAX(created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second')) AS cooldown_until,
                                MAX(
                                    CASE
                                      WHEN metadata->>'streak' ~ '^[0-9]+$' THEN (metadata->>'streak')::int
                                      ELSE NULL
                                    END
                                ) AS streak
                            FROM rate_limit_events
                            WHERE source = 'instagram'
                              AND status_code = 429
                              AND account IS NOT DISTINCT FROM $2
                              AND COALESCE(cooldown_seconds, 0) > 0
                              AND created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second') > NOW()
                        )
                        SELECT cursor_state.last_processed_id,
                               event_state.cooldown_until,
                               event_state.streak
                        FROM cursor_state
                        FULL OUTER JOIN event_state ON TRUE
                        """,
                        _cursor_service,
                        acct_name,
                    )
                if _row:
                    _cursor_expiry, _cursor_streak = self._parse_rate_limit_cursor(
                        _row["last_processed_id"]
                    )
                    _existing_expiry = max(_existing_expiry, _cursor_expiry)
                    _existing_streak = max(_existing_streak, _cursor_streak)
                    _event_until = _row["cooldown_until"]
                    if _event_until is not None:
                        if _event_until.tzinfo is None:
                            _event_until = _event_until.replace(tzinfo=timezone.utc)
                        _event_expiry = _event_until.timestamp()
                        if _event_expiry > _now:
                            _existing_expiry = max(_existing_expiry, _event_expiry)
                    _existing_streak = max(_existing_streak, int(_row["streak"] or 0))
            except Exception as _e:
                logger.debug("instagram: failed reading existing per-account rate-limit state: %s", _e)
        if not hasattr(self, "_consecutive_429s_by_account"):
            self._consecutive_429s_by_account = {}
        previous_account_streak = self._consecutive_429s_by_account.get(acct_name or "", 0)
        self._consecutive_429s = max(
            previous_account_streak,
            _existing_streak,
        ) + 1
        if acct_name:
            self._consecutive_429s_by_account[acct_name] = self._consecutive_429s
        base_cooldown = 900.0
        cooldown = min(base_cooldown * (2 ** (self._consecutive_429s - 1)), 14400.0)
        _proposed_expiry = _now + cooldown
        _expiry = max(_proposed_expiry, _existing_expiry)
        _effective_cooldown = max(cooldown, _expiry - _now)
        logger.warning(
            "instagram: 429 #%d — emergency cooldown %.0fs (%.1fh)",
            self._consecutive_429s, _effective_cooldown, _effective_cooldown / 3600,
        )
        if isinstance(self.rate_limiter, HumanLikeRateLimiter):
            # Override the default 900s with our exponential value
            old = self.rate_limiter.emergency_cooldown
            self.rate_limiter.emergency_cooldown = _effective_cooldown
            self.rate_limiter.trigger_emergency_cooldown(
                "instagram.com", account=acct_name,
            )
            self.rate_limiter.emergency_cooldown = old

        # Persist cooldown expiry + consecutive 429 count to DB so both survive
        # collector relaunches. Format: "{expiry_epoch}:{streak}" — streak is used
        # to resume exponential backoff on the next relaunch without resetting to 0.
        _streak_val = f"{_expiry}:{self._consecutive_429s}"
        if self.pool is not None:
            try:
                async with self.pool.acquire() as _conn:
                    await _conn.execute(
                        "INSERT INTO service_cursors "
                        "  (service, last_processed_id, last_processed_at, status) "
                        "VALUES ($2, $1, NOW(), 'blocked') "
                        "ON CONFLICT (service) DO UPDATE "
                        "SET last_processed_id = $1, last_processed_at = NOW(), status = 'blocked'",
                        _streak_val,
                        _cursor_service,
                    )
                logger.info(
                    "instagram: persisted per-account rate-limit to DB "
                    "(account=%s expiry=%.0f streak=%d)",
                    acct_name or "unknown",
                    _expiry,
                    self._consecutive_429s,
                )
            except Exception as _e:
                logger.warning("instagram: failed to persist rate-limit to DB: %s", _e)
            event_metadata = dict(metadata or {})
            event_metadata.update({
                "streak": self._consecutive_429s,
                "expiry_epoch": _expiry,
                "proposed_expiry_epoch": _proposed_expiry,
                "previous_expiry_epoch": _existing_expiry or None,
            })
            await self._record_rate_limit_event(
                scope=scope,
                status_code=429,
                cooldown_seconds=int(_effective_cooldown),
                reason=f"429 streak {self._consecutive_429s}",
                metadata=event_metadata,
            )
        else:
            logger.warning("instagram: rate-limit NOT persisted — pool is None")

        # TLS fingerprint rotation: if 429/403 keeps recurring inside the
        # cooldown window, advance this account's curl_cffi impersonate
        # target. Cooldown-gated inside the rotator so a flurry of
        # failures only rotates once.
        try:
            if acct_name and TLSFingerprintRotator is not None:
                rot = self._get_tls_rotator(acct_name)
                if rot is not None:
                    rot.rotate_on_failure(reason=str(error)[:120])
                    pool = getattr(self, "pool", None)
                    if pool is not None:
                        await rot.persist(pool)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("TLS rotator hook failed: %s", e)

        if self._current_account:
            self.account_pool.cooldown(self._current_account.name, float(_effective_cooldown))
            # Only rotate within the env-based account pool. Cookie-only accounts
            # (the collect() path) are NOT pool members, so get_next() would
            # otherwise hand back an unrelated pool account and trigger a spurious
            # instaloader login mid-cycle. Skip rotation for non-pool accounts;
            # the next collect() cycle re-selects the cookie account anyway.
            _pool_names = {a.name for a in getattr(self.account_pool, "_accounts", [])}
            if self._current_account.name in _pool_names:
                next_acct = self.account_pool.get_next(exclude=self._current_account.name)
                if next_acct:
                    logger.info("Switching to account %s after rate limit", next_acct.name)
                    self._current_account = next_acct
                    await asyncio.get_event_loop().run_in_executor(
                        None, self._login_account, next_acct
                    )

    # -- TLS fingerprint pinning helpers ---------------------------------

    def _get_tls_rotator(self, account_name: str):
        """Lazily build a TLSFingerprintRotator for ``account_name``.

        Returns None when curl_cffi / the rotator module is unavailable.
        Caller is responsible for awaiting ``rot.load(pool)`` on first
        use if it cares about prior state.
        """
        if TLSFingerprintRotator is None or not account_name:
            return None
        rot = self._tls_rotators.get(account_name)
        if rot is None:
            try:
                rot = TLSFingerprintRotator(
                    account_id=account_name,
                    available_impersonates=self._tls_pool or None,
                    cooldown_secs=self._tls_cooldown_secs,
                )
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("Could not build TLS rotator for %s: %s", account_name, e)
                return None
            self._tls_rotators[account_name] = rot
        return rot

    def _get_curl_cffi_kwargs(self, account_name: str | None) -> dict:
        """Return ``{"impersonate": "..."}`` (or {}) for the active account.

        Pass to ``curl_cffi.requests.AsyncSession`` / Session at request
        construction time so the same account always presents the same
        TLS / JA3 fingerprint within a session. Empty dict when the
        rotator is unavailable so callers can splat unconditionally.
        """
        if not account_name:
            return {}
        rot = self._get_tls_rotator(account_name)
        if rot is None:
            return {}
        return rot.get_curl_cffi_kwargs()

    async def _record_profile_access(self, username, can_access, user_data=None, error=None):
        """Record which cookie-account could/couldn't see this target into
        profile_access_{summary,attempts} (follow-aware selector, Phase 0).

        Best-effort and fully isolated: any failure here is swallowed so profile
        collection is never affected. No new network calls — it only persists the
        outcome of a fetch we already made.
        """
        if not self._access_tracking or ProfileAccessRepository is None:
            return
        if self.pool is None or self._current_account is None:
            return
        try:
            if self._access_repo is None:
                self._access_repo = ProfileAccessRepository(self.pool)
            is_private = None
            is_followed = False
            if user_data:
                _priv = user_data.get("is_private")
                is_private = bool(_priv) if _priv is not None else None
                is_followed = bool(
                    user_data.get("followed_by_viewer")
                    or user_data.get("follows_viewer")
                    or False
                )
            await self._access_repo.record_attempt(
                source="instagram",
                target_id=str(username),
                account=self._current_account.name,
                can_access=can_access,
                is_public=(None if is_private is None else (not is_private)),
                is_followed=is_followed,
                error=error,
            )
        except Exception as e:
            logger.debug("instagram: access-tracking record failed for %s: %s", username, e)

    async def _select_cycle_account(self, healthy, targets):
        """Follow-aware routing (Phase 0 step 2): pick the healthy cookie account
        known to access the most of this cycle's targets, from profile_access.
        Best-effort; falls back to healthy[0] with no data (prior behaviour)."""
        if not healthy:
            return None
        if len(healthy) == 1 or self.pool is None or not self._access_tracking:
            return healthy[0]
        if ProfileAccessRepository is None or SmartAccountSelector is None:
            return healthy[0]
        try:
            tids = [str(t).lstrip("@") for t in (targets or []) if t]
            if not tids:
                return healthy[0]
            if self._access_repo is None:
                self._access_repo = ProfileAccessRepository(self.pool)
            selector = SmartAccountSelector(self._access_repo)
            counts = {acct: 0 for acct in healthy}
            for tid in tids:
                acct = await selector.select_for_operation("instagram", tid, healthy)
                if acct:
                    counts[acct] = counts.get(acct, 0) + 1
            best, best_n = max(counts.items(), key=lambda item: item[1])
            if best_n > 0:
                logger.info(
                    "instagram: follow-aware routing picked %s (covers %d/%d targets)",
                    best, best_n, len(tids))
            return best
        except Exception as e:
            logger.debug("instagram: follow-aware account selection failed: %s", e)
            return healthy[0]

    async def _collect_user(self, client: httpx.AsyncClient, username: str):
        acct_name = self._current_account.name if self._current_account else None
        await self.rate_limiter.async_wait(
            "instagram.com", OperationType.PROFILE_VIEW, account=acct_name,
        )

        # PLAYWRIGHT-PRIMARY: prioritise the real-browser fetch for maximum success
        # rate. It is slower (~one headless Chromium nav per profile) but bypasses
        # the web_profile_info IP/endpoint throttle that 429s raw httpx. The raw
        # httpx API is used only as a fallback when the browser path returns nothing.
        # Toggle with INSTA_PLAYWRIGHT_PRIMARY=false to restore httpx-first.
        playwright_primary = os.getenv("INSTA_PLAYWRIGHT_PRIMARY", "true").lower() == "true"
        user_data = None
        if playwright_primary:
            user_data = await self._fetch_profile_playwright(username)
            if user_data:
                logger.info("instagram/%s: profile fetched via Playwright (primary)", username)

        httpx_profile_fallback = os.getenv("INSTA_HTTPX_PROFILE_FALLBACK", "true").lower() == "true"
        if not user_data and (httpx_profile_fallback or not playwright_primary):
            # httpx API path: primary when Playwright is disabled, else fallback.
            try:
                resp = await asyncio.wait_for(
                    client.get(
                        f"{GRAPH_API}/users/web_profile_info/",
                        params={"username": username},
                    ),
                    timeout=35.0,
                )
            except asyncio.TimeoutError:
                logger.error("instagram: request timed out for %s", username)
                return
            except Exception as e:
                logger.error("instagram: request failed for %s: %s", username, e)
                return
            if resp.status_code == 404:
                logger.warning("User not found: %s", username)
                return
            if resp.status_code in (401, 403):
                await self._record_rate_limit_event(
                    scope="profile_fetch",
                    status_code=resp.status_code,
                    reason="profile fetch auth/rate response",
                    metadata={"username": username, "endpoint": "web_profile_info"},
                )
            if resp.status_code == 429:
                # If Playwright wasn't the primary path, try it now as a last
                # resort before backing off (browser bypasses the httpx throttle).
                if not playwright_primary:
                    user_data = await self._fetch_profile_playwright(username)
                    if user_data:
                        logger.info(
                            "instagram/%s: profile recovered via Playwright Mode-β after API 429",
                            username,
                        )
                if not user_data:
                    await self._handle_rate_limit(
                        Exception("429"),
                        scope="profile_fetch",
                        metadata={"username": username, "endpoint": "web_profile_info"},
                    )
                    raise _RateLimitHandled("429")
            else:
                resp.raise_for_status()
                profile_response = resp.json()
                self._archive_raw_payload(
                    artifact_id=f"httpx/profiles/{username}/{time.time_ns()}",
                    payload=profile_response,
                    target_tables=["instagram_profiles"],
                    metadata={
                        "payload_type": "instagram_httpx_profile_response",
                        "username": username,
                        "collection_account": acct_name,
                        "request_url": f"{GRAPH_API}/users/web_profile_info/",
                        "http_status": resp.status_code,
                        "ingest_path": "httpx_profile_fetch",
                    },
                )
                user_data = profile_response.get("data", {}).get("user", {})
        elif not user_data and playwright_primary:
            logger.info(
                "instagram/%s: skipped raw web_profile_info fallback; Playwright primary returned no usable profile",
                username,
            )

        if not user_data:
            logger.warning("Empty profile data for %s", username)
            # Access-denied outcome: this account couldn't see the target (private
            # + not following, or blocked). Record so the selector can route to a
            # follower account later.
            await self._record_profile_access(username, False, error="empty profile data")
            return

        uid = user_data.get("id", username)
        entity_name = user_data.get("username", username)

        # Success: this account CAN see the target — record it (with is_private /
        # followed-by-viewer) for the follow-aware selector.
        await self._record_profile_access(username, True, user_data)

        # 1. Save Profile to Database
        await self._upsert_profile(user_data)

        follower_count = user_data.get("edge_followed_by", {}).get("count", 0)
        is_owned_profile = (
            self._is_owned_instagram_username(username)
            or self._is_owned_instagram_username(entity_name)
        )
        if self._max_followers and follower_count > self._max_followers and not is_owned_profile:
            logger.info("Skipping %s: %d followers > max %d", username, follower_count, self._max_followers)
            return
        if self._max_followers and follower_count > self._max_followers and is_owned_profile:
            logger.info(
                "instagram/%s: bypassing follower cap for owned/root account (%d followers > max %d)",
                username,
                follower_count,
                self._max_followers,
            )

        self.rate_limiter.record_success("instagram.com")

        # 2. Handle Profile Photo
        profile_pic = user_data.get("profile_pic_url_hd") or user_data.get("profile_pic_url")
        if profile_pic:
            dest_dir = self.account_media_dir / "profiles"
            dest_dir.mkdir(parents=True, exist_ok=True)
            changed, path = await self._photo_tracker.check_and_download(
                profile_pic, uid, "instagram", dest_dir,
            )
            if changed and path:
                data = path.read_bytes()
                metadata = {"raw": user_data}
                artifact_meta = self._photo_tracker.last_artifact_metadata()
                if artifact_meta:
                    metadata["vault_artifact"] = artifact_meta
                await self.insert_media_item(
                    entity_id=uid,
                    entity_name=entity_name,
                    content_type="profile_photo",
                    content_id=f"profile_{uid}",
                    filename=path.name,
                    file_path=str(path),
                    file_size=len(data),
                    sha256=self.sha256_bytes(data),
                    metadata=metadata,
                )

        # 3. Spidering (if enabled)
        if os.getenv("INSTA_SPIDER_FOLLOWERS", "true").lower() == "true":
            await self._spider_followers(client, uid, entity_name)

        # 3b. Owner's OWN follow graph -> social_users (who follows me / who I follow),
        # captured when the collector processes its own profile. Bounded + paced;
        # gated separately from the disabled discovery spider because scraping your
        # own graph is a normal user action, not novel-profile probing.
        if (os.getenv("INSTA_OWN_GRAPH_ENABLED", "true").lower() == "true"
                and self._is_owned_instagram_username(username)):
            import time as _t
            interval = float(os.getenv("INSTA_OWN_GRAPH_INTERVAL_HOURS", "24")) * 3600
            key = username.lower()
            if _t.time() - self._last_own_graph.get(key, 0) >= interval:
                self._last_own_graph[key] = _t.time()
                await self._collect_own_follow_graph(uid, entity_name)

        # 4. Collect Content
        # Fast path: the web_profile_info response already contains the first
        # page of posts in edge_owner_to_timeline_media. Upsert them now so we
        # get at least partial data even if the paginated paths below fail.
        initial_edges = (user_data.get("edge_owner_to_timeline_media", {})
                         .get("edges", []))
        if initial_edges:
            _saved = 0
            for edge in initial_edges:
                node = edge.get("node", {})
                if node and node.get("shortcode"):
                    try:
                        # _process_post upserts the row AND downloads the media
                        # (display_url/video_url/sidecar children) — previously this
                        # only called _upsert_post, so post media was never fetched
                        # (instagram had 0 post media, only profile photos).
                        await self._process_post(node, uid, entity_name)
                        _saved += 1
                    except Exception as e:
                        logger.debug("instagram/%s: process_post failed for %s: %s",
                                     entity_name, node.get("shortcode"), e)
            logger.info("instagram/%s: processed %d/%d posts from profile response (with media)",
                        entity_name, _saved, len(initial_edges))

        # Paginated post enumeration: GraphQL (Mode α) → instaloader (Mode γ)
        # → Playwright (Mode β). Each returns True on success.
        posts_ok = False
        try:
            posts_ok = await self._collect_posts(client, uid, entity_name)
        except Exception as e:
            logger.warning(
                "instagram/%s: Graph post enumeration raised %s — Playwright fallback",
                entity_name, type(e).__name__,
            )
            posts_ok = False

        if not posts_ok:
            try:
                posts_ok = await self._collect_posts_instaloader(uid, entity_name)
            except Exception as e:
                logger.warning(
                    "instagram/%s: instaloader post fallback raised %s",
                    entity_name, type(e).__name__,
                )
                posts_ok = False

        if not posts_ok:
            try:
                await self._collect_posts_playwright(uid, entity_name)
            except Exception as e:
                logger.warning(
                    "instagram/%s: Playwright fallback failed: %s", entity_name, e,
                )

        await self._collect_stories(uid, entity_name)
        await self._collect_highlights(client, uid, entity_name)

        # Profile analytics on a single-profile batch — cheap, generates
        # logging-friendly stats and a slot to feed into dashboards. Wrap
        # in try/except so a malformed user payload never breaks a run.
        try:
            if self._profile_analyzer is not None:
                pstats = self._profile_analyzer.analyze_profiles([{
                    "username": entity_name,
                    "followers_count": user_data.get("edge_followed_by", {}).get("count", 0),
                    "following_count": user_data.get("edge_follow", {}).get("count", 0),
                    "is_verified": bool(user_data.get("is_verified", False)),
                    "is_private": bool(user_data.get("is_private", False)),
                }])
                tier_hits = [t for t, c in pstats.get("influencer_tiers", {}).items() if c]
                logger.debug(
                    "instagram/%s: profile_analyzer tiers=%s ratio=%.2f",
                    entity_name, tier_hits or ["none"],
                    pstats.get("avg_follower_to_following_ratio", 0.0),
                )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("profile_analyzer post-step failed for %s: %s", entity_name, e)

    async def _upsert_profile(self, data: dict):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO instagram_profiles (
                    platform_user_id, username, full_name, bio,
                    followers_count, following_count, posts_count,
                    is_verified, is_private, profile_pic_url,
                    external_url, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
                ON CONFLICT (platform_user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name,
                    bio = EXCLUDED.bio,
                    followers_count = EXCLUDED.followers_count,
                    following_count = EXCLUDED.following_count,
                    posts_count = EXCLUDED.posts_count,
                    is_verified = EXCLUDED.is_verified,
                    is_private = EXCLUDED.is_private,
                    profile_pic_url = EXCLUDED.profile_pic_url,
                    external_url = EXCLUDED.external_url,
                    updated_at = NOW()
            """, 
            data.get("id"), data.get("username"), data.get("full_name"), data.get("biography"),
            data.get("edge_followed_by", {}).get("count", 0),
            data.get("edge_follow", {}).get("count", 0),
            data.get("edge_owner_to_timeline_media", {}).get("count", 0),
            data.get("is_verified", False), data.get("is_private", False),
            data.get("profile_pic_url_hd"), data.get("external_url")
            )
        self._archive_raw_payload(
            artifact_id=f"profiles/{data.get('id') or data.get('username') or 'unknown'}/{time.time_ns()}",
            payload=data,
            target_tables=["instagram_profiles"],
            metadata={
                "payload_type": "instagram_profile",
                "platform_user_id": data.get("id"),
                "username": data.get("username"),
                "collection_account": self._current_account.name if self._current_account else None,
                "ingest_path": self.INGEST_PATH,
            },
        )

    # _spider_followers is now defined in the F-B section below — wired to
    # src/core/spider_discover.SpiderDiscover with read-only follower BFS.

    async def _collect_posts(self, client: httpx.AsyncClient, uid: str, entity_name: str) -> bool:
        """Try to enumerate posts via the GraphQL endpoint.

        Returns True on success (at least one page processed cleanly), False if
        the endpoint signals auth/rate failure (401/429) or returns empty —
        signal to caller to invoke the Playwright fallback.
        """
        if os.getenv("INSTA_GRAPHQL_POSTS_ENABLED", "true").lower() != "true":
            return False
        disabled_until = float(getattr(self, "_graphql_posts_disabled_until", 0.0) or 0.0)
        now = time.time()
        if disabled_until > now:
            logger.info(
                "instagram/%s: GraphQL posts disabled for %.0fs after prior HTTP 400",
                entity_name,
                disabled_until - now,
            )
            return False

        end_cursor = ""
        has_next = True
        page_depth = 0
        any_success = False

        while has_next and not self._stop.is_set():
            await self.rate_limiter.async_wait(
                "instagram.com", OperationType.PAGINATION, pagination_depth=page_depth,
                account=(self._current_account.name if self._current_account else None),
            )

            params = {
                "query_hash": "472f257a40c653c64c666ce877d59d2b",
                "variables": json.dumps({
                    "id": uid,
                    "first": 12,
                    "after": end_cursor,
                }),
            }

            async with self._sem:
                try:
                    resp = await client.get(
                        "https://www.instagram.com/graphql/query/",
                        params=params,
                    )
                    if resp.status_code in (401, 403):
                        await self._record_rate_limit_event(
                            scope="graphql_posts",
                            status_code=resp.status_code,
                            reason="GraphQL auth/rate response",
                            metadata={
                                "username": entity_name,
                                "uid": uid,
                                "endpoint": "graphql/query",
                            },
                        )
                        logger.info(
                            "instagram/%s: GraphQL %s — signalling Playwright fallback",
                            entity_name, resp.status_code,
                        )
                        return False
                    if resp.status_code == 429:
                        await self._handle_rate_limit(
                            Exception("429"),
                            scope="graphql_posts",
                            metadata={
                                "username": entity_name,
                                "uid": uid,
                                "endpoint": "graphql/query",
                            },
                        )
                        return False
                    if resp.status_code == 400 and getattr(self, "_graphql_posts_disable_on_400", True):
                        disable_seconds = int(getattr(self, "_graphql_posts_disable_seconds", 21600) or 21600)
                        self._graphql_posts_disabled_until = time.time() + disable_seconds
                        logger.warning(
                            "instagram/%s: GraphQL posts returned HTTP 400; disabling legacy GraphQL path for %ds",
                            entity_name,
                            disable_seconds,
                        )
                        return False
                    resp.raise_for_status()
                except Exception as e:
                    self.rate_limiter.record_failure("instagram.com")
                    self.circuit_breaker.record_failure()
                    logger.error("GraphQL request failed: %s", e)
                    return any_success

            data = resp.json()
            self._archive_raw_payload(
                artifact_id=f"graphql/posts/{uid}/page_{page_depth}/{time.time_ns()}",
                payload=data,
                target_tables=["instagram_posts"],
                metadata={
                    "payload_type": "instagram_graphql_posts_page",
                    "platform_user_id": uid,
                    "username": entity_name,
                    "page_depth": page_depth,
                    "end_cursor": end_cursor,
                    "request_url": "https://www.instagram.com/graphql/query/",
                    "query_hash": params["query_hash"],
                    "collection_account": self._current_account.name if self._current_account else None,
                    "ingest_path": "httpx_graphql",
                },
            )
            media_data = (data.get("data", {})
                          .get("user", {})
                          .get("edge_owner_to_timeline_media", {}))
            edges = media_data.get("edges", [])
            page_info = media_data.get("page_info", {})
            has_next = page_info.get("has_next_page", False)
            end_cursor = page_info.get("end_cursor", "")

            self.rate_limiter.record_success("instagram.com")
            self.circuit_breaker.record_success()
            page_depth += 1

            if edges:
                any_success = True

            for edge in edges:
                if self._stop.is_set():
                    break
                node = edge.get("node", {})
                await self._process_post(node, uid, entity_name)

        return any_success

    # ------------------------------------------------------------------
    # Playwright (Mode β) hybrid fallback
    # ------------------------------------------------------------------
    @staticmethod
    def _sanitize_playwright_cookie(raw: dict) -> dict | None:
        """Coerce a raw cookie dict into a Chromium-CDP-safe shape.

        Chromium's ``Storage.setCookies`` is *atomic* — a single malformed
        cookie in the batch rejects the entire ``new_context(storage_state=)``
        call with ``Protocol error (Storage.setCookies): Invalid cookie fields``.
        Cookie files harvested from real browsers routinely include entries
        that trip CDP validation:

        * empty ``name`` or ``value`` (rejected by CDP even though Netscape
          allows them);
        * ``expires`` as a float / string / far-future overflow / ``0`` for
          session cookies (CDP wants int seconds, and ``-1`` for session);
        * Netscape octal-escaped values like ``"VLL\\054<id>\\054..."`` with
          literal surrounding quotes and ``\\NNN`` sequences (Instagram's
          ``rur`` cookie is the canonical offender);
        * ``sameSite`` in a form CDP rejects — Chrome JSON exports use
          ``"no_restriction"``/``"unspecified"``/``"lax"``, but CDP only
          accepts the exact literals ``Lax``/``Strict``/``None``;
        * ``sameSite=None`` without ``secure=True`` (Chromium hard-rejects).

        Returns the sanitized cookie dict, or ``None`` if the entry is
        unsalvageable and should be dropped.
        """
        name = (raw.get("name") or "").strip()
        value = raw.get("value")
        if value is None:
            return None
        value = str(value)
        if not name or not value:
            return None

        # Netscape's cookies.txt uses octal escapes (\NNN) for chars that
        # can't appear raw in the tab-separated format — most often the
        # comma inside Instagram's ``rur``. Decode them, then strip the
        # literal surrounding double-quotes Chrome adds when a value
        # contains such special chars. Fall back to raw value on error.
        if "\\" in value:
            try:
                value = re.sub(
                    r"\\([0-3][0-7]{2})",
                    lambda m: chr(int(m.group(1), 8)),
                    value,
                )
            except Exception:
                pass
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if not value:
            return None

        domain = (raw.get("domain") or "").strip()
        if not domain:
            return None
        # CDP is fine with either leading-dot ("host-only=false") or bare
        # host, but requires *some* dot-separated TLD. Skip obviously
        # broken domains (IP-literals, no dots at all).
        bare = domain.lstrip(".")
        if "." not in bare or bare.replace(".", "").isdigit():
            return None

        path = raw.get("path") or "/"
        if not isinstance(path, str) or not path.startswith("/"):
            path = "/"

        # expires: int seconds since epoch; ``-1`` for session cookies.
        # Netscape ``0`` and JSON ``expirationDate=0`` both mean session.
        expires_raw = raw.get("expires", raw.get("expirationDate"))
        try:
            expires = int(float(expires_raw)) if expires_raw not in (None, "") else -1
        except (TypeError, ValueError):
            expires = -1
        if expires <= 0:
            expires = -1
        # Clamp to a safe upper bound — Chromium caps at ~400 days from
        # now anyway, and values approaching 2**53 have caused overflow
        # rejects in older Playwright/Chromium builds. 4102444800 = 2100-01-01.
        elif expires > 4102444800:
            expires = 4102444800

        # sameSite: normalize any casing / Chrome-export synonym to the
        # three literals CDP accepts. Anything else → "Lax".
        same_site_raw = str(raw.get("sameSite") or "").strip().lower()
        same_site_map = {
            "lax": "Lax",
            "strict": "Strict",
            "none": "None",
            "no_restriction": "None",  # Chrome extension export
            "unspecified": "Lax",       # Chrome extension export
            "": "Lax",
        }
        same_site = same_site_map.get(same_site_raw, "Lax")

        secure = bool(raw.get("secure", False))
        # Chromium hard-rejects sameSite=None without secure=True.
        if same_site == "None":
            secure = True

        return {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
            "expires": expires,
            "httpOnly": bool(raw.get("httpOnly", False)),
            "secure": secure,
            "sameSite": same_site,
        }

    def _build_playwright_storage_state(self, account_name: str) -> dict | None:
        """Convert the per-account Netscape cookie file to Playwright storage_state.

        Returns None if no usable cookie file exists. Every cookie is passed
        through :meth:`_sanitize_playwright_cookie` because Chromium's
        ``Storage.setCookies`` is atomic — one malformed entry aborts the
        whole ``new_context`` call with ``Invalid cookie fields``.
        """
        cookie_path = self._account_browser_cookies.get(account_name)
        if not cookie_path or not os.path.exists(cookie_path):
            return None

        raw_cookies: list[dict] = []
        try:
            # Support both Netscape .txt and JSON format
            if cookie_path.endswith(".json"):
                try:
                    raw = json.loads(Path(cookie_path).read_text(encoding="utf-8"))
                except Exception:
                    return None
                if isinstance(raw, list):
                    for c in raw:
                        if not isinstance(c, dict):
                            continue
                        raw_cookies.append({
                            "name": c.get("name"),
                            "value": c.get("value"),
                            "domain": c.get("domain", ".instagram.com"),
                            "path": c.get("path", "/"),
                            "expires": c.get("expirationDate", c.get("expires")),
                            "httpOnly": c.get("httpOnly", False),
                            "secure": c.get("secure", True),
                            "sameSite": c.get("sameSite"),
                        })
            else:
                with open(cookie_path, "r", encoding="utf-8") as f:
                    for line in f:
                        # NB: don't strip trailing tabs here — an empty value
                        # is a legitimate 7th field. Only strip line endings.
                        line = line.rstrip("\r\n")
                        if not line or line.startswith("#"):
                            continue
                        parts = line.split("\t")
                        if len(parts) < 7:
                            continue
                        domain, _flag, path, secure, expires, name, value = parts[:7]
                        raw_cookies.append({
                            "name": name,
                            "value": value,
                            "domain": domain if (domain.startswith(".") or "." in domain) else f".{domain}",
                            "path": path or "/",
                            "expires": expires,
                            "httpOnly": False,
                            "secure": secure.upper() == "TRUE",
                            "sameSite": "Lax",
                        })
        except Exception as e:
            logger.warning("Failed to parse cookies for Playwright (%s): %s", account_name, e)
            return None

        cookies: list[dict] = []
        dropped = 0
        for raw in raw_cookies:
            clean = self._sanitize_playwright_cookie(raw)
            if clean is None:
                dropped += 1
                continue
            cookies.append(clean)

        if dropped:
            logger.debug(
                "Playwright storage_state: dropped %d/%d malformed cookies for %s",
                dropped, len(raw_cookies), account_name,
            )

        if not cookies:
            return None
        return {"cookies": cookies, "origins": []}

    async def _collect_posts_instaloader(self, uid: str, entity_name: str) -> bool:
        """Mode γ: enumerate posts via instaloader's Profile.get_posts().

        Used when the GraphQL query_hash path (Mode α) fails. Instaloader uses
        its own authenticated session so it works independently of the httpx
        client or browser cookies. All blocking I/O is wrapped in run_in_executor
        so the event loop is never blocked.

        Returns True if at least one post was upserted.
        """
        if not self._loader:
            logger.debug("instagram/%s: instaloader not initialised, skipping Mode γ", entity_name)
            return False

        max_posts = int(os.getenv("INSTA_MAX_POSTS_PER_USER", "100"))

        try:
            import instaloader
            loop = asyncio.get_event_loop()

            # Fetch all post nodes synchronously inside the executor so we don't
            # block the event loop on network I/O (each page fetch is blocking).
            def _fetch_posts_sync() -> list[dict]:
                nodes: list[dict] = []
                try:
                    try:
                        profile = instaloader.Profile.from_id(self._loader.context, int(uid))
                    except Exception:
                        profile = instaloader.Profile.from_username(self._loader.context, entity_name)
                    for post in profile.get_posts():
                        if len(nodes) >= max_posts:
                            break
                        loc = getattr(post, "location", None)
                        nodes.append({
                            "shortcode": getattr(post, "shortcode", None),
                            "__typename": getattr(post, "typename", "GraphImage"),
                            "is_video": bool(getattr(post, "is_video", False)),
                            "display_url": getattr(post, "url", None),
                            "video_url": getattr(post, "video_url", None),
                            "taken_at_timestamp": int(
                                getattr(post, "date_utc", __import__("datetime").datetime.utcnow()).timestamp()
                            ),
                            "edge_media_preview_like": {"count": getattr(post, "likes", 0) or 0},
                            "edge_media_to_comment": {"count": getattr(post, "comments", 0) or 0},
                            "edge_media_to_caption": {
                                "edges": [{"node": {"text": getattr(post, "caption", "") or ""}}]
                            },
                            "location": {
                                "name": getattr(loc, "name", None),
                                "lat": getattr(loc, "lat", None),
                                "lng": getattr(loc, "lng", None),
                            } if loc else None,
                            "raw": post._asdict() if hasattr(post, "_asdict") else {},
                        })
                except Exception as e:
                    logger.debug("instaloader post fetch for %s failed: %s", entity_name, e)
                return nodes

            nodes = await loop.run_in_executor(None, _fetch_posts_sync)
        except Exception as e:
            logger.warning("instagram/%s: instaloader executor failed: %s", entity_name, e)
            return False

        if not nodes:
            logger.info("instagram/%s: instaloader returned 0 posts", entity_name)
            return False

        upserted = 0
        for node in nodes:
            if self._stop.is_set():
                break
            if not node.get("shortcode"):
                continue
            try:
                await self._upsert_post(node, uid)
                upserted += 1
            except Exception:
                # WARNING (was debug): a swallowed upsert is silent data loss —
                # exactly how the posts=0 jsonb bug hid for so long. Make it visible.
                logger.warning("instagram/%s: post upsert FAILED for %s",
                               entity_name, node.get("shortcode"), exc_info=True)

        logger.info("instagram/%s: instaloader Mode γ upserted %d/%d posts",
                    entity_name, upserted, len(nodes))
        return upserted > 0

    async def _fetch_profile_playwright(self, username: str) -> dict | None:
        """Mode-β PROFILE fetch: when the web_profile_info API is IP-throttled for
        raw httpx, render the profile in a real headless Chromium and fetch the
        same JSON via an in-page same-origin fetch. The browser carries the
        account's cookies (storage_state), a consistent fingerprint, and a real
        referer chain, which IG's edge frequently serves even when raw httpx 429s.

        Returns the ``user`` dict (same shape as the API ``data.user`` payload) or
        None on any failure. Concurrency: STRICT 1-at-a-time via
        PLAYWRIGHT_SEMAPHORE — do NOT bypass it (OOM guard, see semaphore comment).
        """
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except ImportError:
            logger.warning(
                "Playwright not installed — cannot run Mode-β profile fallback for %s",
                username,
            )
            return None

        acct_name = self._current_account.name if self._current_account else None
        storage_state = self._build_playwright_storage_state(acct_name) if acct_name else None
        ua = self.user_agents.get_for_domain("instagram.com")
        if self._current_account and self._current_account.fingerprint.get("user_agent"):
            ua = self._current_account.fingerprint["user_agent"]

        profile_url = f"https://www.instagram.com/{username}/"

        async with PLAYWRIGHT_SEMAPHORE:
            logger.info(
                "Playwright Mode-β: fetching profile instagram/%s (account=%s)",
                username, acct_name or "anonymous",
            )
            playwright_ctx = await async_playwright().start()
            browser = None
            try:
                browser = await playwright_ctx.chromium.launch(
                    headless=True,
                    args=PLAYWRIGHT_LAUNCH_ARGS,
                )
                context_kwargs: dict = {
                    "user_agent": ua,
                    "viewport": {"width": 1280, "height": 800},
                    "locale": "en-US",
                }
                if storage_state:
                    context_kwargs["storage_state"] = storage_state
                context = await browser.new_context(**context_kwargs)
                page = await context.new_page()

                # Load the profile page first to establish a real session + referer
                # chain before the API fetch (humans land on the page, not the API).
                # wait_until="commit" (not domcontentloaded) returns as soon as the
                # navigation commits — IG's heavy JS otherwise never settles the DOM
                # and goto hangs to the 45s timeout. We only need origin+cookies for
                # the same-origin fetch below, which "commit" already gives us.
                try:
                    await headless_dwell("instagram profile goto")
                    await page.goto(profile_url, wait_until="commit", timeout=30000)
                    await headless_dwell("instagram profile api fetch")
                except Exception as e:
                    logger.warning("Playwright Mode-β goto failed for %s: %s", username, e)
                    return None

                # Same-origin in-page fetch: executes inside the page's JS context
                # with the browser's own cookies, TLS fingerprint and headers.
                async def _evaluate_profile_info() -> dict:
                    return await page.evaluate(
                        """async (uname) => {
                            try {
                                const r = await fetch(
                                    '/api/v1/users/web_profile_info/?username=' + encodeURIComponent(uname),
                                    { headers: { 'x-ig-app-id': '936619743392459' },
                                      credentials: 'include' });
                                return { status: r.status, body: await r.text() };
                            } catch (e) { return { status: -1, body: String(e) }; }
                        }""",
                        username,
                    )

                try:
                    result = await _evaluate_profile_info()
                except Exception as e:
                    logger.warning("Playwright Mode-β evaluate failed for %s: %s", username, e)
                    return None

                status = result.get("status") if isinstance(result, dict) else None
                if status == 429:
                    retry_delay = await sleep_before_pre_cooldown_retry(
                        "instagram",
                        "profile_fetch_playwright",
                        account=acct_name,
                        status_code=429,
                        reason=f"username={username}",
                    )
                    if retry_delay is not None:
                        try:
                            retry_result = await _evaluate_profile_info()
                        except Exception as e:
                            logger.warning(
                                "Playwright Mode-β retry evaluate failed for %s: %s",
                                username,
                                e,
                            )
                            retry_result = None
                        retry_status = (
                            retry_result.get("status")
                            if isinstance(retry_result, dict)
                            else None
                        )
                        if retry_result is not None:
                            if retry_status != 429:
                                await self._record_rate_limit_event(
                                    scope="profile_fetch_playwright",
                                    status_code=429,
                                    reason="Playwright profile transient rate-limit retried",
                                    metadata={
                                        "username": username,
                                        "endpoint": "web_profile_info",
                                        "pre_cooldown_retry": True,
                                        "retry_status_code": retry_status,
                                        "retry_delay_seconds": retry_delay,
                                    },
                                )
                            result = retry_result
                            status = retry_status

                if status in (401, 403, 429):
                    # Dead/expired sessions are 401/403. A 429 is a throttle signal:
                    # record it, but do not mark the cookie dead.
                    is_auth_failure = status in (401, 403)
                    if is_auth_failure:
                        self._session_auth_dead = True
                    if status == 429:
                        await self._handle_rate_limit(
                            Exception("429"),
                            scope="profile_fetch_playwright",
                            metadata={
                                "username": username,
                                "endpoint": "web_profile_info",
                                "ingest_path": "playwright_profile_fetch",
                            },
                        )
                    else:
                        await self._record_rate_limit_event(
                            scope="profile_fetch_playwright",
                            status_code=status,
                            reason="Playwright profile auth response",
                            metadata={"username": username, "endpoint": "web_profile_info"},
                        )
                    if is_auth_failure:
                        logger.warning(
                            "Playwright Mode-β: %s returned %s for %s — session expired/unauthorized",
                            self._current_account.name if self._current_account else "?",
                            status, username,
                        )
                    else:
                        logger.info(
                            "Playwright Mode-β: in-page fetch for %s returned 429 — rate-limited",
                            username,
                        )
                    return None
                if status != 200:
                    logger.info(
                        "Playwright Mode-β: in-page fetch for %s returned status=%s",
                        username, status,
                    )
                    return None
                try:
                    data = json.loads(result.get("body") or "{}")
                except Exception as e:
                    logger.warning("Playwright Mode-β: JSON parse failed for %s: %s", username, e)
                    return None
                self._archive_raw_payload(
                    artifact_id=f"playwright/profiles/{username}/{time.time_ns()}",
                    payload=data,
                    target_tables=["instagram_profiles"],
                    metadata={
                        "payload_type": "instagram_playwright_profile_response",
                        "username": username,
                        "collection_account": acct_name,
                        "request_url": f"{GRAPH_API}/users/web_profile_info/",
                        "http_status": status,
                        "ingest_path": "playwright_profile_fetch",
                    },
                )
                user_data = data.get("data", {}).get("user", {})
                if user_data:
                    logger.info("Playwright Mode-β: recovered profile JSON for %s", username)
                    return user_data
                logger.info("Playwright Mode-β: empty user payload for %s", username)
                return None
            finally:
                # ALWAYS close the browser — leaks here will OOM the WSL host.
                try:
                    if browser is not None:
                        await browser.close()
                except Exception:
                    pass
                try:
                    await playwright_ctx.stop()
                except Exception:
                    pass

    def _playwright_posts_fallback_available(self, entity_name: str) -> bool:
        disabled_until = float(getattr(self, "_playwright_posts_disabled_until", 0.0) or 0.0)
        now = time.time()
        if disabled_until <= now:
            return True
        logger.info(
            "instagram/%s: skipping Playwright post fallback for %.0fs after repeated zero-edge parses",
            entity_name,
            disabled_until - now,
        )
        return False

    def _record_playwright_posts_fallback_result(self, entity_name: str, edge_count: int) -> None:
        if edge_count > 0:
            if self._playwright_posts_zero_count:
                logger.info(
                    "instagram/%s: Playwright post fallback recovered with %d edge(s); clearing zero-edge breaker",
                    entity_name,
                    edge_count,
                )
            self._playwright_posts_zero_count = 0
            self._playwright_posts_disabled_until = 0.0
            return

        self._playwright_posts_zero_count += 1
        threshold = int(getattr(self, "_playwright_posts_zero_threshold", 3) or 3)
        if self._playwright_posts_zero_count < threshold:
            return

        cooldown = int(getattr(self, "_playwright_posts_zero_cooldown_seconds", 3600) or 3600)
        cooldown = max(300, cooldown)
        self._playwright_posts_disabled_until = time.time() + cooldown
        logger.warning(
            "instagram: Playwright post fallback returned zero edges %d time(s); pausing post fallback for %ds",
            self._playwright_posts_zero_count,
            cooldown,
        )

    async def _collect_posts_playwright(self, uid: str, entity_name: str) -> bool:
        """Mode β: spin up a single-process headless Chromium, navigate to the
        profile, scrape ``window._sharedData`` / ``window.__additionalDataLoaded``,
        and upsert any post nodes we find.

        Concurrency: STRICT 1-at-a-time via the module-level ``PLAYWRIGHT_SEMAPHORE``.
        Do NOT raise this without bumping host RAM (see comment near the semaphore).
        """
        if not self._playwright_posts_fallback_available(entity_name):
            return False

        try:
            from playwright.async_api import async_playwright  # type: ignore
        except ImportError:
            logger.warning(
                "Playwright not installed — cannot run Mode β fallback for %s",
                entity_name,
            )
            return False

        acct_name = self._current_account.name if self._current_account else None
        storage_state = self._build_playwright_storage_state(acct_name) if acct_name else None
        ua = self.user_agents.get_for_domain("instagram.com")
        if self._current_account and self._current_account.fingerprint.get("user_agent"):
            ua = self._current_account.fingerprint["user_agent"]

        url = f"https://www.instagram.com/{entity_name}/"

        async with PLAYWRIGHT_SEMAPHORE:
            logger.info(
                "Playwright fallback launching Chromium for instagram/%s (account=%s)",
                entity_name, acct_name or "anonymous",
            )
            playwright_ctx = await async_playwright().start()
            browser = None
            try:
                browser = await playwright_ctx.chromium.launch(
                    headless=True,
                    args=PLAYWRIGHT_LAUNCH_ARGS,
                )
                context_kwargs: dict = {
                    "user_agent": ua,
                    "viewport": {"width": 1280, "height": 800},
                    "locale": "en-US",
                }
                if storage_state:
                    context_kwargs["storage_state"] = storage_state
                context = await browser.new_context(**context_kwargs)
                page = await context.new_page()

                try:
                    # "load" not "networkidle" — IG polls continuously and never goes
                    # network-idle, so networkidle always hangs to the timeout.
                    await headless_dwell("instagram posts goto")
                    await page.goto(url, wait_until="load", timeout=30000)
                    await headless_dwell("instagram posts evaluate")
                except Exception as e:
                    logger.warning("Playwright goto failed for %s: %s", url, e)
                    return False

                # IG layouts vary; try several extraction strategies.
                payload = await page.evaluate(
                    """() => {
                        const out = {};
                        try { out.shared = window._sharedData || null; } catch(e) {}
                        try {
                            const all = [];
                            for (const k of Object.keys(window)) {
                                if (k.startsWith('__additionalData')) all.push(window[k]);
                            }
                            out.additional = all;
                        } catch(e) {}
                        return out;
                    }"""
                )

                edges = self._extract_post_edges_from_payload(payload)
                self._archive_raw_payload(
                    artifact_id=f"playwright/posts/{entity_name}/{time.time_ns()}",
                    payload=payload,
                    target_tables=["instagram_posts"],
                    metadata={
                        "payload_type": "instagram_playwright_posts_window",
                        "platform_user_id": uid,
                        "username": entity_name,
                        "edge_count": len(edges),
                        "request_url": url,
                        "collection_account": acct_name,
                        "ingest_path": "playwright_posts",
                    },
                )
                if not edges:
                    self._record_playwright_posts_fallback_result(entity_name, 0)
                    logger.info(
                        "Playwright fallback: no post edges parsed for %s "
                        "(IG layout may have changed)", entity_name,
                    )
                    return False

                self._record_playwright_posts_fallback_result(entity_name, len(edges))
                logger.info(
                    "Playwright fallback: extracted %d post nodes for %s",
                    len(edges), entity_name,
                )
                for edge in edges:
                    if self._stop.is_set():
                        break
                    node = edge.get("node", edge) if isinstance(edge, dict) else {}
                    if node:
                        try:
                            await self._process_post(node, uid, entity_name)
                        except Exception as e:
                            logger.debug("process_post failed: %s", e)
                return True
            finally:
                # ALWAYS close the browser — leaks here will OOM the WSL host.
                try:
                    if browser is not None:
                        await browser.close()
                except Exception:
                    pass
                try:
                    await playwright_ctx.stop()
                except Exception:
                    pass
        return False

    @staticmethod
    def _extract_post_edges_from_payload(payload: dict) -> list:
        """Best-effort traversal of IG's nested JSON shapes to find post edges."""
        return _parse_extract_post_edges(payload)

    async def _collect_stories(self, uid: str, entity_name: str):
        """Collect stories for a user.
        
        Uses run_in_executor to wrap sync instaloader calls so we don't block
        the asyncio event loop (see §blocking-instaloader pattern).
        """
        if not self._loader:
            return
        try:
            import instaloader
            loop = asyncio.get_event_loop()
            
            # Sync: Profile.from_id + get_stories iteration
            def _fetch_story_items_sync() -> list[dict]:
                items = []
                try:
                    profile = instaloader.Profile.from_id(self._loader.context, int(uid))
                    for story in self._loader.get_stories(userids=[profile.userid]):
                        for item in story.get_items():
                            items.append({
                                "mediaid": item.mediaid,
                                "is_video": item.is_video,
                                "url": item.video_url if item.is_video else item.url,
                                "raw": item._asdict() if hasattr(item, "_asdict") else {},
                            })
                except Exception as e:
                    logger.debug("_fetch_story_items_sync failed for %s: %s", uid, e)
                return items
            
            story_items = await loop.run_in_executor(None, _fetch_story_items_sync)
            
            for item in story_items:
                if self._stop.is_set():
                    return
                await self.rate_limiter.async_wait("instagram.com", OperationType.MEDIA_DOWNLOAD)

                ext = "mp4" if item["is_video"] else "jpg"
                content_type = "story_video" if item["is_video"] else "story"
                cid = f"story_{item['mediaid']}"

                if self.is_known(cid):
                    continue

                await self.download_media({
                    "entity_id": uid,
                    "entity_name": entity_name,
                    "content_type": content_type,
                    "content_id": cid,
                    "url": item["url"],
                    "extension": ext,
                    "raw": item["raw"],
                })
        except Exception as e:
            logger.debug("Stories collection failed for %s: %s", entity_name, e)

    async def _collect_highlights(self, client: httpx.AsyncClient, uid: str, entity_name: str):
        """Collect highlights for a user.
        
        Uses run_in_executor to wrap sync instaloader calls so we don't block
        the asyncio event loop (see §blocking-instaloader pattern).
        """
        if not self._loader:
            return
        try:
            import instaloader
            loop = asyncio.get_event_loop()
            
            # Sync: Profile.from_id + get_highlights iteration
            def _fetch_highlight_items_sync() -> list[dict]:
                items = []
                try:
                    profile = instaloader.Profile.from_id(self._loader.context, int(uid))
                    for highlight in self._loader.get_highlights(profile):
                        for item in highlight.get_items():
                            items.append({
                                "highlight_unique_id": highlight.unique_id,
                                "mediaid": item.mediaid,
                                "is_video": item.is_video,
                                "url": item.video_url if item.is_video else item.url,
                                "raw": item._asdict() if hasattr(item, "_asdict") else {},
                            })
                except Exception as e:
                    logger.debug("_fetch_highlight_items_sync failed for %s: %s", uid, e)
                return items
            
            highlight_items = await loop.run_in_executor(None, _fetch_highlight_items_sync)
            
            for item in highlight_items:
                if self._stop.is_set():
                    return
                await self.rate_limiter.async_wait("instagram.com", OperationType.MEDIA_DOWNLOAD)

                ext = "mp4" if item["is_video"] else "jpg"
                content_type = "highlight_video" if item["is_video"] else "highlight"
                cid = f"highlight_{item['highlight_unique_id']}_{item['mediaid']}"

                if self.is_known(cid):
                    continue

                await self.download_media({
                    "entity_id": uid,
                    "entity_name": entity_name,
                    "content_type": content_type,
                    "content_id": cid,
                    "url": item["url"],
                    "extension": ext,
                    "raw": item["raw"],
                })
        except Exception as e:
            logger.debug("Highlights collection failed for %s: %s", entity_name, e)

    async def _process_post(self, node: dict, uid: str, entity_name: str):
        shortcode = node.get("shortcode", "")
        typename = node.get("__typename", "")

        # Save post metadata to database
        await self._upsert_post(node, uid)

        if typename == "GraphSidecar":
            sidecar_edges = (node.get("edge_sidecar_to_children", {})
                             .get("edges", []))
            for i, se in enumerate(sidecar_edges):
                child = se.get("node", {})
                cid = f"{shortcode}_{i}"
                if not self.is_known(cid):
                    await self._download_node(child, uid, entity_name, cid, parent_node=node)
        else:
            if not self.is_known(shortcode):
                await self._download_node(node, uid, entity_name, shortcode)

    async def _upsert_post(self, node: dict, uid: str):
        caption = ""
        edges = node.get("edge_media_to_caption", {}).get("edges", [])
        if edges:
            caption = edges[0].get("node", {}).get("text", "")

        # Extract location data from the node (populated when geotags are enabled)
        loc = node.get("location") or {}
        location_name = loc.get("name") or None
        location_lat = loc.get("lat") or None
        location_lng = loc.get("lng") or None

        async with self.pool.acquire() as conn:
            # First ensure profile exists (might be missing if we are spidering
            # from a post). Root-cause fix for the NULL-FK bug: if the author
            # profile hasn't been collected yet, create a minimal stub keyed on
            # the uid so the post is never inserted with a NULL profile_id (which
            # hid it from analyzer timelines / geo). The profile collector later
            # enriches this same row (unique platform_user_id).
            profile_row = await conn.fetchrow("SELECT id FROM instagram_profiles WHERE platform_user_id = $1", uid)
            if profile_row:
                profile_uuid = profile_row['id']
            else:
                profile_uuid = await conn.fetchval("""
                    INSERT INTO instagram_profiles (platform_user_id)
                    VALUES ($1)
                    ON CONFLICT (platform_user_id) DO UPDATE
                        SET platform_user_id = EXCLUDED.platform_user_id
                    RETURNING id
                """, uid)

            await conn.execute("""
                INSERT INTO instagram_posts (
                    platform_post_id, profile_id, media_type, caption,
                    location_name, location_lat, location_lng,
                    likes_count, comments_count, platform_created_at,
                    collected_at, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW(), $11)
                ON CONFLICT (platform_post_id) DO UPDATE SET
                    profile_id = COALESCE(instagram_posts.profile_id, EXCLUDED.profile_id),
                    likes_count = EXCLUDED.likes_count,
                    comments_count = EXCLUDED.comments_count,
                    caption = EXCLUDED.caption,
                    location_name = COALESCE(EXCLUDED.location_name, instagram_posts.location_name),
                    location_lat = COALESCE(EXCLUDED.location_lat, instagram_posts.location_lat),
                    location_lng = COALESCE(EXCLUDED.location_lng, instagram_posts.location_lng),
                    metadata = EXCLUDED.metadata
            """,
            node.get("shortcode"), profile_uuid, node.get("__typename"), caption,
            location_name, location_lat, location_lng,
            node.get("edge_media_preview_like", {}).get("count", 0),
            node.get("edge_media_to_comment", {}).get("count", 0),
            datetime.fromtimestamp(node.get("taken_at_timestamp", time.time())),
            # metadata is a jsonb column; asyncpg has no dict->jsonb codec here, so
            # a raw dict threw on EVERY post (silently swallowed by callers' bare
            # except) => instagram_posts stayed empty. Encode to a JSON string.
            json.dumps(node, default=str),
            )
        self._archive_raw_payload(
            artifact_id=f"posts/{node.get('shortcode') or node.get('id') or 'unknown'}/{time.time_ns()}",
            payload=node,
            target_tables=["instagram_posts"],
            metadata={
                "payload_type": "instagram_post_node",
                "platform_user_id": uid,
                "platform_post_id": node.get("shortcode") or node.get("id"),
                "media_type": node.get("__typename"),
                "collection_account": self._current_account.name if self._current_account else None,
                "ingest_path": self.INGEST_PATH,
            },
        )

    async def _download_node(self, node: dict, uid: str, entity_name: str, content_id: str, parent_node: dict | None = None):
        is_video = node.get("is_video", False)

        if is_video:
            url = node.get("video_url")
            ext = "mp4"
            content_type = "video"
        else:
            url = node.get("display_url")
            ext = "jpg"
            content_type = "post"

        if not url:
            return

        await self._content_aware_delay(content_type)
        await self.download_media({
            "entity_id": uid,
            "entity_name": entity_name,
            "content_type": content_type,
            "content_id": content_id,
            "url": url,
            "extension": ext,
            "source_url": f"https://www.instagram.com/p/{content_id.split('_')[0]}/",
            "raw": node if not parent_node else {"node": node, "parent": parent_node}
        })

    async def _warmup(self, client: httpx.AsyncClient):
        if self._warmed_up:
            return
        warmup_enabled = os.getenv("INSTA_WARMUP_ENABLED", "true").lower() == "true"
        if not warmup_enabled:
            self._warmed_up = True
            return
        try:
            logger.info("Warmup: initial pause (simulating app open)...")
            await asyncio.sleep(random.uniform(30, 60))
            await client.get(
                "https://www.instagram.com/",
                headers=self._headers(self._current_account),
            )
            logger.info("Warmup: browsing pause...")
            await asyncio.sleep(random.uniform(30, 60))
            logger.info("Warmup: pre-operation pause...")
            await asyncio.sleep(random.uniform(30, 60))
            self._warmed_up = True
            logger.info("Warmup sequence complete")
        except Exception as e:
            logger.debug("Warmup failed (non-fatal): %s", e)
            self._warmed_up = True

    def _check_session_age(self, username: str) -> bool:
        meta_file = self._session_dir / f"{username}.meta"
        if not meta_file.exists():
            self._save_session_meta(username)
            return True
        try:
            meta = json.loads(meta_file.read_text())
            created_at = meta.get("created_at", 0)
            jitter = random.randint(-3, 3) * 86400
            max_age = (self._session_max_age_days * 86400) + jitter
            if time.time() - created_at > max_age:
                logger.info("Session for %s expired (age %dd), forcing re-auth",
                            username, int((time.time() - created_at) // 86400))
                return False
            return True
        except Exception as e:
            # Corrupt or unreadable session meta means we don't know how old
            # the session is. Treat as expired and force re-auth — using a
            # potentially-old session is ban-bait.
            logger.warning(
                "Session meta for %s unreadable (%s), forcing re-auth",
                username, e,
            )
            return False

    def _save_session_meta(self, username: str):
        meta_file = self._session_dir / f"{username}.meta"
        meta_file.write_text(json.dumps({
            "created_at": time.time(),
            "username": username,
        }))

    async def download_media(self, item: dict):
        cid = item["content_id"]
        if self.is_known(cid):
            return

        filename = self.build_filename(
            entity_id=item["entity_id"],
            entity_name=item["entity_name"],
            content_type=item["content_type"],
            content_id=cid,
            extension=item.get("extension", "jpg"),
        )

        # Per-account subdirectory isolation
        dest_dir = self.account_media_dir / item["content_type"]
        dest = dest_dir / filename
        assert_media_write_allowed(dest)
        dest_dir.mkdir(parents=True, exist_ok=True)

        if dest.exists():
            return

        try:
            await self.rate_limiter.async_wait("instagram.com", OperationType.MEDIA_DOWNLOAD)
            cookies = self._get_session_cookies()
            async with httpx.AsyncClient(
                timeout=60, cookies=cookies, follow_redirects=True,
            ) as client:
                resp = await client.get(item["url"])
                resp.raise_for_status()
                data = resp.content

            sha = self.sha256_bytes(data)
            
            metadata = {
                "entity_id": item["entity_id"],
                "entity_name": item["entity_name"],
                "content_type": item["content_type"],
                "content_id": cid,
                "source_url": item.get("source_url"),
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "raw": item.get("raw", {})
            }
            artifact = write_atomic_artifact(
                source="instagram",
                artifact_id=f"headless/{item['content_type']}/{cid}",
                artifact_kind="media_blob",
                data=data,
                extension=item.get("extension", "jpg"),
                expected_sha256=sha,
                metadata={
                    **metadata,
                    "filename": filename,
                    "request_url": item["url"],
                    "ingest_path": self.INGEST_PATH,
                    "legacy_path": str(dest),
                    "rebuild_target_tables": ["media_items"],
                },
                root=VAULT_ROOT,
            )
            if artifact.path is None:
                raise RuntimeError(artifact.error or "artifact write failed")
            metadata["vault_artifact"] = {
                "ok": artifact.ok,
                "partial": artifact.partial,
                "path": artifact.relative_path,
                "blob_path": artifact.blob_relative_path,
                "sha256": artifact.sha256,
                "file_size": artifact.file_size,
                "sidecar_ok": artifact.sidecar.ok if artifact.sidecar else None,
                "sidecar_path": artifact.sidecar.relative_path if artifact.sidecar else None,
                "duplicate_blob": artifact.duplicate_blob,
                "error": artifact.error,
            }
            self._known_ids.add(cid)

            self.rate_limiter.record_success("instagram.com")

            await self.insert_media_item(
                entity_id=item["entity_id"],
                entity_name=item["entity_name"],
                content_type=item["content_type"],
                content_id=cid,
                filename=filename,
                file_path=str(artifact.path),
                file_size=len(data),
                sha256=sha,
                source_url=item.get("source_url"),
                metadata=metadata
            )
        except Exception as e:
            self.rate_limiter.record_failure("instagram.com")
            logger.error("Download failed %s: %s", cid, e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))

    # =====================================================================
    # === AUTH + PROFILE (Agent F-A) — expanded port ======================
    # =====================================================================
    # Everything below this banner is owned by Agent F-A (Wave 2 F-A).
    # Agent F-B's scope (posts / spider / Playwright / stories /
    # highlights) lives EARLIER in the file — do not interleave new
    # post-side methods here.
    #
    # Absorbed from instagramtoolkit/:
    #   • combined_modules.LoginManager challenge-handling (TOTP +
    #     drop-file + email-link sentinel)
    #   • profile_photo_tracker.ProfilePhotoTracker change-detection
    #     audit log (now via src/core/profile_photo_tracker)
    #   • collect_relationships.RelationshipCollector get_followers /
    #     get_followees enumeration (read-only, instaloader-driven)
    #   • profile_scanner._fetch_and_save  →  collect_user_profile()
    #   • account_manager per-account daily quota  →
    #     src/core/account_quota integration hook.
    #
    # Dropped (write-side or out-of-scope):
    #   • bulk_sender, send_photos, send_message, comment_*, like_*,
    #     follow_*, DM, story-reply.
    #   • account_manager CLI add/remove flows.
    #   • web/server.py standalone Flask UI.
    #   • main.py / main-Prawn-L390.py CLI entrypoints.
    #
    # Deferred (out-of-scope for Wave 2 F-A; revisit Wave 3+):
    #   • Per-account TLS fingerprint pinning (curl_cffi rotation).
    #   • Username DB reconciliation jobs (`scripts/refresh_sessions.py`).
    #   • Profile analyzer ML / nudity heuristic from profile_analyzer.py.
    # =====================================================================

    # ---------- AUTH: challenge handling ---------------------------------

    def _resolve_challenge_code(self, username: str, channel: str = "any") -> str:
        """Resolve a challenge / SMS / email-link code.

        Channels considered, in order:
          1. ``credentials/instagram/2fa/<username>.code`` drop-file
             (any digits / link).  Reused for every challenge type.
          2. ``credentials/instagram/challenge/<username>.<channel>``
             (sms / email).  Lets ops drop a code per-channel.
          3. INSTA_ACCOUNT_<N>_TOTP_SECRET (only if channel in {totp,any}).

        Empty string if nothing is available.  Always non-blocking.
        """
        # 1. legacy 2fa drop-file (already handled by _resolve_2fa_code; we
        #    also accept it for sms/email so ops only need one well-known
        #    location).
        code = self._resolve_2fa_code(username)
        if code:
            return code

        # 2. per-channel drop-file
        if channel and channel != "any":
            drop = Path("credentials/instagram/challenge") / f"{username}.{channel}"
            if drop.exists():
                try:
                    raw = drop.read_text(encoding="utf-8").strip()
                    token = raw.split()[0] if raw else ""
                    if token:
                        logger.info(
                            "Using challenge drop-file (%s) for %s", channel, username,
                        )
                        return token
                except Exception as e:
                    logger.warning(
                        "Failed to read challenge drop-file for %s/%s: %s",
                        username, channel, e,
                    )
        return ""

    def _consume_challenge_dropfile(self, username: str, channel: str) -> None:
        """One-shot deletion of the per-channel drop-file after success."""
        if channel in ("any", "totp"):
            return
        drop = Path("credentials/instagram/challenge") / f"{username}.{channel}"
        try:
            if drop.exists():
                drop.unlink()
                logger.info("Consumed challenge drop-file (%s) for %s", channel, username)
        except Exception:
            pass

    def _detect_challenge_kind(self, exc: BaseException) -> str:
        """Classify an instaloader exception as one of:
        ``2fa`` / ``sms`` / ``email`` / ``checkpoint`` / ``unknown``.
        """
        cls = type(exc).__name__
        text = str(exc).lower()
        if (
            cls == "TwoFactorAuthRequiredException"
            or "two-factor" in text
            or "two_factor" in text
            or "2fa" in text
        ):
            return "2fa"
        if "sms" in text:
            return "sms"
        if "email" in text or "e-mail" in text:
            return "email"
        if "checkpoint" in text or "challenge" in text:
            return "checkpoint"
        return "unknown"

    # ---------- AUTH: session-capsule helpers ----------------------------

    def _build_ig_session_capsule(self, account_name: str) -> "IgSession | None":
        """Wrap the current loader session in an IgSession capsule.

        Returns None if the optional :mod:`auth_session` module is missing
        or no cookies are present.  This is purely advisory — the legacy
        instaloader path remains the source-of-truth.
        """
        if IgSession is None:
            return None
        cookies = self._get_session_cookies()
        if not cookies:
            return None
        try:
            cap = IgSession(account_name=account_name)
            cap.cookies = dict(cookies)
            cap.user_id = cookies.get("ds_user_id") or cap.user_id
            cap.login_status = "logged_in"
            return cap
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("IgSession build failed for %s: %s", account_name, e)
            return None

    async def _is_session_alive(self, account_name: str) -> bool:
        """Use IgSession.is_alive (or warmup probe) to sanity-check auth.

        Falls back to True (assume alive) when the capsule module is not
        importable; the legacy ``test_login`` path will catch dead
        sessions on the next operation anyway.
        """
        cap = self._build_ig_session_capsule(account_name)
        if cap is None:
            return True
        try:
            return await cap.is_alive()
        except Exception as e:
            logger.debug("is_alive probe raised for %s: %s — assuming alive", account_name, e)
            return True

    # ---------- AUTH: account-quota integration --------------------------

    def _quota_tracker(self):
        """Return the process-wide AccountQuotaTracker, or None."""
        try:
            return _get_default_quota_tracker()
        except Exception:
            return None

    async def _quota_has_room(
        self, account_name: str, weight: int = 1,
    ) -> bool:
        """Defer to AccountQuotaTracker if registered, else fall back to
        legacy in-memory ``_check_daily_quota``.
        """
        tracker = self._quota_tracker()
        if tracker is None:
            return self._check_daily_quota(account_name)
        try:
            ok = await tracker.has_quota("instagram", account_name, weight=weight)
            if not ok:
                logger.warning(
                    "AccountQuotaTracker says instagram/%s exhausted (weight=%d)",
                    account_name, weight,
                )
            return ok
        except Exception as e:  # pragma: no cover
            logger.debug("quota.has_quota failed for %s: %s — legacy fallback", account_name, e)
            return self._check_daily_quota(account_name)

    async def _quota_consume(
        self, account_name: str, *, views: int = 0, actions: int = 1,
    ) -> None:
        """Record consumption against AccountQuotaTracker (best-effort)
        AND the legacy in-memory counter."""
        # legacy counter — always update so reads via _check_daily_quota
        # remain consistent within the process lifetime.
        self._record_daily_action(account_name, views=views, actions=actions)
        tracker = self._quota_tracker()
        if tracker is None:
            return
        try:
            weight = max(1, actions + views)
            await tracker.consume("instagram", account_name, weight=weight)
        except QuotaExhaustedError:
            # Already exhausted — surface as a soft signal; the next
            # has_room check will short-circuit.
            logger.warning(
                "AccountQuotaTracker.consume exhausted for instagram/%s",
                account_name,
            )
        except Exception as e:  # pragma: no cover
            logger.debug("quota.consume failed for %s: %s", account_name, e)

    # ---------- PROFILE: standalone read API -----------------------------

    async def collect_user_profile(
        self,
        username: str,
        *,
        client: "httpx.AsyncClient | None" = None,
        download_photo: bool = True,
    ) -> dict:
        """Collect a single user's profile and (optionally) profile-photo.

        This is the standalone entrypoint called by the scheduler /
        ad-hoc tools.  Returns the raw user dict from the Graph
        ``web_profile_info`` endpoint, or ``{}`` on miss.

        It does NOT enumerate posts / followers / following — those are
        explicit follow-up calls.  The legacy combined ``_collect_user``
        path (which fans out into posts + photo + spider) remains the
        primary worker code path.
        """
        own_client = False
        if client is None:
            cookies = self._get_session_cookies()
            proxy = self._get_proxy(self._current_account)
            kw: dict = dict(
                timeout=30,
                cookies=cookies,
                headers=self._headers(self._current_account),
                follow_redirects=True,
            )
            if proxy:
                kw["proxy"] = proxy
            client = httpx.AsyncClient(**kw)
            own_client = True

        try:
            acct = self._current_account.name if self._current_account else None
            await self.rate_limiter.async_wait(
                "instagram.com", OperationType.PROFILE_VIEW, account=acct,
            )
            resp = await client.get(
                f"{GRAPH_API}/users/web_profile_info/",
                params={"username": username},
            )
            if resp.status_code == 404:
                logger.info("collect_user_profile: %s not found", username)
                return {}
            if resp.status_code in (401, 403):
                await self._record_rate_limit_event(
                    scope="profile_fetch",
                    status_code=resp.status_code,
                    reason="collect_user_profile auth/rate response",
                    metadata={"username": username, "endpoint": "web_profile_info"},
                )
            if resp.status_code == 429:
                await self._handle_rate_limit(
                    Exception("429"),
                    scope="profile_fetch",
                    metadata={"username": username, "endpoint": "web_profile_info"},
                )
                return {}
            resp.raise_for_status()
            profile_response = resp.json()
            self._archive_raw_payload(
                artifact_id=f"httpx/profiles/{username}/{time.time_ns()}",
                payload=profile_response,
                target_tables=["instagram_profiles"],
                metadata={
                    "payload_type": "instagram_httpx_profile_response",
                    "username": username,
                    "collection_account": acct,
                    "request_url": f"{GRAPH_API}/users/web_profile_info/",
                    "http_status": resp.status_code,
                    "ingest_path": "collect_user_profile",
                },
            )
            user = profile_response.get("data", {}).get("user", {}) or {}
            if not user:
                await self._record_profile_access(username, False, error="empty profile data")
                return {}

            # ── User-intelligence diff: snapshot the row BEFORE upserting so the
            # change tracker can compare old → new and emit one row per changed
            # field into instagram_user_changes. Wrapped in try/except so any
            # failure (DB, schema drift, etc.) is non-fatal to ingestion.
            prev_row = None
            try:
                async with self.pool.acquire() as conn:
                    prev_row = await conn.fetchrow(
                        "SELECT username, full_name, bio, followers_count, "
                        "following_count, posts_count, is_verified, is_private, "
                        "profile_pic_url, external_url "
                        "FROM instagram_profiles WHERE platform_user_id = $1",
                        str(user.get("id") or username),
                    )
            except Exception as exc:
                logger.debug("user_change_tracker[ig]: prev-row fetch failed: %s", exc)

            await self._upsert_profile(user)
            await self._record_profile_access(username, True, user)

            try:
                tracker = UserChangeTracker(self.pool)
                # Normalize prev_row (DB column names) into the same key-space
                # as INSTAGRAM_TRACKED_FIELDS (Graph payload-style names).
                current_normalized: dict | None = None
                if prev_row is not None:
                    pr = dict(prev_row)
                    current_normalized = {
                        "username":         pr.get("username"),
                        "full_name":        pr.get("full_name"),
                        "biography":        pr.get("bio"),
                        "is_verified":      pr.get("is_verified"),
                        "is_private":       pr.get("is_private"),
                        "profile_pic_url":  pr.get("profile_pic_url"),
                        "follower_count":   pr.get("followers_count"),
                        "following_count":  pr.get("following_count"),
                        "post_count":       pr.get("posts_count"),
                        "external_url":     pr.get("external_url"),
                        # is_business is not stored in instagram_profiles today;
                        # left absent so the first observation is baseline-only.
                    }
                new_snapshot = {
                    "username":         user.get("username"),
                    "full_name":        user.get("full_name"),
                    "biography":        user.get("biography"),
                    "is_verified":      user.get("is_verified"),
                    "is_private":       user.get("is_private"),
                    "is_business":      user.get("is_business_account",
                                                  user.get("is_business")),
                    "profile_pic_url":  (user.get("profile_pic_url_hd")
                                          or user.get("profile_pic_url")),
                    "follower_count":   (user.get("edge_followed_by") or {}).get("count"),
                    "following_count":  (user.get("edge_follow") or {}).get("count"),
                    "post_count":       (user.get("edge_owner_to_timeline_media") or {}).get("count"),
                    "external_url":     user.get("external_url"),
                }
                try:
                    pk_val = int(user.get("id") or 0)
                except (TypeError, ValueError):
                    pk_val = 0
                if pk_val:
                    await tracker.detect_and_log(
                        table="instagram_user_changes",
                        pk_col="user_id",
                        pk_val=pk_val,
                        current_row=current_normalized,
                        new_row=new_snapshot,
                        fields=INSTAGRAM_TRACKED_FIELDS,
                    )
            except Exception as exc:
                logger.debug("user_change_tracker[ig]: detect_and_log failed: %s", exc)

            uid = user.get("id", username)
            entity_name = user.get("username", username)

            if download_photo:
                pic = user.get("profile_pic_url_hd") or user.get("profile_pic_url")
                if pic:
                    await self._track_profile_photo_change(
                        uid=uid, entity_name=entity_name, photo_url=pic,
                        raw=user,
                    )

            if self._current_account:
                await self._quota_consume(
                    self._current_account.name, views=1, actions=1,
                )
            return user
        finally:
            if own_client:
                await client.aclose()

    async def _track_profile_photo_change(
        self,
        *,
        uid: str,
        entity_name: str,
        photo_url: str,
        raw: dict | None = None,
    ) -> None:
        """Download (if changed) and audit-log a profile-photo update.

        Uses :class:`ProfilePhotoTracker` for change detection.  When a
        change is observed, the new photo is dual-recorded as a
        ``profile_photo`` media item AND appended to the audit log
        (``instagram_profile_photo_history`` if the table exists, else
        a debug log line — schema migration is out of scope here).
        """
        dest_dir = self.account_media_dir / "profiles"
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            changed, path = await self._photo_tracker.check_and_download(
                photo_url, uid, "instagram", dest_dir,
            )
        except Exception as e:
            logger.warning("profile-photo check failed for %s: %s", entity_name, e)
            return
        if not (changed and path):
            return

        try:
            data = path.read_bytes()
        except Exception as e:
            logger.warning("profile-photo read failed for %s: %s", entity_name, e)
            return

        # Prefer the unified dedupe_hash sha256, fall back to BaseCollector's.
        sha = (
            _dedupe_sha256(data) if _dedupe_sha256 is not None
            else self.sha256_bytes(data)
        )
        metadata = {"raw": raw or {}, "source_url": photo_url}
        artifact_meta = self._photo_tracker.last_artifact_metadata()
        if artifact_meta:
            metadata["vault_artifact"] = artifact_meta

        await self.insert_media_item(
            entity_id=uid,
            entity_name=entity_name,
            content_type="profile_photo",
            content_id=f"profile_{uid}_{int(time.time())}",
            filename=path.name,
            file_path=str(path),
            file_size=len(data),
            sha256=sha,
            metadata=metadata,
        )

        # Best-effort audit-log append.  Table may not exist yet — that's
        # fine, we degrade silently to a logger.info breadcrumb.
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO instagram_profile_photo_history
                        (platform_user_id, username, photo_url, sha256,
                         file_path, observed_at)
                    VALUES ($1, $2, $3, $4, $5, NOW())
                    """,
                    str(uid), entity_name, photo_url, sha, str(path),
                )
        except Exception:
            logger.info(
                "profile-photo CHANGE detected for %s (sha=%s, %d bytes) — "
                "history table absent, audit only in app log",
                entity_name, sha[:12], len(data),
            )

    # ---------- PROFILE: relationship enumeration ------------------------

    async def get_user_followers(
        self,
        username: str,
        *,
        max_count: int = 1000,
    ) -> list[dict]:
        """Enumerate followers of `username`.

        Uses the instaloader path (`Profile.get_followers`) under
        ``run_in_executor`` because instaloader is sync.  Read-only,
        bounded by ``max_count`` AND by the per-session ceiling
        ``INSTA_MAX_FOLLOWERS_PER_SESSION`` (default 5000) — Instagram
        bans aggressive enumeration.

        Returns a list of ``{"username": ..., "user_id": ...,
        "is_private": bool, "is_verified": bool}`` dicts.
        """
        return await self._collect_relationship(
            username=username,
            kind="followers",
            max_count=max_count,
        )

    async def get_user_following(
        self,
        username: str,
        *,
        max_count: int = 1000,
    ) -> list[dict]:
        """Enumerate accounts followed by `username`.  See
        :meth:`get_user_followers` for caveats."""
        return await self._collect_relationship(
            username=username,
            kind="following",
            max_count=max_count,
        )

    async def _collect_relationship(
        self,
        *,
        username: str,
        kind: str,
        max_count: int,
    ) -> list[dict]:
        if kind not in ("followers", "following"):
            raise ValueError(f"unknown relationship kind: {kind}")
        if not self._loader:
            self._init_loader()
        if not self._loader:
            logger.warning(
                "instaloader unavailable — cannot enumerate %s for %s",
                kind, username,
            )
            return []
        if self._current_account and not await self._quota_has_room(
            self._current_account.name, weight=2,
        ):
            return []

        session_cap = int(
            os.getenv("INSTA_MAX_FOLLOWERS_PER_SESSION", "5000")
        )
        effective = min(max_count, session_cap)
        logger.info(
            "Enumerating %s for %s (max=%d, session_cap=%d)",
            kind, username, effective, session_cap,
        )

        def _enumerate_sync() -> list[dict]:
            import instaloader  # local import to keep top-of-module light
            try:
                profile = instaloader.Profile.from_username(
                    self._loader.context, username,
                )
            except Exception as e:
                logger.warning("Profile.from_username(%s) failed: %s", username, e)
                return []
            iterator = (
                profile.get_followers() if kind == "followers"
                else profile.get_followees()
            )
            out: list[dict] = []
            try:
                for entry in iterator:
                    if len(out) >= effective:
                        break
                    out.append({
                        "username": getattr(entry, "username", None),
                        "user_id": str(getattr(entry, "userid", "") or ""),
                        "is_private": bool(getattr(entry, "is_private", False)),
                        "is_verified": bool(getattr(entry, "is_verified", False)),
                        "full_name": getattr(entry, "full_name", "") or "",
                    })
            except Exception as e:
                # Instaloader raises on rate-limit / private — keep what we got.
                logger.info(
                    "%s enumeration interrupted for %s after %d entries: %s",
                    kind, username, len(out), e,
                )
            return out

        loop = asyncio.get_event_loop()
        try:
            entries = await loop.run_in_executor(None, _enumerate_sync)
        except Exception as e:
            logger.error("%s enumeration crashed for %s: %s", kind, username, e)
            return []

        # Best-effort persistence to instagram_relationships if it exists.
        if entries:
            await self._persist_relationships(username, kind, entries)

        if self._current_account:
            await self._quota_consume(
                self._current_account.name, views=0,
                actions=max(1, len(entries) // 100),
            )
        return entries

    async def _persist_relationships(
        self, source_username: str, kind: str, entries: list[dict],
    ) -> None:
        """Persist enumerated relationships.  Silent no-op if the
        ``instagram_relationships`` table is absent — schema migration
        is owned by Wave 1 / DB layer, not this collector."""
        if not entries:
            return
        rows = [
            (
                source_username,
                e.get("username") or "",
                e.get("user_id") or "",
                kind,
            )
            for e in entries
            if e.get("username") or e.get("user_id")
        ]
        if not rows:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO instagram_relationships
                        (source_username, target_username, target_user_id,
                         relationship_type, observed_at)
                    VALUES ($1, $2, $3, $4, NOW())
                    ON CONFLICT (source_username, target_username,
                                 relationship_type)
                    DO UPDATE SET observed_at = NOW(),
                                  target_user_id = EXCLUDED.target_user_id
                    """,
                    rows,
                )
        except Exception:
            logger.debug(
                "instagram_relationships table absent — %d %s entries for %s "
                "kept in-memory only",
                len(rows), kind, source_username,
            )

    # =====================================================================
    # === END AUTH + PROFILE (Agent F-A) ==================================
    # =====================================================================

    # =====================================================================
    # === POSTS + SPIDER + PLAYWRIGHT (Agent F-B) =========================
    # =====================================================================
    # Wave 2 Batch F-B additions. Functions below extend post/reel/story/
    # highlight collection, add tagged + saved post enumeration, and wire
    # the Instagram follower graph into src/core/spider_discover.SpiderDiscover.
    # Read-only: no writes/follows/likes/comments.
    # ---------------------------------------------------------------------

    # ---- Reels collection -----------------------------------------------
    async def collect_user_reels(
        self, username: str, *, limit: int | None = None,
    ) -> int:
        """Enumerate a user's reels (clips). Uses instaloader's clips iterator
        when available, falls back to filtering posts by ``product_type``.

        Returns the number of reels processed.
        """
        if not self._loader:
            logger.debug("collect_user_reels(%s): loader not initialised", username)
            return 0

        max_reels = int(
            limit or os.getenv("INSTA_MAX_REELS_PER_USER", "50")
        )
        processed = 0
        try:
            import instaloader
            profile = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: instaloader.Profile.from_username(
                    self._loader.context, username,
                ),
            )
            uid = str(getattr(profile, "userid", "") or username)
            entity_name = getattr(profile, "username", username)

            # Prefer dedicated clips iterator if instaloader exposes it.
            iter_fn = getattr(profile, "get_clips", None) or getattr(
                profile, "get_reels", None,
            )
            if iter_fn is None:
                # Fall back to scanning posts and filtering by product_type.
                iter_fn = profile.get_posts

            for post in iter_fn():
                if self._stop.is_set() or processed >= max_reels:
                    break
                product_type = getattr(post, "product_type", None) or getattr(
                    post, "typename", "",
                )
                # Accept anything advertised as a clip/reel; if we fell back to
                # get_posts and product_type is missing we still want to skip
                # static images here.
                if iter_fn is profile.get_posts and product_type not in {
                    "clips", "reel", "GraphVideo",
                }:
                    if not getattr(post, "is_video", False):
                        continue

                shortcode = getattr(post, "shortcode", None)
                video_url = getattr(post, "video_url", None)
                if not shortcode or not video_url:
                    continue

                cid = f"reel_{shortcode}"
                if self.is_known(cid):
                    continue

                await self._content_aware_delay("reel")
                # Persist a row in instagram_posts for graph queries.
                node = {
                    "shortcode": shortcode,
                    "__typename": "GraphVideo",
                    "edge_media_preview_like": {
                        "count": getattr(post, "likes", 0) or 0,
                    },
                    "edge_media_to_comment": {
                        "count": getattr(post, "comments", 0) or 0,
                    },
                    "taken_at_timestamp": int(
                        getattr(post, "date_utc", datetime.utcnow()).timestamp()
                    ),
                    "edge_media_to_caption": {
                        "edges": [
                            {"node": {"text": getattr(post, "caption", "") or ""}}
                        ]
                    },
                    "video_url": video_url,
                    "display_url": getattr(post, "url", None),
                    "is_video": True,
                    "product_type": "clips",
                }
                try:
                    await self._upsert_post(node, uid)
                except Exception:
                    # WARNING (was debug): swallowed upsert = silent data loss.
                    logger.warning("reel upsert FAILED for %s", shortcode, exc_info=True)

                await self.download_media({
                    "entity_id": uid,
                    "entity_name": entity_name,
                    "content_type": "reel",
                    "content_id": cid,
                    "url": video_url,
                    "extension": "mp4",
                    "source_url": f"https://www.instagram.com/reel/{shortcode}/",
                    "raw": node,
                })
                processed += 1
        except Exception as e:
            logger.debug("collect_user_reels(%s) failed: %s", username, e)
        return processed

    # ---- Tagged posts ---------------------------------------------------
    async def collect_tagged_posts(
        self, username: str, *, limit: int | None = None,
    ) -> int:
        """Enumerate posts where ``username`` is tagged.

        Uses ``Profile.get_tagged_posts`` if instaloader exposes it; silently
        no-ops otherwise.
        """
        if not self._loader:
            return 0

        max_n = int(limit or os.getenv("INSTA_MAX_TAGGED_PER_USER", "30"))
        processed = 0
        try:
            import instaloader
            profile = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: instaloader.Profile.from_username(
                    self._loader.context, username,
                ),
            )
            uid = str(getattr(profile, "userid", "") or username)
            entity_name = getattr(profile, "username", username)

            tagged_iter = getattr(profile, "get_tagged_posts", None)
            if tagged_iter is None:
                logger.debug(
                    "collect_tagged_posts(%s): instaloader build lacks get_tagged_posts",
                    username,
                )
                return 0

            for post in tagged_iter():
                if self._stop.is_set() or processed >= max_n:
                    break
                shortcode = getattr(post, "shortcode", None)
                if not shortcode:
                    continue
                cid = f"tagged_{shortcode}"
                if self.is_known(cid):
                    continue
                is_video = bool(getattr(post, "is_video", False))
                url = getattr(post, "video_url", None) if is_video else getattr(
                    post, "url", None,
                )
                if not url:
                    continue
                await self._content_aware_delay("video" if is_video else "post")
                await self.download_media({
                    "entity_id": uid,
                    "entity_name": entity_name,
                    "content_type": "tagged_video" if is_video else "tagged_post",
                    "content_id": cid,
                    "url": url,
                    "extension": "mp4" if is_video else "jpg",
                    "source_url": f"https://www.instagram.com/p/{shortcode}/",
                    "raw": {
                        "shortcode": shortcode,
                        "owner": getattr(getattr(post, "owner_profile", None), "username", None),
                        "is_video": is_video,
                    },
                })
                processed += 1
        except Exception as e:
            logger.debug("collect_tagged_posts(%s) failed: %s", username, e)
        return processed

    # ---- Saved posts (own account only) ---------------------------------
    async def collect_saved_posts(self, *, limit: int | None = None) -> int:
        """Enumerate the *currently authenticated* account's saved posts.

        Only works when logged in (instaloader requires the auth context).
        Returns count processed.
        """
        if not self._loader or not self._current_account:
            return 0

        max_n = int(limit or os.getenv("INSTA_MAX_SAVED", "100"))
        processed = 0
        try:
            import instaloader
            # Instaloader exposes saved posts via the *test_login* / authenticated
            # profile. We grab the logged-in profile, then call get_saved_posts.
            ctx = self._loader.context
            login_user = ctx.username
            if not login_user:
                return 0
            profile = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: instaloader.Profile.from_username(ctx, login_user),
            )
            saved_iter = getattr(profile, "get_saved_posts", None)
            if saved_iter is None:
                logger.debug(
                    "collect_saved_posts: instaloader build lacks get_saved_posts",
                )
                return 0
            uid = str(getattr(profile, "userid", "") or login_user)
            entity_name = login_user

            for post in saved_iter():
                if self._stop.is_set() or processed >= max_n:
                    break
                shortcode = getattr(post, "shortcode", None)
                if not shortcode:
                    continue
                cid = f"saved_{shortcode}"
                if self.is_known(cid):
                    continue
                is_video = bool(getattr(post, "is_video", False))
                url = getattr(post, "video_url", None) if is_video else getattr(
                    post, "url", None,
                )
                if not url:
                    continue
                await self._content_aware_delay("video" if is_video else "post")
                await self.download_media({
                    "entity_id": uid,
                    "entity_name": entity_name,
                    "content_type": "saved_video" if is_video else "saved_post",
                    "content_id": cid,
                    "url": url,
                    "extension": "mp4" if is_video else "jpg",
                    "source_url": f"https://www.instagram.com/p/{shortcode}/",
                    "raw": {
                        "shortcode": shortcode,
                        "owner": getattr(getattr(post, "owner_profile", None), "username", None),
                        "is_video": is_video,
                        "saved_by": login_user,
                    },
                })
                processed += 1
        except Exception as e:
            logger.debug("collect_saved_posts failed: %s", e)
        return processed

    # ---- Public-API aliases (match Agent-F-B contract names) -----------
    async def collect_user_posts(self, username: str) -> bool:
        """Public entry: resolve username → uid then call _collect_posts.

        Returns True if at least one page parsed cleanly. Falls through to
        the Playwright fallback on auth/empty signals — same policy as the
        in-line dispatcher in _collect_user.
        """
        try:
            cookies = self._get_session_cookies()
            async with httpx.AsyncClient(
                timeout=30, cookies=cookies, headers=self._headers(self._current_account),
                follow_redirects=True,
            ) as client:
                resp = await client.get(
                    f"{GRAPH_API}/users/web_profile_info/",
                    params={"username": username},
                )
                if resp.status_code != 200:
                    if resp.status_code in (401, 403, 429):
                        await self._record_rate_limit_event(
                            scope="profile_fetch",
                            status_code=resp.status_code,
                            reason="profile fetch fallback auth/rate response",
                            metadata={"username": username, "endpoint": "web_profile_info"},
                        )
                    return False
                data = resp.json().get("data", {}).get("user", {}) or {}
                uid = str(data.get("id") or "")
                if not uid:
                    await self._record_profile_access(username, False, error="empty profile data")
                    return False
                await self._record_profile_access(username, True, data)
                ok = await self._collect_posts(client, uid, username)
                if not ok:
                    try:
                        await self._collect_posts_playwright(uid, username)
                    except Exception as e:
                        logger.debug("playwright fallback failed for %s: %s", username, e)
                return ok
        except Exception as e:
            logger.debug("collect_user_posts(%s) failed: %s", username, e)
            return False

    async def collect_stories(self, username: str) -> int:
        """Public alias around _collect_stories that resolves username → uid."""
        if not self._loader:
            return 0
        try:
            import instaloader
            profile = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: instaloader.Profile.from_username(
                    self._loader.context, username,
                ),
            )
            uid = str(getattr(profile, "userid", "") or username)
            entity_name = getattr(profile, "username", username)
            await self._collect_stories(uid, entity_name)
            return 1
        except Exception as e:
            logger.debug("collect_stories(%s) failed: %s", username, e)
            return 0

    async def collect_highlights(self, username: str) -> int:
        """Public alias around _collect_highlights."""
        if not self._loader:
            return 0
        try:
            import instaloader
            profile = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: instaloader.Profile.from_username(
                    self._loader.context, username,
                ),
            )
            uid = str(getattr(profile, "userid", "") or username)
            entity_name = getattr(profile, "username", username)
            cookies = self._get_session_cookies()
            async with httpx.AsyncClient(
                timeout=30, cookies=cookies, headers=self._headers(self._current_account),
                follow_redirects=True,
            ) as client:
                await self._collect_highlights(client, uid, entity_name)
            return 1
        except Exception as e:
            logger.debug("collect_highlights(%s) failed: %s", username, e)
            return 0

    # ---- Spider/discover wiring (Wave 0 spider_discover) -----------------
    async def _fetch_follow_list_web(self, client, owner_uid, direction, cap, delay):
        """Fetch an account's OWN followers/following via the cookie-authenticated
        friendships API (i.instagram.com/api/v1/friendships/{id}/{followers|following}).
        Paginated, capped, paced; STOPS on 429 (conservative). direction:
        'followers'|'following'. Returns the raw user dicts."""
        import random
        max_pages = int(os.getenv("INSTA_MULTI_GRAPH_MAX_PAGES", "20"))
        users, max_id, pages = [], None, 0
        while len(users) < cap and pages < max_pages and not self._stop.is_set():
            params = {"count": 100}
            if max_id:
                params["max_id"] = max_id
            try:
                resp = await asyncio.wait_for(
                    client.get(f"{GRAPH_API}/friendships/{owner_uid}/{direction}/", params=params),
                    timeout=35.0)
            except Exception as e:
                logger.debug("multi-graph %s fetch error: %s", direction, e)
                break
            if resp.status_code in (429, 401, 403):
                # IG guards the friendships (follower-list) endpoint hard: even a valid
                # fresh session gets 401 "Please wait a few minutes" after a handful of
                # requests. Treat ALL of these as a soft rate-limit — STOP immediately,
                # do NOT mark the account dead (the cookie is fine), and try again on
                # the next throttle window. This is why the extension is the safe path.
                await self._record_rate_limit_event(
                    scope=f"multi_graph_{direction}",
                    status_code=resp.status_code,
                    reason="friendships endpoint auth/rate response",
                    metadata={
                        "owner_uid": owner_uid,
                        "direction": direction,
                        "endpoint": "friendships",
                    },
                )
                logger.warning("multi-graph: HTTP %d on %s %s — rate-limited, backing off (conservative)",
                               resp.status_code, owner_uid, direction)
                break
            if resp.status_code != 200:
                logger.debug("multi-graph %s HTTP %d", direction, resp.status_code)
                break
            try:
                data = resp.json()
            except Exception:
                break
            batch = data.get("users") or []
            if not batch:
                break
            users.extend(batch)
            max_id = data.get("next_max_id")
            pages += 1
            if not max_id:
                break
            await asyncio.sleep(random.uniform(*delay))
        return users[:cap]

    async def _write_follow_edges(self, owner_account, users, direction):
        """Persist each edge to follow_edges (per-account graph) + social_users union."""
        ctx = "follower" if direction == "followers" else "follow"
        for u in users:
            uid = str(u.get("pk") or u.get("id") or "")
            if not uid:
                continue
            uname = u.get("username")
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO follow_edges
                            (platform, owner_account, target_uid, direction, target_username, first_seen, last_seen)
                        VALUES ('instagram', $1, $2, $3, $4, now(), now())
                        ON CONFLICT (platform, owner_account, target_uid, direction) DO UPDATE SET
                            last_seen = now(),
                            target_username = COALESCE(EXCLUDED.target_username, follow_edges.target_username)
                        """,
                        owner_account, uid, direction, uname,
                    )
            except Exception:
                logger.debug("follow_edges write failed for %s", uid, exc_info=True)
            await self._upsert_social_user(uid, uname, u.get("full_name"), u.get("profile_pic_url"), ctx)

    async def _collect_all_account_graphs(self):
        """MULTI-ACCOUNT foundation: capture EACH owned account's own follow graph
        via ITS OWN cookies (friendships API) — the accounts the single-session
        extension can't reach. Writes follow_edges (per-account directional graph).

        DEFAULT OFF (INSTA_MULTI_GRAPH_ENABLED) so it never touches freshly-refreshed
        accounts until enabled deliberately. Conservative: per-account throttle to
        once / INSTA_MULTI_GRAPH_INTERVAL_HOURS, capped, 5-12s page delays, hard stop
        on 429, skips known-dead accounts.
        """
        if os.getenv("INSTA_MULTI_GRAPH_ENABLED", "false").lower() != "true":
            return
        import time as _t
        import random
        interval = float(os.getenv("INSTA_MULTI_GRAPH_INTERVAL_HOURS", "24")) * 3600
        cap = int(os.getenv("INSTA_MULTI_GRAPH_MAX", "3000"))
        delay = (float(os.getenv("INSTA_MULTI_GRAPH_DELAY_MIN", "5")),
                 float(os.getenv("INSTA_MULTI_GRAPH_DELAY_MAX", "12")))
        for acct_name, cookie_path in list((self._account_browser_cookies or {}).items()):
            owner_account = self._canonical_instagram_username(acct_name) or acct_name
            if self._stop.is_set():
                break
            if acct_name in self._dead_cookie_accounts:
                continue
            key = "multigraph:" + acct_name
            if _t.time() - self._last_own_graph.get(key, 0) < interval:
                continue
            if not cookie_path or not os.path.exists(cookie_path):
                continue
            cookies = self._parse_browser_cookies(cookie_path)
            owner_uid = cookies.get("ds_user_id")
            if not owner_uid or "sessionid" not in cookies:
                continue
            self._last_own_graph[key] = _t.time()  # claim before work (avoid re-run on error)
            headers = self._headers()
            headers["X-CSRFToken"] = cookies.get("csrftoken", "")
            try:
                async with httpx.AsyncClient(cookies=cookies, headers=headers,
                                             timeout=35.0, follow_redirects=True) as client:
                    for direction in ("followers", "following"):
                        if self._stop.is_set():
                            break
                        users = await self._fetch_follow_list_web(client, owner_uid, direction, cap, delay)
                        await self._write_follow_edges(owner_account, users, direction)
                        logger.info("instagram multi-graph: account %s %s -> %d edges",
                                    owner_account, direction, len(users))
                        await asyncio.sleep(random.uniform(*delay))
            except Exception as e:
                logger.warning("instagram multi-graph: account %s failed: %s", acct_name, e)

    async def _record_cookie_status(self, account: str, status: str, reason):
        """Persist per-account cookie validity for the /accounts dashboard panel so
        it can show 'refresh needed' without live-probing (ban-sensitive for IG).
        The collector tests every cycle; this just records the outcome."""
        if not account or self.pool is None:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO cookie_status (platform, account, status, reason, checked_at)
                    VALUES ('instagram', $1, $2, $3, now())
                    ON CONFLICT (platform, account) DO UPDATE SET
                        status = EXCLUDED.status, reason = EXCLUDED.reason, checked_at = now()
                    """,
                    account, status, reason,
                )
        except Exception:
            logger.debug("cookie_status upsert failed for %s", account, exc_info=True)

    async def _upsert_social_user(self, uid, username, display_name, photo, context):
        """Record one user in social_users with a relationship context. Matches the
        extension/ig_ingest convention (uid = numeric id when known) so headless and
        extension writes converge on the same row instead of duplicating."""
        if not uid:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO social_users
                        (platform, uid, platform_user_id, username, display_name,
                         profile_photo_url, contexts, first_seen, last_seen, times_seen)
                    VALUES ('instagram', $1, $1, $2, $3, $4, ARRAY[$5], now(), now(), 1)
                    ON CONFLICT (platform, uid) DO UPDATE SET
                        last_seen = now(),
                        times_seen = social_users.times_seen + 1,
                        username = COALESCE(EXCLUDED.username, social_users.username),
                        display_name = COALESCE(social_users.display_name, EXCLUDED.display_name),
                        profile_photo_url = COALESCE(social_users.profile_photo_url, EXCLUDED.profile_photo_url),
                        contexts = (SELECT array(SELECT DISTINCT unnest(social_users.contexts || EXCLUDED.contexts)))
                    """,
                    str(uid), username, display_name, photo, context,
                )
        except Exception:
            logger.debug("own-graph: social_users upsert failed for %s", uid, exc_info=True)

    async def _collect_own_follow_graph(self, owner_uid: str, owner_name: str):
        """Capture the LOGGED-IN owner's OWN followers + following into social_users
        with 'follower'/'follow' contexts.

        Scraping your OWN graph is a normal, bounded user action — distinct from the
        open discovery spider (which is disabled for anti-ban) — so it's gated by its
        own INSTA_OWN_GRAPH_ENABLED and paced by the same human rate-limiter. Bounded
        by INSTA_OWN_GRAPH_MAX per side. This is the headless counterpart to the
        extension's ds_user_id self-graph capture.
        """
        if not owner_uid:
            return
        if not self._loader:
            # Cookie/Playwright-primary mode has no instaloader session — the browser
            # EXTENSION captures the owner follow graph in that mode (ban-safe live
            # session). Only the instaloader path (cookie-login / PLAYWRIGHT_PRIMARY
            # =false) can run this headless. Log once so it's not silently dead.
            logger.info(
                "instagram own-graph: no instaloader session for %s (cookie/Playwright "
                "mode) — extension handles the follow graph here", owner_name,
            )
            return
        import instaloader as _il
        cap = int(os.getenv("INSTA_OWN_GRAPH_MAX", "3000"))
        loader = self._loader
        try:
            prof = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _il.Profile.from_id(loader.context, int(owner_uid)),
            )
        except Exception as e:
            logger.debug("own-graph: Profile.from_id(%s) failed: %s", owner_uid, e)
            return
        for iter_name, ctx, label in (
            ("get_followers", "follower", "followers"),
            ("get_followees", "follow", "following"),
        ):
            iter_fn = getattr(prof, iter_name, None)
            if iter_fn is None:
                continue
            count = 0
            try:
                for nb in iter_fn():
                    if count >= cap or self._stop.is_set():
                        break
                    tid = str(getattr(nb, "userid", "") or "")
                    if not tid:
                        continue
                    # Same pacing as the rest of the collector (anti-ban).
                    await self.rate_limiter.async_wait("instagram.com", OperationType.PAGINATION)
                    await self._upsert_social_user(
                        tid, getattr(nb, "username", None) or None,
                        getattr(nb, "full_name", None) or None,
                        getattr(nb, "profile_pic_url", None) or None, ctx,
                    )
                    count += 1
            except Exception as e:
                logger.debug("own-graph: %s iterate failed: %s", label, e)
            logger.info("instagram own-graph: recorded %d %s for owner %s", count, label, owner_name)

    async def _spider_followers(self, client: httpx.AsyncClient, uid: str, username: str):
        """Discover followers/following via Wave 0 SpiderDiscover.

        Replaces the pre-existing stub. Honours INSTA_SPIDER_HOPS,
        INSTA_SPIDER_CONCURRENCY, INSTA_SPIDER_MAX_FOLLOWERS env vars and
        is fully read-only.
        """
        try:
            from src.core.spider_discover import (
                SpiderDiscover, EdgeType, Edge,
            )
        except Exception as e:
            logger.debug("spider_discover unavailable: %s", e)
            return

        if not self._loader:
            logger.debug("spider_followers(%s): loader not initialised", username)
            return

        max_followers = int(os.getenv("INSTA_SPIDER_MAX_FOLLOWERS", "200"))
        max_following = int(os.getenv("INSTA_SPIDER_MAX_FOLLOWING", "200"))
        max_hops = int(os.getenv("INSTA_SPIDER_HOPS", "1"))
        concurrency = int(os.getenv("INSTA_SPIDER_CONCURRENCY", "2"))

        loader = self._loader
        rate_limiter = self.rate_limiter
        acct_name = self._current_account.name if self._current_account else None

        class _IGFetcher:
            supported_edge_types = (EdgeType.FOLLOWER, EdgeType.FOLLOWING)

            async def fetch_edges(self, node_id, edge_type):
                # node_id is a numeric IG userid (string). We instaloader-lookup
                # via Profile.from_id and stream followers/followees, capped.
                import instaloader as _il
                cap = max_followers if edge_type == EdgeType.FOLLOWER else max_following
                try:
                    prof = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: _il.Profile.from_id(loader.context, int(node_id)),
                    )
                except Exception as e:
                    logger.debug("spider fetch_edges from_id(%s) failed: %s", node_id, e)
                    return
                iter_fn = (
                    prof.get_followers if edge_type == EdgeType.FOLLOWER
                    else prof.get_followees
                )
                count = 0
                try:
                    for neighbour in iter_fn():
                        if count >= cap:
                            break
                        target_id = str(getattr(neighbour, "userid", "") or "")
                        target_name = getattr(neighbour, "username", "")
                        if not target_id:
                            continue
                        # Honour rate-limit between edge yields.
                        await rate_limiter.async_wait(
                            "instagram.com",
                            OperationType.PAGINATION,
                            account=acct_name,
                        )
                        yield Edge(
                            source=str(node_id),
                            target=target_id,
                            edge_type=edge_type,
                            metadata={"username": target_name},
                        )
                        count += 1
                except Exception as e:
                    logger.debug(
                        "spider fetch_edges(%s,%s) iter raised %s",
                        node_id, edge_type, e,
                    )

        async def _edge_sink(edge):
            # Persist into instagram_relationships if the table exists; soft-fail
            # otherwise so the spider keeps moving.
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO instagram_relationships (
                            source_user_id, target_user_id, relationship_type,
                            target_username, collected_at
                        ) VALUES ($1, $2, $3, $4, NOW())
                        ON CONFLICT (source_user_id, target_user_id, relationship_type)
                        DO NOTHING
                        """,
                        edge.source, edge.target, edge.edge_type.value,
                        edge.metadata.get("username"),
                    )
            except Exception:
                pass

        try:
            spider = SpiderDiscover(
                platform="instagram",
                fetcher=_IGFetcher(),
                pool=self.pool,
                max_hops=max_hops,
                concurrency=concurrency,
                rate_waiter=lambda: self.rate_limiter.async_wait(
                    "instagram.com", OperationType.PROFILE_VIEW, account=acct_name,
                ),
                edge_sink=_edge_sink,
            )
            await spider.seed(uid)
            await spider.run()
            logger.info(
                "instagram spider for %s done: nodes=%s edges=%s",
                username,
                getattr(spider.stats, "nodes_processed", "?"),
                getattr(spider.stats, "edges_emitted", "?"),
            )
        except Exception as e:
            logger.warning("spider_followers(%s) failed: %s", username, e)

    # ---- Playwright helpers --------------------------------------------
    async def _playwright_fetch_url(
        self, url: str, *, account_name: str | None = None,
        wait_until: str = "networkidle", timeout_ms: int = 45000,
    ) -> dict | None:
        """Generic single-shot Playwright fetch: navigate → grab page payload.

        Used for endpoints instagrapi/instaloader/Graph can't hit (login walls,
        new layouts). Honours the strict 1-at-a-time global semaphore.
        Returns the parsed ``window`` payload or ``None``.
        """
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except ImportError:
            logger.debug("playwright not installed — _playwright_fetch_url skipped")
            return None

        storage_state = (
            self._build_playwright_storage_state(account_name)
            if account_name else None
        )
        ua = self.user_agents.get_for_domain("instagram.com")
        if self._current_account and self._current_account.fingerprint.get("user_agent"):
            ua = self._current_account.fingerprint["user_agent"]

        async with PLAYWRIGHT_SEMAPHORE:
            playwright_ctx = await async_playwright().start()
            browser = None
            try:
                browser = await playwright_ctx.chromium.launch(
                    headless=True, args=PLAYWRIGHT_LAUNCH_ARGS,
                )
                kw = {
                    "user_agent": ua,
                    "viewport": {"width": 1280, "height": 800},
                    "locale": "en-US",
                }
                if storage_state:
                    kw["storage_state"] = storage_state
                ctx = await browser.new_context(**kw)
                page = await ctx.new_page()
                try:
                    await headless_dwell("instagram generic goto")
                    await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
                    await headless_dwell("instagram generic evaluate")
                except Exception as e:
                    logger.debug("playwright_fetch_url goto(%s) failed: %s", url, e)
                    return None
                payload = await page.evaluate(
                    """() => {
                        const out = {};
                        try { out.shared = window._sharedData || null; } catch(e){}
                        try {
                            const all = [];
                            for (const k of Object.keys(window)) {
                                if (k.startsWith('__additionalData')) all.push(window[k]);
                            }
                            out.additional = all;
                        } catch(e){}
                        return out;
                    }"""
                )
                return payload if isinstance(payload, dict) else None
            finally:
                try:
                    if browser is not None:
                        await browser.close()
                except Exception:
                    pass
                try:
                    await playwright_ctx.stop()
                except Exception:
                    pass

    async def _collect_reels_playwright(self, uid: str, entity_name: str) -> int:
        """Mode β fallback for /<user>/reels/. Same parser as posts; we just
        navigate to a different URL."""
        url = f"https://www.instagram.com/{entity_name}/reels/"
        acct_name = self._current_account.name if self._current_account else None
        payload = await self._playwright_fetch_url(url, account_name=acct_name)
        if not payload:
            return 0
        edges = self._extract_post_edges_from_payload(payload)
        self._archive_raw_payload(
            artifact_id=f"playwright/reels/{entity_name}/{time.time_ns()}",
            payload=payload,
            target_tables=["instagram_posts"],
            metadata={
                "payload_type": "instagram_playwright_reels_window",
                "platform_user_id": uid,
                "username": entity_name,
                "edge_count": len(edges),
                "request_url": url,
                "collection_account": acct_name,
                "ingest_path": "playwright_reels",
            },
        )
        if not edges:
            return 0
        n = 0
        for edge in edges:
            if self._stop.is_set():
                break
            node = edge.get("node", edge) if isinstance(edge, dict) else {}
            if not node:
                continue
            try:
                await self._process_post(node, uid, entity_name)
                n += 1
            except Exception as e:
                logger.debug("reels playwright process_post failed: %s", e)
        return n

    # === END POSTS + SPIDER + PLAYWRIGHT (Agent F-B) =====================
