"""TikTok collector — Wave 2 hardened port of ``tiktoktoolkit/``.

Ported from the standalone ``tiktoktoolkit/`` (cli.py, provider.py, spider.py,
account_manager.py, rate_limiter.py, validation.py, invalid_username_detector.py,
profile_photo_tracker.py, ytdlp_downloader.py) into the unified collector
framework.

ABSORBED (parity targets, ~95%):
    - Cookie validation (Netscape jar required/recommended cookies)
    - gallery-dl + yt-dlp fallback chain with sidecar JSON ingest
    - Per-account media directory + atomic file save
    - Profile + post upserts (tiktok_profiles, tiktok_posts) including
      hashtags / mentions / challenges / music / stats / verified flags
    - Spider queue drain (legacy ``tiktok_spider_queue``) + Wave 0
      ``SpiderDiscover`` adapter (``TiktokEdgeFetcher``) over follower/following
    - Username validation + invalid-username classification (404/private/banned)
    - HTML SIGI_STATE / ItemModule scrape fallback
    - Adaptive per-account rate limiting (429 backoff via Wave 0 module)
    - Per-account daily quota cap (Wave 0 ``account_quota``)
    - Content dedupe via ``dedupe_hash.sha256_bytes``

DROPPED (intentionally — out of scope for read-only ingest):
    - Standalone web UI / dashboard (``cleanup_ui.py``)
    - CLI / setup wizard (``cli.py``, ``setup.bat``, interactive prompts)
    - Any write/post/upload endpoints (TikTok has none in the toolkit either,
      but the toolkit's reconciler write-paths are skipped)
    - Follow / like / DM writes (read-only; no graph mutation)
    - Browser-based login flow (``browser_downloader.py`` interactive auth)

DEFERRED (left as TODO for a later wave; non-blocking):
    - Playwright fallback (``_collect_via_playwright`` is a no-op stub —
      headless browser scraping is heavyweight and rarely needed when
      gallery-dl + yt-dlp succeed)
    - Profile-photo perceptual-hash change tracking (``profile_photo_tracker``
      from toolkit) — Wave 0 ``profile_photo_tracker`` exists but TikTok
      hasn't been wired in yet
    - Reconciler tier1/tier2 reconciliation jobs

⚠️  IP-CONFLICT WARNING
   Instagram, TikTok, and Lemon8 MUST NEVER run simultaneously when sharing
   a public IP (Meta and ByteDance both fingerprint cross-platform request
   patterns; concurrent traffic from the same IP triggers immediate
   challenges/bans). The scheduler / concurrency rule that enforces this
   lives outside this module (see ``scheduler/`` mutex group); the
   collector itself does no enforcement. If you're invoking ``run()``
   directly from a script, ensure no IG/Lemon8 collector is in flight.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import os
import random
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx

from src.core.base_collector import BaseCollector
from src.collectors.tiktok.parse import safe_int as _parse_safe_int, to_dt as _parse_to_dt
from src.core.file_naming import sanitize_name
from src.core.proximity import refresh_account_proximity_cache
from src.core.rate_limit_events import record_rate_limit_event
from src.core.vault import VAULT_ROOT, write_atomic_artifact
from src.core.user_change_tracker import (
    UserChangeTracker,
    TIKTOK_TRACKED_FIELDS,
)

# Wave 0 modules — imported lazily where heavy or where the module may be
# absent in some test environments. Top-level imports are kept for the
# always-present ones so ``ast.parse`` + container import surfaces breakage
# early.
try:
    from src.core.account_quota import AccountQuotaTracker, QuotaConfig
except Exception:  # pragma: no cover — keep collector importable
    AccountQuotaTracker = None  # type: ignore[assignment]
    QuotaConfig = None  # type: ignore[assignment]

try:
    from src.core.spider_discover import Edge, EdgeType, SpiderDiscover
except Exception:  # pragma: no cover
    Edge = None  # type: ignore[assignment]
    EdgeType = None  # type: ignore[assignment]
    SpiderDiscover = None  # type: ignore[assignment]

try:
    from src.core.dedupe_hash import sha256_bytes as _dedupe_sha256_bytes
except Exception:  # pragma: no cover
    _dedupe_sha256_bytes = None  # type: ignore[assignment]

# Follow-aware account selector (Phase 0). Records which cookie identity can
# see which target so a later pass can route private targets to an identity
# that actually follows them. Defensive import — collection still works
# without it. (Mirrors the Instagram collector's wiring.)
try:  # pragma: no cover
    from src.core.profile_access import ProfileAccessRepository, SmartAccountSelector
except Exception:  # pragma: no cover
    ProfileAccessRepository = None  # type: ignore[assignment]
    SmartAccountSelector = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Module-level toggle for the Playwright browser fallback. Re-read at the call
# site (via ``os.getenv``) so monkeypatch-based tests can flip it after import.
TIKTOK_BROWSER_FALLBACK_ENABLED = os.getenv(
    "TIKTOK_BROWSER_FALLBACK_ENABLED", "true"
).lower() == "true"

# Substrings in gallery-dl output that signal "no public videos available" —
# either the account is private, login-walled, removed, or anti-bot blocked.
# When we see one of these AND gallery-dl returned 0 items, we fall through to
# the browser fallback rather than declaring the user empty.
_BROWSER_FALLBACK_TRIGGER_KEYWORDS = (
    "private", "login_required", "login required", "401", "403", "404",
    "not found", "forbidden", "unauthorized", "captcha",
)

REQUIRED_COOKIES = {"sessionid", "tt_csrf_token", "ttwid", "msToken", "tt_chain_token", "sid_guard"}
RECOMMENDED_COOKIES = {"s_v_web_id", "odin_tt", "cmpl_token", "passport_csrf_token"}

# TikTok username pattern: alphanumeric + dot/underscore/hyphen, 1-30 chars.
# Ported from ``tiktoktoolkit/src/validation.py``.
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{1,30}$")


class InvalidReason(Enum):
    """Reasons a username is considered invalid (ported from toolkit)."""

    NOT_FOUND = "not_found"
    ACCOUNT_DELETED = "account_deleted"
    USERNAME_CHANGED = "username_changed"
    PRIVATE_BANNED = "private_banned"
    UNKNOWN = "unknown"


@dataclass
class ValidationResult:
    is_valid: bool
    is_rate_limited: bool = False
    is_network_error: bool = False
    invalid_reason: Optional[InvalidReason] = None
    error_message: Optional[str] = None
    should_retry: bool = False


def validate_username(username: str) -> str:
    """Validate + sanitize a TikTok username.

    Strips a leading ``@`` and asserts the result matches USERNAME_PATTERN.
    Raises ``ValueError`` on invalid input. Mirrors the toolkit's
    ``validation.validate_username`` but uses the unified ``ValueError`` so
    we don't pull in the toolkit's bespoke ``ValidationError`` class.
    """
    if not isinstance(username, str):
        raise ValueError(f"username must be str, got {type(username).__name__}")
    sanitized = username.strip().lstrip("@")
    if not sanitized:
        raise ValueError("empty username")
    if not USERNAME_PATTERN.match(sanitized):
        raise ValueError(f"invalid username format: {username!r}")
    return sanitized


# Keyword sets ported from invalid_username_detector.py — used by
# ``classify_invalid_username`` to map a scrape error → InvalidReason.
_NOT_FOUND_KEYWORDS = (
    "user not found", "couldn't find this user", "could not find this user",
    "no user found", "user does not exist", "user doesn't exist",
    "account doesn't exist", "account does not exist", "404",
)
_DELETED_KEYWORDS = (
    "account deleted", "account has been deleted", "account was deleted",
)
_CHANGED_KEYWORDS = (
    "username changed", "username has changed", "account moved",
)
_PRIVATE_BANNED_KEYWORDS = (
    "private account", "account is private",
    "account banned", "account has been banned", "account suspended",
)
_RATE_LIMIT_KEYWORDS = (
    "rate limit", "too many requests", "ratelimit", "rate_limit", "429",
)

_PROFILE_WALL_KEYWORDS = (
    ("captcha", "captcha"),
    ("security check", "security_check"),
    ("verify to continue", "verify_to_continue"),
    ("verify you are", "verify_human"),
    ("challenge", "challenge"),
    ("access denied", "access_denied"),
    ("unusual traffic", "unusual_traffic"),
    ("failed to solve javascript challenge", "javascript_challenge"),
)


def classify_invalid_username(
    err_text: str | None, http_status: Optional[int] = None
) -> ValidationResult:
    """Best-effort classification of a scrape error into a ValidationResult."""
    text = (err_text or "").lower()
    if http_status == 429 or any(k in text for k in _RATE_LIMIT_KEYWORDS):
        return ValidationResult(is_valid=True, is_rate_limited=True, should_retry=True)
    if http_status == 404 or any(k in text for k in _NOT_FOUND_KEYWORDS):
        return ValidationResult(is_valid=False, invalid_reason=InvalidReason.NOT_FOUND, error_message=err_text)
    if any(k in text for k in _DELETED_KEYWORDS):
        return ValidationResult(is_valid=False, invalid_reason=InvalidReason.ACCOUNT_DELETED, error_message=err_text)
    if any(k in text for k in _CHANGED_KEYWORDS):
        return ValidationResult(is_valid=False, invalid_reason=InvalidReason.USERNAME_CHANGED, error_message=err_text)
    if any(k in text for k in _PRIVATE_BANNED_KEYWORDS):
        return ValidationResult(is_valid=False, invalid_reason=InvalidReason.PRIVATE_BANNED, error_message=err_text)
    if http_status and http_status >= 500:
        return ValidationResult(is_valid=True, is_network_error=True, should_retry=True)
    return ValidationResult(is_valid=True, is_network_error=True, should_retry=True, error_message=err_text)


def classify_profile_wall(page_html: str | None) -> str | None:
    """Detect TikTok challenge/login shell pages that return HTTP 200."""
    text = (page_html or "").lower()
    if not text:
        return None
    for needle, reason in _PROFILE_WALL_KEYWORDS:
        if needle in text:
            return reason
    return None


def validate_cookies(cookies_file: str) -> dict:
    """Validate a Netscape-format cookies file for required TikTok cookies."""
    result = {"valid": False, "total": 0, "present": set(), "missing": set(), "expired": set(), "warnings": []}
    if not cookies_file or not os.path.isfile(cookies_file):
        result["warnings"].append("Cookies file not found")
        return result

    now = int(time.time())

    try:
        with open(cookies_file) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or not line:
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    name = parts[5]
                    expiry = int(parts[4]) if parts[4].isdigit() else 0
                    result["total"] += 1
                    result["present"].add(name)
                    if expiry > 0 and expiry < now:
                        result["expired"].add(name)
    except Exception as e:
        result["warnings"].append(f"Parse error: {e}")
        return result

    result["missing"] = REQUIRED_COOKIES - result["present"]
    expired_required = result["expired"] & REQUIRED_COOKIES
    if expired_required:
        result["warnings"].append(f"Expired required cookies: {expired_required}")

    result["valid"] = len(result["missing"]) == 0 and len(expired_required) == 0
    if not result["valid"]:
        missing_recommended = RECOMMENDED_COOKIES - result["present"]
        if missing_recommended:
            result["warnings"].append(f"Missing recommended: {missing_recommended}")

    return result


# ----------------------------------------------------------------------------
# EdgeFetcher: lets ``SpiderDiscover`` walk TikTok's follower/following graph
# without the collector having to care about queue plumbing. Mirrors the
# pattern used by ``GithubEdgeFetcher`` in ``collectors/github.py``.
# ----------------------------------------------------------------------------


class TiktokEdgeFetcher:
    """Adapter exposing TikTok's follower/following graph to ``SpiderDiscover``.

    TikTok exposes following / followers via the un-documented ``user/list``
    endpoint, which we reach via the same gallery-dl / yt-dlp / API fallback
    chain the rest of the collector uses. ``supported_edge_types`` is fixed
    to ``(FOLLOWING,)`` for now — the followers list often requires a logged-in
    session to be readable, so we lean on outbound following edges (the
    creators *they* follow) for related-creator discovery, which TikTok keeps
    public for non-private accounts.
    """

    if EdgeType is not None:  # pragma: no branch — set at import time
        supported_edge_types: tuple = (EdgeType.FOLLOWING,)
    else:  # SpiderDiscover not importable in this env
        supported_edge_types: tuple = ()

    def __init__(self, collector: "TiktokCollector") -> None:
        self._c = collector

    async def fetch_edges(self, node_id: str, edge_type) -> AsyncIterator:
        """Stream ``Edge(source, target, edge_type)`` records.

        ``node_id`` is the TikTok username (without ``@``). We delegate the
        per-username following enumeration to ``collect_following`` which
        knows about cookies / fallbacks; that method yields raw username
        strings that we wrap in ``Edge`` for the spider.
        """
        if Edge is None or EdgeType is None:
            return
        if edge_type not in self.supported_edge_types:
            raise NotImplementedError(f"unsupported edge type: {edge_type}")
        async for follow_username in self._c.collect_following(node_id):
            if not follow_username:
                continue
            yield Edge(source=node_id, target=follow_username, edge_type=edge_type)


# ----------------------------------------------------------------------------
# Main collector
# ----------------------------------------------------------------------------


class TiktokCollector(BaseCollector):
    SOURCE_NAME = "tiktok"

    # Default daily quota — TikTok web throttles aggressively; 500 profile
    # views / day per cookie set is the empirically-safe ceiling per the
    # toolkit's account_manager defaults.
    # Dials pushed (owner accepted higher ban risk): 500 -> 800/day.
    DEFAULT_DAILY_QUOTA = int(os.getenv("TIKTOK_DAILY_QUOTA", "800"))

    @staticmethod
    def _discover_cookie_file() -> str:
        """Return the best per-username cookie file under credentials/tiktok/.

        Bryan's convention: cookies are exported per-account as
        ``tiktok_<username>.txt`` (e.g. ``tiktok_hongyime.txt``). The old
        default was a single ``tiktok_cookies.txt`` stub which is sometimes
        left behind as an empty placeholder — the collector must skip it in
        favor of any real named file.

        Selection strategy:
          1. Enumerate credentials/tiktok/tiktok_*.txt (plus credentials/
             fallback for older layouts).
          2. Reject anything with fewer than 1 KB of content — those are
             empty placeholders or a single-header stub.
          3. Reject the exact name ``tiktok_cookies.txt`` if any named
             siblings exist (Bryan's migration path leaves this stub around).
          4. Return the LARGEST non-rejected file (a full cookie jar with
             session tokens weighs 60–80 KB; a stub with just tracking
             cookies weighs <1 KB). Ties broken alphabetically.

        Never raises — returns '' if nothing qualifies (which triggers the
        collector's "no cookies configured" fallback path).
        """
        import glob
        try:
            candidates = []
            for d in ("credentials/tiktok", "credentials"):
                candidates.extend(glob.glob(os.path.join(d, "tiktok_*.txt")))
            if not candidates:
                return ""
            has_named = any(
                os.path.basename(p) != "tiktok_cookies.txt" for p in candidates
            )
            scored = []
            for p in candidates:
                try:
                    size = os.path.getsize(p)
                except OSError:
                    continue
                if size < 1024:
                    continue  # empty / placeholder / single-header stub
                if has_named and os.path.basename(p) == "tiktok_cookies.txt":
                    continue  # skip the legacy stub when named files exist
                scored.append((size, p))
            if not scored:
                return ""
            # Largest first (most cookies = most complete session), tie-break
            # by path so runs are deterministic.
            scored.sort(key=lambda t: (-t[0], t[1]))
            chosen = scored[0][1]
            logger.info(
                "tiktok: auto-discovered cookie file %s (%d bytes, %d candidates)",
                chosen, scored[0][0], len(candidates),
            )
            return chosen
        except Exception:
            return ""

    def __init__(self):
        super().__init__()
        self._cookies_file = os.getenv("TIKTOK_COOKIES_FILE", "")
        # Auto-discover a per-username cookie file (credentials/tiktok/tiktok_*.txt)
        # when TIKTOK_COOKIES_FILE isn't set, so dropped-in named files work.
        if not self._cookies_file:
            self._cookies_file = self._discover_cookie_file()
        self._session_id = os.getenv("TIKTOK_SESSION_ID", "")
        # Dials pushed (owner accepted higher ban risk): sleep 0.5/2 -> 0.4/1.5.
        self._min_sleep = float(os.getenv("TIKTOK_MIN_SLEEP", "0.4"))
        self._max_sleep = float(os.getenv("TIKTOK_MAX_SLEEP", "1.5"))
        self._retries = int(os.getenv("TIKTOK_RETRIES", "2"))
        self._timeout = int(os.getenv("TIKTOK_TIMEOUT_SECONDS", "300"))
        self._browser_fallback = os.getenv("TIKTOK_BROWSER_FALLBACK_ENABLED", "true").lower() == "true"
        self._ytdlp_fallback = os.getenv("TIKTOK_YTDLP_FALLBACK_ENABLED", "true").lower() == "true"
        self._local_tool_cooldown_until = 0.0
        self._local_tool_cooldown_seconds = int(os.getenv("TIKTOK_LOCAL_TOOL_429_COOLDOWN_SECONDS", "1800"))
        self._local_tool_cooldown_restored = False
        self._profile_metadata_cooldown_until = 0.0
        self._profile_metadata_cooldown_reason = ""
        self._profile_backoff_enabled = os.getenv("TIKTOK_PROFILE_BACKOFF_ENABLED", "true").lower() == "true"
        self._profile_backoff_dir = Path(
            os.getenv(
                "TIKTOK_PROFILE_BACKOFF_DIR",
                str(VAULT_ROOT / "state" / "tiktok" / "profile_backoff"),
            )
        )
        self._profile_empty_backoff_seconds = max(
            0,
            int(os.getenv("TIKTOK_PROFILE_EMPTY_BACKOFF_SECONDS", "7200") or "0"),
        )
        self._profile_failure_backoff_seconds = max(
            0,
            int(os.getenv("TIKTOK_PROFILE_FAILURE_BACKOFF_SECONDS", "21600") or "0"),
        )
        self._profile_timeout_backoff_seconds = max(
            0,
            int(os.getenv("TIKTOK_PROFILE_TIMEOUT_BACKOFF_SECONDS", "14400") or "0"),
        )
        self._gallery_dl_archive_enabled = (
            os.getenv("TIKTOK_GALLERY_DL_ARCHIVE_ENABLED", "true").lower() == "true"
        )
        self._gallery_dl_archive_dir = Path(
            os.getenv(
                "TIKTOK_GALLERY_DL_ARCHIVE_DIR",
                str(VAULT_ROOT / "state" / "tiktok" / "gallery_dl_archives"),
            )
        )
        self._gallery_dl_range_cursor_enabled = (
            os.getenv("TIKTOK_GALLERY_DL_RANGE_CURSOR_ENABLED", "true").lower() == "true"
        )
        self._gallery_dl_range_dir = Path(
            os.getenv(
                "TIKTOK_GALLERY_DL_RANGE_DIR",
                str(VAULT_ROOT / "state" / "tiktok" / "gallery_dl_ranges"),
            )
        )
        self._gallery_dl_empty_ranges_before_reset = max(
            1,
            int(os.getenv("TIKTOK_GALLERY_DL_EMPTY_RANGES_BEFORE_RESET", "2") or "2"),
        )
        self._gallery_dl_range_max_start = max(
            0,
            int(os.getenv("TIKTOK_GALLERY_DL_RANGE_MAX_START", "3000") or "0"),
        )
        self._use_gallery_dl = (
            self._check_tool("gallery-dl")
            and os.getenv("TIKTOK_GALLERY_DL_ENABLED", "true").lower() == "true"
        )
        self._use_yt_dlp = self._check_tool("yt-dlp")
        logger.info(
            "tiktok tool availability: gallery-dl=%s yt-dlp=%s browser_fallback=%s ytdlp_fallback=%s",
            self._use_gallery_dl, self._use_yt_dlp, self._browser_fallback, self._ytdlp_fallback,
        )
        # Dial pushed 2 -> 3 (owner accepted higher ban risk). Still serialized
        # enough to look human; back off if challenges spike.
        self._sem = asyncio.Semaphore(int(os.getenv("TIKTOK_DOWNLOAD_CONCURRENCY", "3")))
        self._cookies_valid = False
        self._tracker_file = Path(os.getenv("TIKTOK_TRACKER_FILE", "data/tiktok_tracker.json"))
        self._tracked_ids: set[str] = set()
        # FAMOUS-FILTER (Bryan): older hard skip knob kept for compatibility.
        # New default behavior is profile-first/profile-only via
        # TIKTOK_VIDEO_FOLLOWER_CAP below.
        self._famous_follower_cap = int(os.getenv("TIKTOK_FAMOUS_FOLLOWER_CAP", "0") or "0")
        self._video_follower_cap = int(
            os.getenv(
                "TIKTOK_VIDEO_FOLLOWER_CAP",
                os.getenv("TIKTOK_PROFILE_VIDEO_FOLLOWER_CAP", "300"),
            )
            or "0"
        )
        self._profile_chunk_items = max(0, int(os.getenv("TIKTOK_PROFILE_CHUNK_ITEMS", "60") or "0"))
        self._gallery_dl_max_items = max(
            0,
            int(os.getenv("TIKTOK_GALLERY_DL_MAX_ITEMS", str(self._profile_chunk_items)) or "0"),
        )
        self._yt_dlp_max_downloads = max(
            0,
            int(os.getenv("TIKTOK_YTDLP_MAX_DOWNLOADS", str(self._profile_chunk_items)) or "0"),
        )
        self._yt_dlp_after_empty_gallery = (
            os.getenv("TIKTOK_YTDLP_AFTER_EMPTY_GALLERY", "false").lower() == "true"
        )
        self._yt_dlp_after_gallery_timeout = (
            os.getenv("TIKTOK_YTDLP_AFTER_GALLERY_TIMEOUT", "false").lower() == "true"
        )
        self._last_gallery_dl_empty_user: str | None = None
        self._last_gallery_dl_timeout_user: str | None = None
        self._last_gallery_dl_range_by_user: dict[str, tuple[int, int]] = {}
        self._target_limit_per_cycle = max(0, int(os.getenv("TIKTOK_TARGETS_PER_CYCLE", "60")))
        self._cooldown_profile_only_per_cycle = max(
            0,
            int(os.getenv("TIKTOK_COOLDOWN_PROFILE_ONLY_PER_CYCLE", "25") or "0"),
        )
        self._spider_queue_per_cycle = max(0, int(os.getenv("TIKTOK_SPIDER_QUEUE_PER_CYCLE", "80")))
        self._spider_first = os.getenv("TIKTOK_SPIDER_QUEUE_FIRST", "true").lower() == "true"
        self._spider_processing_stale_minutes = max(
            5,
            int(os.getenv("TIKTOK_SPIDER_PROCESSING_STALE_MINUTES", "45") or "45"),
        )

        # Follow-aware access tracker (Phase 0, lazy — needs self.pool, created
        # on first use in _record_profile_access). Records every profile fetch
        # outcome into profile_access_{summary,attempts} so SmartAccountSelector
        # can later route a private target to a cookie identity that can see it.
        # Enable/disable via TIKTOK_ACCESS_TRACKING (default on).
        self._access_repo = None
        self._access_tracking = os.getenv("TIKTOK_ACCESS_TRACKING", "1") == "1"

        # account_quota: register a daily cap so the scheduler can refuse new
        # work once we've hit it. ``has_quota`` on a missing config is a
        # no-op so this stays safe even if the same tracker is imported
        # from another collector first.
        self._quota = AccountQuotaTracker() if AccountQuotaTracker is not None else None
        if self._quota is not None and QuotaConfig is not None:
            try:
                self._quota.register(
                    "tiktok",
                    QuotaConfig(daily_limit=self.DEFAULT_DAILY_QUOTA),
                )
            except Exception:  # noqa: BLE001 — registration is local-only
                logger.debug("tiktok: quota registration skipped", exc_info=True)

        if self._cookies_file:
            result = validate_cookies(self._cookies_file)
            self._cookies_valid = result["valid"]
            if not result["valid"]:
                for w in result.get("warnings", []):
                    logger.warning("TikTok cookie issue: %s", w)
                if result["missing"]:
                    logger.warning("Missing required cookies: %s", result["missing"])
            else:
                logger.info("TikTok cookies validated: %d cookies, all required present", result["total"])

    @staticmethod
    def _check_tool(name: str) -> bool:
        try:
            subprocess.run([name, "--version"], capture_output=True, check=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            return False

    def _safe_tiktok_state_user(self, username: str) -> str:
        return sanitize_name(username or "unknown")[:120] or "unknown"

    def _gallery_dl_range_state_path(self, username: str) -> Path:
        return self._gallery_dl_range_dir / f"{self._safe_tiktok_state_user(username)}.json"

    def _read_gallery_dl_range_state(self, username: str) -> dict:
        path = self._gallery_dl_range_state_path(username)
        try:
            if not path.exists():
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            logger.debug("tiktok: gallery-dl range state unreadable for %s", username, exc_info=True)
            return {}

    def _write_gallery_dl_range_state(self, username: str, state: dict) -> None:
        try:
            self._gallery_dl_range_dir.mkdir(parents=True, exist_ok=True)
            path = self._gallery_dl_range_state_path(username)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            logger.debug("tiktok: gallery-dl range state write failed for %s", username, exc_info=True)

    def _profile_backoff_state_path(self, username: str) -> Path:
        return self._profile_backoff_dir / f"{self._safe_tiktok_state_user(username)}.json"

    def _read_profile_backoff_state(self, username: str) -> dict:
        if not self._profile_backoff_enabled:
            return {}
        path = self._profile_backoff_state_path(username)
        try:
            if not path.exists():
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            logger.debug("tiktok: profile backoff state unreadable for %s", username, exc_info=True)
            return {}

    def _profile_backoff_remaining(self, username: str) -> int:
        state = self._read_profile_backoff_state(username)
        until = state.get("cooldown_until")
        if not until:
            return 0
        try:
            if isinstance(until, (int, float)):
                until_dt = datetime.fromtimestamp(float(until), tz=timezone.utc)
            else:
                until_dt = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
                if until_dt.tzinfo is None:
                    until_dt = until_dt.replace(tzinfo=timezone.utc)
            remaining = (until_dt.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
            return max(0, int(remaining))
        except Exception:
            logger.debug("tiktok: profile backoff timestamp invalid for %s", username, exc_info=True)
            return 0

    def _record_profile_backoff(self, username: str, *, reason: str, seconds: int) -> None:
        if not self._profile_backoff_enabled or seconds <= 0:
            return
        try:
            self._profile_backoff_dir.mkdir(parents=True, exist_ok=True)
            existing = self._read_profile_backoff_state(username)
            failures = int(existing.get("failures") or 0) + 1
            until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
            state = {
                "cooldown_until": until.isoformat(),
                "failures": failures,
                "last_reason": reason,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            path = self._profile_backoff_state_path(username)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
            logger.info(
                "tiktok: delayed local fallback for %s for %ds after %s",
                username,
                seconds,
                reason,
            )
        except Exception:
            logger.debug("tiktok: profile backoff state write failed for %s", username, exc_info=True)

    def _gallery_dl_range_window(self, username: str) -> tuple[int, int] | None:
        if not self._gallery_dl_range_cursor_enabled or not self._gallery_dl_max_items:
            return None
        state = self._read_gallery_dl_range_state(username)
        start = self._safe_int(state.get("next_start"), 1)
        if start < 1 or (self._gallery_dl_range_max_start and start > self._gallery_dl_range_max_start):
            start = 1
        end = start + self._gallery_dl_max_items - 1
        self._last_gallery_dl_range_by_user[username] = (start, end)
        return start, end

    def _advance_gallery_dl_range_cursor(self, username: str, *, file_count: int, ok: bool) -> None:
        window = self._last_gallery_dl_range_by_user.get(username)
        if not window or not self._gallery_dl_range_cursor_enabled:
            return
        start, end = window
        state = self._read_gallery_dl_range_state(username)
        empty_ranges = self._safe_int(state.get("empty_ranges"), 0)
        if file_count > 0:
            next_start = end + 1
            empty_ranges = 0
        elif ok:
            empty_ranges += 1
            next_start = end + 1 if empty_ranges < self._gallery_dl_empty_ranges_before_reset else 1
            if next_start == 1:
                empty_ranges = 0
        else:
            return
        if self._gallery_dl_range_max_start and next_start > self._gallery_dl_range_max_start:
            next_start = 1
            empty_ranges = 0
        self._write_gallery_dl_range_state(
            username,
            {
                "next_start": next_start,
                "empty_ranges": empty_ranges,
                "last_start": start,
                "last_end": end,
                "last_file_count": int(file_count),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def _gallery_dl_archive_args(self, username: str) -> list[str]:
        """Use gallery-dl's archive to avoid re-downloading known profile media.

        DB-level duplicate checks happen after gallery-dl downloads into a tempdir,
        so a timed-out large profile can otherwise spend every cycle fetching the
        same newest files. A per-username archive lets gallery-dl skip files it has
        already fetched and continue deeper on later cycles.
        """
        if not self._gallery_dl_archive_enabled:
            return []
        safe_user = self._safe_tiktok_state_user(username)
        try:
            self._gallery_dl_archive_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.debug(
                "tiktok: gallery-dl archive dir unavailable: %s",
                self._gallery_dl_archive_dir,
                exc_info=True,
            )
            return []
        archive = self._gallery_dl_archive_dir / f"{safe_user}.txt"
        args = ["--download-archive", str(archive)]
        range_window = self._gallery_dl_range_window(username)
        if range_window:
            args.extend(["--range", f"{range_window[0]}-{range_window[1]}"])
        elif self._gallery_dl_max_items:
            args.extend(["--range", f"1-{self._gallery_dl_max_items}"])
        return args

    @property
    def account_media_dir(self) -> Path:
        if self._cookies_file:
            acc_name = Path(self._cookies_file).stem
            path = self.media_dir / f"account_{sanitize_name(acc_name)}"
        else:
            path = self.media_dir / "default"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _account_name(self) -> str:
        return Path(self._cookies_file).stem if self._cookies_file else "tiktok_default"

    def _local_tool_cooling_down(self) -> bool:
        return time.time() < self._local_tool_cooldown_until

    def _profile_metadata_cooling_down(self) -> bool:
        return time.time() < self._profile_metadata_cooldown_until

    def _start_profile_metadata_cooldown(self, reason: str, cooldown_seconds: int | None = None) -> None:
        seconds = int(cooldown_seconds or self._local_tool_cooldown_seconds or 0)
        if seconds <= 0:
            return
        until = time.time() + seconds
        self._profile_metadata_cooldown_until = max(
            self._profile_metadata_cooldown_until,
            until,
        )
        self._profile_metadata_cooldown_reason = reason

    async def _sync_persisted_local_tool_cooldown(self) -> None:
        if self._local_tool_cooldown_restored or self.pool is None:
            return
        self._local_tool_cooldown_restored = True
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT created_at, cooldown_seconds, reason, scope
                    FROM rate_limit_events
                    WHERE source = 'tiktok'
                      AND account = $1
                      AND (status_code = 429 OR status_code IS NULL OR scope = 'profile_metadata')
                      AND cooldown_seconds IS NOT NULL
                      AND created_at + (cooldown_seconds * INTERVAL '1 second') > NOW()
                    ORDER BY created_at DESC
                    LIMIT 10
                    """,
                    self._account_name(),
                )
        except Exception as exc:
            logger.debug("tiktok: persisted local-tool cooldown check failed: %s", exc)
            return
        if not rows:
            return
        restored_any = False
        for row in rows:
            created_at = row["created_at"]
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            remaining = (
                created_at.astimezone(timezone.utc)
                + timedelta(seconds=int(row["cooldown_seconds"] or 0))
                - datetime.now(timezone.utc)
            ).total_seconds()
            if remaining <= 0:
                continue
            if row["scope"] == "profile_metadata":
                self._profile_metadata_cooldown_until = max(
                    self._profile_metadata_cooldown_until,
                    time.time() + remaining,
                )
                self._profile_metadata_cooldown_reason = row["reason"] or "profile_metadata"
                logger.info(
                    "tiktok: restored profile metadata cooldown for %ds (%s)",
                    int(remaining),
                    row["reason"] or "profile_metadata",
                )
            else:
                self._local_tool_cooldown_until = max(
                    self._local_tool_cooldown_until,
                    time.time() + remaining,
                )
                logger.info(
                    "tiktok: restored local tool cooldown for %ds (%s)",
                    int(remaining),
                    row["reason"] or "rate-limit",
                )
            restored_any = True
        if not restored_any:
            return

    async def _record_local_rate_limit_event(
        self,
        *,
        username: str,
        tool: str,
        result,
    ) -> None:
        text = f"{result.stderr or ''}\n{result.stdout or ''}"
        status_code = 429 if "429" in text else None
        validation = classify_invalid_username(text, http_status=status_code)
        if not validation.is_rate_limited:
            return

        cooldown_seconds = self._local_tool_cooldown_seconds
        remaining = getattr(self.rate_limiter, "get_cooldown_remaining", None)
        if callable(remaining):
            try:
                cooldown_seconds = int(remaining("tiktok.com") or 0) or cooldown_seconds
            except Exception:
                cooldown_seconds = self._local_tool_cooldown_seconds
        self._local_tool_cooldown_until = max(
            self._local_tool_cooldown_until,
            time.time() + cooldown_seconds,
        )

        await record_rate_limit_event(
            self.pool,
            source="tiktok",
            account=self._account_name(),
            scope=f"{tool}_local",
            status_code=status_code,
            cooldown_seconds=cooldown_seconds,
            reason="local tool output matched rate-limit signature",
            metadata={
                "username": username,
                "tool": tool,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "file_count": result.file_count,
                "stderr": result.err_summary(800),
                "stdout": (result.stdout or "")[:400],
            },
        )

    async def collect(self, targets: list[str]):
        await self._load_tracker_state()
        await self._sync_persisted_local_tool_cooldown()
        spider_enabled = os.getenv("TIKTOK_SPIDER_ENABLED", "true").lower() == "true"
        if spider_enabled and self._spider_first:
            await self._process_spider_queue(max_items=self._spider_queue_per_cycle)
        cycle_targets = targets
        if self._target_limit_per_cycle:
            cycle_targets = targets[:self._target_limit_per_cycle]
            if len(targets) > len(cycle_targets):
                logger.info(
                    "tiktok: processing %d/%d configured targets this cycle",
                    len(cycle_targets), len(targets),
                )
        cooldown_profile_attempts = 0
        for username in cycle_targets:
            if self._stop.is_set(): break
            if self._local_tool_cooling_down():
                remaining = int(self._local_tool_cooldown_until - time.time())
                self._intentional_idle_reason = (
                    f"TikTok local downloader cooldown active for {self._account_name()} "
                    f"({remaining}s remaining)"
                )
                if self._profile_metadata_cooling_down():
                    meta_remaining = int(self._profile_metadata_cooldown_until - time.time())
                    reason = self._profile_metadata_cooldown_reason or "profile metadata cooldown"
                    logger.info(
                        "tiktok: %s; profile metadata also cooling down (%ds remaining; %s)",
                        self._intentional_idle_reason,
                        meta_remaining,
                        reason,
                    )
                    break
                if cooldown_profile_attempts >= self._cooldown_profile_only_per_cycle:
                    logger.info(
                        "tiktok: %s; profile-only cooldown budget exhausted (%d)",
                        self._intentional_idle_reason,
                        self._cooldown_profile_only_per_cycle,
                    )
                    break
                username = username.lstrip("@")
                if self._is_invalid_username(username):
                    continue
                cooldown_profile_attempts += 1
                logger.info(
                    "tiktok: %s; refreshing profile-only for %s (%d/%d)",
                    self._intentional_idle_reason,
                    username,
                    cooldown_profile_attempts,
                    self._cooldown_profile_only_per_cycle,
                )
                try:
                    profile_id = await self.collect_user_profile(username)
                    if profile_id:
                        self._progress_count += 1
                        await self.checkpoint.save_progress(username)
                except Exception as e:
                    logger.warning("tiktok profile-only cooldown refresh failed for %s: %s", username, e)
                    await self.send_to_dlq(username, username, str(e))
                await asyncio.sleep(random.uniform(self._min_sleep, self._max_sleep))
                continue
            username = username.lstrip("@")
            if self._is_invalid_username(username): continue

            logger.info("Collecting tiktok/%s", username)
            try:
                await self._collect_user(username)
                await self.checkpoint.save_progress(username)
            except Exception as e:
                logger.error("Failed tiktok/%s: %s", username, e)
                await self.send_to_dlq(username, username, str(e))

        # Spider queue processing
        if spider_enabled and not self._spider_first:
            await self._process_spider_queue(max_items=self._spider_queue_per_cycle)

    async def _process_spider_queue(self, max_items: int | None = None):
        await refresh_account_proximity_cache(self.pool)
        await self._reclaim_stale_spider_queue()
        processed = 0
        while not self._stop.is_set():
            if max_items is not None and max_items > 0 and processed >= max_items:
                logger.info("tiktok spider queue: processed %d queued target(s) this cycle", processed)
                break
            if self._local_tool_cooling_down():
                remaining = int(self._local_tool_cooldown_until - time.time())
                self._intentional_idle_reason = (
                    f"TikTok local downloader cooldown active for {self._account_name()} "
                    f"({remaining}s remaining)"
                )
                logger.info("tiktok spider queue: %s", self._intentional_idle_reason)
                break
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    UPDATE tiktok_spider_queue
                    SET status = 'processing', collected_at = NOW()
                    WHERE id = (
                        SELECT q.id
                        FROM tiktok_spider_queue q
                        LEFT JOIN LATERAL (
                            SELECT MIN(ap.tier) AS proximity_tier
                            FROM account_proximity_cache ap
                            WHERE ap.platform = 'tiktok'
                              AND (
                                     ap.account_id = q.platform_user_id
                                  OR ap.account_id = lower(q.username)
                              )
                        ) prox ON TRUE
                        WHERE q.status = 'pending'
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
                    RETURNING id, platform_user_id, username
                """)
            if not row: break
            target = row['username'] or row['platform_user_id']
            try:
                result = await self._collect_user(target)
                processed += 1
                if result in {"unavailable", "private"}:
                    status = "unavailable"
                elif result in {"delayed", "cooldown", "empty"}:
                    status = "delayed"
                else:
                    status = "completed"
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE tiktok_spider_queue
                        SET status = $2, collected_at = NOW()
                        WHERE id = $1
                        """,
                        row["id"],
                        status,
                    )
            except Exception:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE tiktok_spider_queue SET status = 'failed', collected_at = NOW() WHERE id = $1",
                        row["id"],
                    )

    async def _reclaim_stale_spider_queue(self) -> None:
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE tiktok_spider_queue
                    SET status = 'pending'
                    WHERE status = 'processing'
                      AND COALESCE(collected_at, NOW() - INTERVAL '100 years')
                          < NOW() - ($1::int * INTERVAL '1 minute')
                    """,
                    self._spider_processing_stale_minutes,
                )
            if not result.endswith(" 0"):
                logger.info("tiktok spider queue: reclaimed stale processing rows (%s)", result)
        except Exception:
            logger.debug("tiktok spider queue: stale processing reclaim failed", exc_info=True)

    @staticmethod
    def _is_invalid_username(username: str) -> bool:
        """Cheap pre-flight format check. Use ``validate_username`` for the
        canonical strict version; this stays for back-compat with code paths
        that want a bool rather than an exception."""
        try:
            validate_username(username)
            return False
        except Exception:
            return True

    async def _record_profile_access(
        self,
        username: str,
        can_access: bool,
        is_private: bool | None = None,
        is_followed: bool = False,
        error: str | None = None,
    ) -> None:
        """Record whether the current cookie identity could see this target into
        profile_access_{summary,attempts} (follow-aware selector, Phase 0).

        Mirrors instagram's _record_profile_access. Best-effort and fully
        isolated: any failure here is swallowed (debug-logged) so collection is
        never affected. No new network calls — it only persists the outcome of
        a fetch the collector already made.

        Unlike Instagram there is no per-request account pool: TikTok runs a
        single cookie identity per collector instance, so ``account`` is the
        cookie-file stem (same convention as the quota tracker) or the stable
        literal "tiktok_default" when no cookie jar is configured.
        """
        if not self._access_tracking or ProfileAccessRepository is None:
            return
        if self.pool is None:
            return
        try:
            if self._access_repo is None:
                self._access_repo = ProfileAccessRepository(self.pool)
            account = (
                Path(self._cookies_file).stem if self._cookies_file else "tiktok_default"
            )
            await self._access_repo.record_attempt(
                source="tiktok",
                target_id=str(username),
                account=account,
                can_access=can_access,
                is_public=(None if is_private is None else (not is_private)),
                is_followed=is_followed,
                error=error,
            )
        except Exception as e:
            logger.debug("tiktok: access-tracking record failed for %s: %s", username, e)

    async def _stored_followers_count(self, username: str) -> int | None:
        if self.pool is None:
            return None
        try:
            async with self.pool.acquire() as conn:
                fc = await conn.fetchval(
                    """
                    SELECT followers_count
                    FROM tiktok_profiles
                    WHERE LOWER(username) = LOWER($1)
                    ORDER BY updated_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    username,
                )
            return int(fc) if fc is not None else None
        except Exception:
            logger.debug("tiktok follower-cap check failed for %s", username, exc_info=True)
            return None

    async def _collect_user(self, username: str) -> str:
        profile_url = f"https://www.tiktok.com/@{username}"
        self._last_gallery_dl_empty_user = None
        self._last_gallery_dl_timeout_user = None
        known_followers = await self._stored_followers_count(username)
        if self._profile_metadata_cooling_down():
            remaining = int(self._profile_metadata_cooldown_until - time.time())
            reason = self._profile_metadata_cooldown_reason or "profile metadata cooldown"
            logger.info(
                "tiktok: profile metadata cooldown active for %s (%ds remaining; %s); "
                "trying media fallback without metadata fetch",
                self._account_name(),
                remaining,
                reason,
            )
            metadata = {"status": "metadata_cooldown"}
        else:
            metadata = await self._scrape_profile_metadata(username)
        status = str(metadata.get("status") or "")
        if status in {"unavailable", "not_found", "deleted", "account_deleted", "username_changed"}:
            await self._record_profile_access(username, False, error=status)
            return "unavailable"
        if status == "private" or metadata.get("is_private") is True:
            await self._record_profile_access(username, False, is_private=True, error="private")
            logger.info("tiktok: profile-only private/unavailable target %s", username)
            return "private"

        followers_count = metadata.get("followers_count")
        if followers_count is None:
            followers_count = known_followers
        try:
            followers_count = int(followers_count) if followers_count is not None else None
        except Exception:
            followers_count = None

        cap = self._video_follower_cap or self._famous_follower_cap
        if cap and followers_count is not None and followers_count > cap:
            logger.info(
                "tiktok: profile-only skip videos for %s (%d followers > cap %d)",
                username,
                followers_count,
                cap,
            )
            await self._record_profile_access(username, True, is_private=False)
            return "profile_only"

        backoff_remaining = self._profile_backoff_remaining(username)
        if backoff_remaining > 0:
            logger.info(
                "tiktok: profile local fallback backoff active for %s (%ds remaining)",
                username,
                backoff_remaining,
            )
            return "delayed"

        if self._use_gallery_dl:
            try:
                ok = await self._collect_via_gallery_dl(username, profile_url)
            except asyncio.TimeoutError:
                logger.warning("tiktok: gallery-dl coroutine timed out for %s", username)
                ok = False
            if ok:
                # Success: this cookie identity CAN see the target — record for
                # the follow-aware selector (Phase 0). is_private isn't known at
                # this boundary (it lives in the per-post sidecars already
                # upserted by _ingest_tmpdir), so is_public stays unchanged.
                await self._record_profile_access(username, True)
                return "collected"
            if self._local_tool_cooling_down():
                logger.info(
                    "tiktok: local downloader cooldown started for %s; skipping remaining fallbacks for %s",
                    self._account_name(),
                    username,
                )
                return "cooldown"

        if self._use_yt_dlp and self._ytdlp_fallback:
            if self._last_gallery_dl_empty_user == username and not self._yt_dlp_after_empty_gallery:
                logger.info(
                    "tiktok: gallery-dl returned a clean empty range for %s; skipping yt-dlp fallback",
                    username,
                )
            elif self._last_gallery_dl_timeout_user == username and not self._yt_dlp_after_gallery_timeout:
                logger.info(
                    "tiktok: gallery-dl timed out with no files for %s; skipping yt-dlp this cycle",
                    username,
                )
            else:
                try:
                    ok = await self._collect_via_yt_dlp(username, profile_url)
                except asyncio.TimeoutError:
                    logger.warning("tiktok: yt-dlp coroutine timed out for %s", username)
                    ok = False
                if ok:
                    # Success (see gallery-dl note above): record can_access=True.
                    await self._record_profile_access(username, True)
                    return "collected"
                if self._local_tool_cooling_down():
                    logger.info(
                        "tiktok: local downloader cooldown started for %s; skipping remaining fallbacks for %s",
                        self._account_name(),
                        username,
                    )
                    return "cooldown"

        if self._browser_fallback:
            if await self._collect_via_playwright(username):
                # Success via browser fallback: record can_access=True.
                await self._record_profile_access(username, True)
                return "collected"
        # NOTE (Phase 0): no can_access=False call site is wired here on
        # purpose. At this boundary a failed gallery-dl/yt-dlp/playwright chain
        # cannot be distinguished from "account has zero public posts" or a
        # transient tool failure. Only unambiguous successes are recorded.
        if await self._collect_via_api(username):
            await self._record_profile_access(username, True)
            return "collected"
        if self._last_gallery_dl_timeout_user == username:
            return "delayed"
        if status == "ok":
            return "empty"
        return "delayed"

    async def _scrape_profile_metadata(self, username: str) -> dict:
        """Try to fetch and save profile metadata to DB."""
        try:
            username = validate_username(username)
        except ValueError as exc:
            return {"status": "unavailable", "error": str(exc)}

        await self.wait_rate_limit("tiktok.com")
        cookies = {"sessionid": self._session_id} if self._session_id else {}
        headers = {
            "User-Agent": self.user_agents.get_for_domain("tiktok.com"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.tiktok.com/",
        }
        try:
            async with httpx.AsyncClient(timeout=30, cookies=cookies, follow_redirects=True) as client:
                resp = await client.get(f"https://www.tiktok.com/@{username}", headers=headers)
            validation = classify_invalid_username(resp.text, http_status=resp.status_code)
            if not validation.is_valid:
                reason = validation.invalid_reason.value if validation.invalid_reason else "unavailable"
                return {
                    "status": "private" if validation.invalid_reason == InvalidReason.PRIVATE_BANNED else reason,
                    "error": validation.error_message,
                }
            if validation.is_rate_limited:
                self._start_profile_metadata_cooldown(
                    "TikTok profile metadata HTTP rate-limit",
                    self._local_tool_cooldown_seconds,
                )
                await record_rate_limit_event(
                    self.pool,
                    source="tiktok",
                    account=self._account_name(),
                    scope="profile_metadata",
                    status_code=resp.status_code,
                    cooldown_seconds=self._local_tool_cooldown_seconds,
                    reason="TikTok profile metadata HTTP rate-limit",
                    metadata={"username": username},
                )
                return {"status": "delayed", "http_status": resp.status_code}
            resp.raise_for_status()
        except Exception as exc:
            logger.debug("tiktok: metadata fetch failed for %s: %s", username, exc)
            return {"status": "delayed", "error": str(exc)}

        author, stats = self._extract_profile_from_html(resp.text, username)
        if not author:
            validation = classify_invalid_username(resp.text, http_status=resp.status_code)
            if not validation.is_valid:
                reason = validation.invalid_reason.value if validation.invalid_reason else "unavailable"
                return {
                    "status": "private" if validation.invalid_reason == InvalidReason.PRIVATE_BANNED else reason,
                    "error": validation.error_message,
                }
            wall_reason = classify_profile_wall(resp.text)
            if wall_reason:
                reason = f"TikTok profile metadata challenge wall: {wall_reason}"
                self._start_profile_metadata_cooldown(reason, self._local_tool_cooldown_seconds)
                await record_rate_limit_event(
                    self.pool,
                    source="tiktok",
                    account=self._account_name(),
                    scope="profile_metadata",
                    status_code=resp.status_code,
                    cooldown_seconds=self._local_tool_cooldown_seconds,
                    reason=reason,
                    metadata={"username": username, "wall_reason": wall_reason},
                )
                return {
                    "status": "delayed",
                    "http_status": resp.status_code,
                    "error": f"profile_wall:{wall_reason}",
                }
            return {"status": "missing"}

        profile_uuid = await self._upsert_profile(author, stats) if self.pool is not None else None
        followers = None
        if isinstance(stats, dict):
            followers = stats.get("followerCount")
        if followers is None:
            followers = author.get("followerCount") or author.get("followers")
        followers = self._safe_int(followers, default=-1)
        is_private = self._safe_bool(author.get("privateAccount", False))
        return {
            "status": "private" if is_private else "ok",
            "profile_uuid": profile_uuid,
            "followers_count": None if followers < 0 else followers,
            "is_private": is_private,
        }

    @staticmethod
    def _iter_embedded_json(page_html: str):
        for match in re.finditer(
            r"<script[^>]+id=[\"'](?:SIGI_STATE|__UNIVERSAL_DATA_FOR_REHYDRATION__|__NEXT_DATA__)[\"'][^>]*>(.*?)</script>",
            page_html,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            body = html_lib.unescape(match.group(1).strip())
            if not body:
                continue
            try:
                yield json.loads(body)
            except Exception:
                continue

    @classmethod
    def _extract_profile_from_html(cls, page_html: str, username: str) -> tuple[dict | None, dict | None]:
        for state in cls._iter_embedded_json(page_html):
            author, stats = cls._extract_profile_from_state(state, username)
            if author:
                return author, stats
        return None, None

    @classmethod
    def _extract_profile_from_state(cls, state, username: str) -> tuple[dict | None, dict | None]:
        target = (username or "").strip().lstrip("@").lower()
        stack = [state]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                user_info = node.get("userInfo")
                if isinstance(user_info, dict):
                    user = user_info.get("user")
                    stats = user_info.get("stats")
                    if cls._state_user_matches(user, target):
                        return user, stats if isinstance(stats, dict) else None

                user_module = node.get("UserModule")
                if isinstance(user_module, dict):
                    users = user_module.get("users")
                    stats_map = user_module.get("stats")
                    if isinstance(users, dict):
                        for user_id, user in users.items():
                            if cls._state_user_matches(user, target):
                                stats = stats_map.get(user_id) if isinstance(stats_map, dict) else None
                                return user, stats if isinstance(stats, dict) else None

                if cls._state_user_matches(node, target):
                    stats = node.get("stats") if isinstance(node.get("stats"), dict) else None
                    return node, stats

                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        return None, None

    @staticmethod
    def _state_user_matches(user, target: str) -> bool:
        if not isinstance(user, dict):
            return False
        unique_id = user.get("uniqueId") or user.get("unique_id") or user.get("username")
        return str(unique_id or "").strip().lstrip("@").lower() == target

    @staticmethod
    def _safe_int(value, default: int = 0) -> int:
        return _parse_safe_int(value, default)

    @staticmethod
    def _to_dt(value):
        """Coerce a unix timestamp (int or str) to aware datetime; None on failure."""
        return _parse_to_dt(value)

    @staticmethod
    def _clean_username(value) -> str | None:
        if value is None:
            return None
        text = str(value).strip().lstrip("@")
        return text or None

    @staticmethod
    def _safe_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "t", "yes", "y"}
        return bool(value)

    async def _upsert_profile(self, author: dict, author_stats: dict | None = None) -> str | None:
        """Upsert a tiktok_profiles row from a sidecar's author/authorStats blocks.

        Returns the profile UUID (str) on success, or None.
        """
        if not isinstance(author, dict):
            return None
        platform_user_id = author.get("id")
        if not platform_user_id:
            return None
        stats = author_stats or {}
        # Some sidecars nest stats under author itself; tolerate both.
        if not stats and isinstance(author.get("stats"), dict):
            stats = author["stats"]

        avatar = author.get("avatarLarger") or author.get("avatarMedium") or author.get("avatarThumb")

        # ── User-intelligence diff (Tier 4): snapshot the row BEFORE upserting
        # so UserChangeTracker can compare old → new and emit one row per
        # changed field into tiktok_user_changes. Wrapped in try/except so any
        # failure (DB, schema drift, etc.) is non-fatal to ingestion.
        prev_row = None
        try:
            async with self.pool.acquire() as conn:
                prev_row = await conn.fetchrow(
                    "SELECT username, nickname, bio, avatar_url, "
                    "following_count, followers_count, heart_count, "
                    "video_count, digg_count, is_verified, is_private "
                    "FROM tiktok_profiles WHERE platform_user_id = $1",
                    str(platform_user_id),
                )
        except Exception as exc:
            logger.debug("user_change_tracker[tiktok]: prev-row fetch failed: %s", exc)

        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO tiktok_profiles (
                        platform_user_id, username, nickname, avatar_url, bio,
                        following_count, followers_count, heart_count, video_count,
                        digg_count, is_verified, is_private, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NOW())
                    ON CONFLICT (platform_user_id) DO UPDATE SET
                        username = COALESCE(EXCLUDED.username, tiktok_profiles.username),
                        nickname = COALESCE(EXCLUDED.nickname, tiktok_profiles.nickname),
                        avatar_url = COALESCE(EXCLUDED.avatar_url, tiktok_profiles.avatar_url),
                        bio = COALESCE(EXCLUDED.bio, tiktok_profiles.bio),
                        following_count = EXCLUDED.following_count,
                        followers_count = EXCLUDED.followers_count,
                        heart_count = EXCLUDED.heart_count,
                        video_count = EXCLUDED.video_count,
                        digg_count = EXCLUDED.digg_count,
                        is_verified = EXCLUDED.is_verified,
                        is_private = EXCLUDED.is_private,
                        updated_at = NOW()
                    RETURNING id
                """,
                str(platform_user_id),
                author.get("uniqueId"),
                author.get("nickname"),
                avatar,
                author.get("signature"),
                self._safe_int(stats.get("followingCount")),
                self._safe_int(stats.get("followerCount")),
                self._safe_int(stats.get("heartCount") or stats.get("heart")),
                self._safe_int(stats.get("videoCount")),
                self._safe_int(stats.get("diggCount")),
                bool(author.get("verified", False)),
                bool(author.get("privateAccount", False)),
                )
        except Exception as e:
            logger.warning("tiktok _upsert_profile failed for %s: %s", platform_user_id, e)
            return None

        # ── Change-log write (non-fatal, after the upsert connection is
        # released). Field names match the tiktok_profiles column names, so
        # prev_row passes through unmodified. Count fields are snapshotted as
        # None when the sidecar carried no stats block, so a stats-less
        # payload can't log a bogus "N → 0" drop.
        try:
            tracker = UserChangeTracker(self.pool)
            new_snapshot = {
                "username":        author.get("uniqueId"),
                "nickname":        author.get("nickname"),
                "bio":             author.get("signature"),
                "avatar_url":      avatar,
                "followers_count": self._safe_int(stats.get("followerCount")) if stats else None,
                "following_count": self._safe_int(stats.get("followingCount")) if stats else None,
                "heart_count":     self._safe_int(stats.get("heartCount") or stats.get("heart")) if stats else None,
                "video_count":     self._safe_int(stats.get("videoCount")) if stats else None,
                "digg_count":      self._safe_int(stats.get("diggCount")) if stats else None,
                "is_verified":     bool(author.get("verified", False)),
                "is_private":      bool(author.get("privateAccount", False)),
            }
            await tracker.detect_and_log(
                table="tiktok_user_changes",
                pk_col="user_id",
                pk_val=str(platform_user_id),
                current_row=dict(prev_row) if prev_row is not None else None,
                new_row=new_snapshot,
                fields=TIKTOK_TRACKED_FIELDS,
            )
        except Exception as exc:
            logger.debug("user_change_tracker[tiktok]: detect_and_log failed: %s", exc)

        return str(row["id"]) if row else None

    async def _ensure_post_profile(self, conn, data: dict, username: str | None) -> str | None:
        """Return or create a minimal TikTok profile for post attribution.

        Gallery/API post payloads sometimes have no author block but still carry
        enough top-level metadata (metadata.user / secUid) to identify the owner.
        Attach those posts to a stub profile instead of leaving profile_id NULL.
        """
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        handle = self._clean_username(
            author.get("uniqueId")
            or data.get("uniqueId")
            or data.get("user")
            or data.get("author")
            or data.get("authorUniqueId")
            or username
        )
        platform_user_id = (
            author.get("id")
            or data.get("authorId")
            or data.get("userId")
            or data.get("uid")
            or data.get("secUid")
            or data.get("authorSecId")
        )
        if platform_user_id:
            platform_user_id = str(platform_user_id).strip()
        if not platform_user_id and handle:
            platform_user_id = f"handle:{handle}"

        if platform_user_id:
            row = await conn.fetchrow(
                "SELECT id FROM tiktok_profiles WHERE platform_user_id = $1",
                platform_user_id,
            )
            if row:
                return str(row["id"])

        if handle:
            row = await conn.fetchrow(
                """
                SELECT id
                FROM tiktok_profiles
                WHERE LOWER(username) = LOWER($1)
                ORDER BY updated_at DESC NULLS LAST
                LIMIT 1
                """,
                handle,
            )
            if row:
                return str(row["id"])

        if not platform_user_id:
            return None

        avatar = (
            author.get("avatarLarger")
            or author.get("avatarMedium")
            or author.get("avatarThumb")
            or data.get("avatarLarger")
            or data.get("avatarMedium")
            or data.get("avatarThumb")
        )
        row = await conn.fetchrow(
            """
            INSERT INTO tiktok_profiles (
                platform_user_id, username, nickname, avatar_url, bio,
                is_verified, is_private, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            ON CONFLICT (platform_user_id) DO UPDATE SET
                username = COALESCE(tiktok_profiles.username, EXCLUDED.username),
                nickname = COALESCE(tiktok_profiles.nickname, EXCLUDED.nickname),
                avatar_url = COALESCE(tiktok_profiles.avatar_url, EXCLUDED.avatar_url),
                bio = COALESCE(tiktok_profiles.bio, EXCLUDED.bio),
                is_verified = tiktok_profiles.is_verified OR EXCLUDED.is_verified,
                is_private = tiktok_profiles.is_private OR EXCLUDED.is_private,
                updated_at = NOW()
            RETURNING id
            """,
            platform_user_id,
            handle,
            author.get("nickname") or data.get("nickname"),
            avatar,
            author.get("signature") or data.get("signature"),
            self._safe_bool(author.get("verified", data.get("verified", False))),
            self._safe_bool(author.get("privateAccount", data.get("privateAccount", False))),
        )
        return str(row["id"]) if row else None

    async def _upsert_post(self, data: dict, username: str, profile_uuid: str | None = None):
        post_id = data.get("id")
        if not post_id:
            return
        video = data.get("video") if isinstance(data.get("video"), dict) else {}
        stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
        music = data.get("music") if isinstance(data.get("music"), dict) else {}

        # hashtags / mentions / challenges from textExtra and challenges[]
        hashtags: list[str] = []
        mentions: list[str] = []
        for t in (data.get("textExtra") or []):
            if not isinstance(t, dict):
                continue
            if t.get("hashtagName"):
                hashtags.append(t["hashtagName"])
            if t.get("userUniqueId"):
                mentions.append(t["userUniqueId"])
        challenges = [c.get("title") for c in (data.get("challenges") or []) if isinstance(c, dict) and c.get("title")]

        try:
            async with self.pool.acquire() as conn:
                if profile_uuid is None:
                    # Fallback: look up by username if author block was missing.
                    profile_row = await conn.fetchrow(
                        "SELECT id FROM tiktok_profiles WHERE username = $1", username
                    )
                    profile_uuid = str(profile_row["id"]) if profile_row else None
                if profile_uuid is None:
                    profile_uuid = await self._ensure_post_profile(conn, data, username)

                await conn.execute("""
                    INSERT INTO tiktok_posts (
                        platform_post_id, profile_id, video_url, cover_image_url,
                        title, description, hashtags, mentions, challenges,
                        music_id, music_title, music_author, music_duration,
                        duet_enabled, stitch_enabled,
                        view_count, like_count, comment_count, share_count,
                        duration, create_time, metadata
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                              $14, $15, $16, $17, $18, $19, $20, $21, $22)
                    ON CONFLICT (platform_post_id) DO UPDATE SET
                        profile_id = COALESCE(EXCLUDED.profile_id, tiktok_posts.profile_id),
                        video_url = COALESCE(EXCLUDED.video_url, tiktok_posts.video_url),
                        cover_image_url = COALESCE(EXCLUDED.cover_image_url, tiktok_posts.cover_image_url),
                        title = COALESCE(EXCLUDED.title, tiktok_posts.title),
                        description = COALESCE(EXCLUDED.description, tiktok_posts.description),
                        hashtags = EXCLUDED.hashtags,
                        mentions = EXCLUDED.mentions,
                        challenges = EXCLUDED.challenges,
                        music_id = COALESCE(EXCLUDED.music_id, tiktok_posts.music_id),
                        music_title = COALESCE(EXCLUDED.music_title, tiktok_posts.music_title),
                        music_author = COALESCE(EXCLUDED.music_author, tiktok_posts.music_author),
                        music_duration = COALESCE(EXCLUDED.music_duration, tiktok_posts.music_duration),
                        duet_enabled = EXCLUDED.duet_enabled,
                        stitch_enabled = EXCLUDED.stitch_enabled,
                        view_count = EXCLUDED.view_count,
                        like_count = EXCLUDED.like_count,
                        comment_count = EXCLUDED.comment_count,
                        share_count = EXCLUDED.share_count,
                        duration = COALESCE(EXCLUDED.duration, tiktok_posts.duration),
                        create_time = COALESCE(EXCLUDED.create_time, tiktok_posts.create_time),
                        metadata = EXCLUDED.metadata
                """,
                str(post_id),
                profile_uuid,
                video.get("downloadAddr") or video.get("playAddr"),
                video.get("cover") or video.get("originCover"),
                (data.get("desc") or "")[:500] or None,  # title (short)
                data.get("desc"),
                hashtags or None,
                mentions or None,
                challenges or None,
                str(music.get("id")) if music.get("id") is not None else None,
                music.get("title"),
                music.get("authorName"),
                self._safe_int(music.get("duration")),
                bool(data.get("duetEnabled", False)),
                bool(data.get("stitchEnabled", False)),
                self._safe_int(stats.get("playCount")),
                self._safe_int(stats.get("diggCount")),
                self._safe_int(stats.get("commentCount")),
                self._safe_int(stats.get("shareCount")),
                self._safe_int(video.get("duration")),
                self._to_dt(data.get("createTime")),
                json.dumps(data, default=str, ensure_ascii=False),
                )
        except Exception as e:
            logger.warning("tiktok _upsert_post failed for %s: %s", post_id, e)

    async def _collect_via_gallery_dl(self, username: str, profile_url: str) -> bool:
        logger.info("tiktok fallback gallery-dl: starting for %s", username)
        from src.core.subprocess_downloader import gallery_dl_download, managed_tempdir
        try:
            async with managed_tempdir("tiktok_gdl_") as tmpdir:
                result = await gallery_dl_download(
                    profile_url,
                    cookies_file=self._cookies_file,
                    extra_args=self._gallery_dl_archive_args(username),
                    timeout=self._timeout,
                    tempdir=tmpdir,
                    stop_event=self._stop if hasattr(self._stop, "wait") else None,
                )
                if not result.ok:
                    logger.warning(
                        "tiktok fallback gallery-dl failed for %s: rc=%s timed_out=%s "
                        "files=%d stderr=%s stdout=%s",
                        username, result.returncode, result.timed_out, result.file_count,
                        result.err_summary(800), (result.stdout or "")[:400],
                    )
                    await self._record_local_rate_limit_event(
                        username=username,
                        tool="gallery-dl",
                        result=result,
                    )
                    # Even on timeout/non-zero rc, ingest any files already pulled —
                    # gallery-dl/yt-dlp write incrementally, so a partial download is
                    # still real data worth keeping rather than discarding.
                    if result.file_count > 0:
                        self._advance_gallery_dl_range_cursor(username, file_count=result.file_count, ok=True)
                        await self._ingest_tmpdir(tmpdir, username)
                        return True
                    if result.timed_out:
                        self._last_gallery_dl_timeout_user = username
                        self._record_profile_backoff(
                            username,
                            reason="gallery-dl_timeout_no_files",
                            seconds=self._profile_timeout_backoff_seconds,
                        )
                    else:
                        self._record_profile_backoff(
                            username,
                            reason=f"gallery-dl_rc_{result.returncode}_no_files",
                            seconds=self._profile_failure_backoff_seconds,
                        )
                    return False

                logger.info(
                    "tiktok fallback gallery-dl: %s rc=0, downloaded %d files (stderr_tail=%s)",
                    username, result.file_count, result.err_summary(300),
                )
                if result.file_count == 0:
                    self._last_gallery_dl_empty_user = username
                    self._advance_gallery_dl_range_cursor(username, file_count=0, ok=True)
                    logger.info(
                        "tiktok fallback gallery-dl: %s returned 0 files for current range",
                        username,
                    )
                    self._record_profile_backoff(
                        username,
                        reason="gallery-dl_empty_range",
                        seconds=self._profile_empty_backoff_seconds,
                    )
                    return False
                self._advance_gallery_dl_range_cursor(username, file_count=result.file_count, ok=True)
                await self._ingest_tmpdir(tmpdir, username)
                return True
        except Exception as e:
            logger.warning("tiktok fallback gallery-dl exception for %s: %s: %s",
                           username, type(e).__name__, e, exc_info=True)
            return False

    async def _collect_via_yt_dlp(self, username: str, profile_url: str) -> bool:
        logger.info("tiktok fallback yt-dlp: starting for %s", username)
        from src.core.subprocess_downloader import yt_dlp_download, managed_tempdir
        try:
            async with managed_tempdir("tiktok_ytdlp_") as tmpdir:
                result = await yt_dlp_download(
                    profile_url,
                    cookies_file=self._cookies_file,
                    max_downloads=(self._yt_dlp_max_downloads or None),
                    timeout=self._timeout,
                    retries=self._retries,
                    tempdir=tmpdir,
                    stop_event=self._stop if hasattr(self._stop, "wait") else None,
                )
                if not result.ok:
                    logger.warning(
                        "tiktok fallback yt-dlp failed for %s: rc=%s timed_out=%s "
                        "files=%d stderr=%s stdout=%s",
                        username, result.returncode, result.timed_out, result.file_count,
                        result.err_summary(800), (result.stdout or "")[:400],
                    )
                    await self._record_local_rate_limit_event(
                        username=username,
                        tool="yt-dlp",
                        result=result,
                    )
                    # Ingest partial downloads even on timeout (see gallery-dl note).
                    if result.file_count > 0:
                        await self._ingest_tmpdir(tmpdir, username)
                        return True
                    self._record_profile_backoff(
                        username,
                        reason=(
                            "yt-dlp_timeout_no_files"
                            if result.timed_out
                            else f"yt-dlp_rc_{result.returncode}_no_files"
                        ),
                        seconds=(
                            self._profile_timeout_backoff_seconds
                            if result.timed_out
                            else self._profile_failure_backoff_seconds
                        ),
                    )
                    return False

                logger.info(
                    "tiktok fallback yt-dlp: %s rc=%s, downloaded %d files (stderr_tail=%s)",
                    username, result.returncode, result.file_count, result.err_summary(300),
                )
                if result.file_count == 0:
                    await self._record_local_rate_limit_event(
                        username=username,
                        tool="yt-dlp",
                        result=result,
                    )
                    self._record_profile_backoff(
                        username,
                        reason="yt-dlp_empty",
                        seconds=self._profile_empty_backoff_seconds,
                    )
                    return False
                await self._ingest_tmpdir(tmpdir, username)
                return True
        except Exception as e:
            logger.warning("tiktok fallback yt-dlp exception for %s: %s: %s",
                           username, type(e).__name__, e, exc_info=True)
            return False

    async def _ingest_tmpdir(self, tmpdir: str, username: str):
        # Pass 1: ingest gallery-dl JSON sidecars to populate tiktok_profiles
        # and tiktok_posts. Sidecars sit next to each media file as <media>.json.
        sidecar_count = 0
        post_count = 0
        profile_uuids: dict[str, str] = {}  # platform_user_id -> profile uuid
        for sc in Path(tmpdir).rglob("*.json"):
            if self._stop.is_set(): break
            if not sc.is_file(): continue
            # Skip our own metadata files if any leak in.
            if sc.name.endswith("_metadata.json"): continue
            try:
                with open(sc, "r", encoding="utf-8") as fh:
                    sidecar = json.load(fh)
            except Exception as e:
                logger.debug("tiktok sidecar parse skip %s: %s", sc.name, e)
                continue
            if not isinstance(sidecar, dict):
                continue
            sidecar_count += 1

            author = sidecar.get("author") if isinstance(sidecar.get("author"), dict) else None
            author_stats = sidecar.get("authorStats") if isinstance(sidecar.get("authorStats"), dict) else None
            profile_uuid = None
            if author:
                pid = author.get("id")
                if pid and pid in profile_uuids:
                    profile_uuid = profile_uuids[pid]
                else:
                    profile_uuid = await self._upsert_profile(author, author_stats)
                    if pid and profile_uuid:
                        profile_uuids[pid] = profile_uuid

            # Only upsert posts when sidecar looks like a post (has top-level id + desc/video).
            if sidecar.get("id") and (sidecar.get("video") or sidecar.get("desc") is not None or sidecar.get("createTime")):
                try:
                    await self._upsert_post(sidecar, username, profile_uuid)
                    post_count += 1
                except Exception as e:
                    logger.warning("tiktok sidecar upsert_post failed %s: %s", sc.name, e)

        if sidecar_count:
            logger.info(
                "tiktok ingest: parsed %d sidecars, upserted %d profile(s) and %d post(s) for %s",
                sidecar_count, len(profile_uuids), post_count, username,
            )
            # Post/profile sidecar upserts are real collector progress even when
            # all media files are duplicates. Let the worker watchdog see that.
            self._progress_count += max(1, post_count)

        # Pass 2: copy media files into our store (existing behavior).
        for f in Path(tmpdir).rglob("*"):
            if self._stop.is_set(): break
            if not f.is_file(): continue
            ext = f.suffix.lstrip(".").lower()
            if ext not in ("jpg", "jpeg", "png", "mp4", "webm", "gif", "webp"): continue

            cid = f.stem
            # gallery-dl filenames embed the title; trim to first whitespace token
            # so cid stays a stable numeric video id and fits varchar(100).
            cid = cid.split()[0][:100] if cid else cid
            if self.is_known(cid): continue

            data = f.read_bytes()
            content_type = "video" if ext in ("mp4", "webm") else "post"

            await self.download_media({
                "entity_id": username,
                "entity_name": username,
                "content_type": content_type,
                "content_id": cid,
                "data": data,
                "extension": ext if ext != "jpeg" else "jpg",
            })
            await asyncio.sleep(random.uniform(self._min_sleep, self._max_sleep))

    async def _collect_via_api(self, username: str) -> bool:
        await self.wait_rate_limit("tiktok.com")
        cookies = {"sessionid": self._session_id} if self._session_id else {}

        try:
            async with httpx.AsyncClient(timeout=30, cookies=cookies, follow_redirects=True) as client:
                resp = await client.get(f"https://www.tiktok.com/@{username}", headers={"User-Agent": self.user_agents.get_for_domain("tiktok.com")})
                resp.raise_for_status()

                html = resp.text
                marker = '"ItemModule":'
                start = html.find(marker)
                if start == -1:
                    return False

                bracket_start = html.find("{", start + len(marker))
                if bracket_start == -1:
                    return False
                depth, end = 0, bracket_start
                for i, ch in enumerate(html[bracket_start:], bracket_start):
                    if ch == "{": depth += 1
                    elif ch == "}": depth -= 1
                    if depth == 0:
                        end = i + 1
                        break

                items = json.loads(html[bracket_start:end])
                processed = 0
                for video_id, video_data in items.items():
                    if self._stop.is_set(): break
                    await self._upsert_post(video_data, username)
                    processed += 1
                    cover = video_data.get("video", {}).get("cover")
                    if cover:
                        await self.download_media({
                            "entity_id": username, "entity_name": username,
                            "content_type": "thumbnail", "content_id": video_id,
                            "url": cover, "extension": "jpg", "raw": video_data
                        })
                return processed > 0
        except Exception as e:
            logger.error("API fallback failed for %s: %s", username, e)
            return False

    async def _collect_via_playwright(self, username: str) -> bool:
        """Browser-automation fallback (Playwright/Chromium).

        Lazy-imports ``TikTokBrowserDownloader`` so the heavyweight Playwright
        runtime isn't pulled in unless this fallback is actually exercised.
        Honours both the module-level ``TIKTOK_BROWSER_FALLBACK_ENABLED`` flag
        (re-read here so tests can monkeypatch it after import) and the
        instance-level ``self._browser_fallback`` toggle set in __init__.

        Returns True iff at least one media item was successfully fetched and
        ingested; False otherwise (caller cascades to the next fallback).
        """
        # Re-read the module-level flag so monkeypatch.setenv at test time
        # toggles behaviour without re-importing the module.
        if os.getenv("TIKTOK_BROWSER_FALLBACK_ENABLED", "true").lower() != "true":
            logger.info(
                "tiktok fallback playwright: disabled via env for %s", username,
            )
            return False
        if not self._browser_fallback:
            return False

        try:
            # Lazy import: keeps Playwright cost out of every collector start.
            from src.core.tiktok_browser import TikTokBrowserDownloader
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "tiktok fallback playwright: import failed for %s: %s",
                username, exc,
            )
            return False

        cookies_path = (
            Path(self._cookies_file)
            if self._cookies_file
            else Path(os.getenv("TIKTOK_COOKIES_FILE", "/data/cookies/tiktok.txt"))
        )
        max_videos = int(os.getenv("TIKTOK_BROWSER_MAX_VIDEOS", str(self._profile_chunk_items or 50)))
        browser_output_dir = Path(
            os.getenv("TIKTOK_BROWSER_TEMP_DIR", str(VAULT_ROOT / "tmp" / "tiktok_browser"))
        )
        browser = TikTokBrowserDownloader(cookies_file=cookies_path)
        try:
            try:
                items = await browser.download_user(
                    username, max_videos=max_videos
                )
            except Exception as exc:
                logger.warning(
                    "tiktok fallback playwright: download_user crashed for %s: %s",
                    username, exc, exc_info=True,
                )
                return False
        finally:
            try:
                await browser.close()
            except Exception:
                logger.debug("tiktok fallback playwright: close failed", exc_info=True)

        if not items:
            logger.info(
                "tiktok fallback playwright: no items for %s", username,
            )
            return False

        # Items returned by TikTokBrowserDownloader carry a (downloaded) file
        # path; ingest each through the unified ``download_media`` envelope so
        # they flow through the same UserChangeTracker / dedupe_hash path as
        # gallery-dl/yt-dlp results.
        ingested = 0
        for it in items:
            if self._stop.is_set():
                break
            vid = (it or {}).get("video_id")
            fp = (it or {}).get("file_path")
            if not vid or not fp:
                continue
            if self.is_known(vid):
                continue
            try:
                with open(fp, "rb") as fh:
                    data = fh.read()
            except OSError as exc:
                logger.warning(
                    "tiktok fallback playwright: read %s failed: %s", fp, exc,
                )
                continue
            try:
                await self.download_media({
                    "entity_id": username,
                    "entity_name": username,
                    "content_type": "video",
                    "content_id": vid,
                    "data": data,
                    "extension": "mp4",
                    "raw": (it or {}).get("metadata") or {},
                })
                ingested += 1
            finally:
                self._unlink_browser_temp_file(fp, browser_output_dir)
            await asyncio.sleep(random.uniform(self._min_sleep, self._max_sleep))
        logger.info(
            "tiktok fallback playwright: ingested %d/%d items for %s",
            ingested, len(items), username,
        )
        return ingested > 0

    @staticmethod
    def _unlink_browser_temp_file(file_path: str | os.PathLike[str], output_dir: Path) -> None:
        try:
            path = Path(file_path).resolve(strict=False)
            root = output_dir.resolve(strict=False)
            path.relative_to(root)
        except Exception:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.debug("tiktok fallback playwright: temp cleanup failed for %s", path, exc_info=True)

    async def _load_tracker_state(self):
        if self.pool:
            try:
                async with self.pool.acquire() as conn:
                    rows = await conn.fetch("SELECT platform_post_id FROM tiktok_posts")
                    self._tracked_ids = {r["platform_post_id"] for r in rows}
            except Exception as e: logger.debug("_load_tracker_state failed: %s", e)

    async def _record_download(self, username: str, video_id: str, file_path: str):
        self._tracked_ids.add(video_id)

    @staticmethod
    def _build_tiktok_source_url(item: dict) -> str | None:
        """Canonical TikTok post URL (media_items.source_url). Content-id
        conventions inside this collector:
          content_type=video: content_id = numeric aweme_id OR
                                       "dom_<slug>" for DOM-scraped items
          content_type=post:  content_id = "<aweme_id>" OR
                                       "<aweme_id>_<slot_index>" for
                                       photo-slot rows extracted from
                                       photo-slideshow posts (gallery-dl
                                       filename convention)
          content_type=photo: content_id = "<aweme_id>_<slot_index>"
                                       (multi-image posts)
          content_type=profile_photo: content_id = "profile_<sec_uid>"

        Returns https://www.tiktok.com/@<username>/video/<aweme_id> for
        video/post (TikTok's /video/ path is the canonical share URL for
        both single-video AND slideshow posts), /photo/ for the photo
        content type, or the profile page for profile_photo. Returns None
        if we can't extract enough (no username, or a dom_<slug> id that
        can't be back-mapped to an aweme_id)."""
        ctype = (item.get("content_type") or "").strip()
        cid = (item.get("content_id") or "").strip()
        username = (item.get("entity_name") or "").strip()
        if not username:
            return None
        if ctype == "profile_photo":
            return f"https://www.tiktok.com/@{username}"
        if ctype == "photo":
            aweme = cid.rsplit("_", 1)[0]
            if not aweme.isdigit():
                return None
            return f"https://www.tiktok.com/@{username}/photo/{aweme}"
        if ctype in ("video", "post"):
            if cid.startswith("dom_"):
                # DOM-scraped identifier — no aweme_id available, so we
                # can't build a per-post URL. Fall back to the profile URL
                # (still better than NULL — the file is at least traceable
                # to an entity).
                return f"https://www.tiktok.com/@{username}"
            # Strip a trailing "_<slot>" suffix: gallery-dl produces
            # "<aweme_id>_<NN>.jpg" filenames for photo-slideshow slots,
            # and the collector maps those into content_type=post rows.
            aweme = cid.rsplit("_", 1)[0] if "_" in cid else cid
            if not aweme.isdigit():
                return None
            return f"https://www.tiktok.com/@{username}/video/{aweme}"
        return None

    async def download_media(self, item: dict):
        cid = item["content_id"]
        if self.is_known(cid): return

        filename = self.build_filename(
            item["entity_id"], item["entity_name"],
            item["content_type"], cid, extension=item.get("extension", "mp4")
        )

        try:
            if "data" in item:
                data = item["data"]
            elif "url" in item:
                await self.wait_rate_limit("tiktok.com")
                async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                    resp = await client.get(item["url"], headers={"User-Agent": self.user_agents.get_for_domain("tiktok.com")})
                    resp.raise_for_status()
                    data = resp.content
            else: return

            sha = (
                _dedupe_sha256_bytes(data) if _dedupe_sha256_bytes is not None
                else self.sha256_bytes(data)
            )
            source_url = self._build_tiktok_source_url(item)
            metadata = {
                "entity_id": item["entity_id"],
                "entity_name": item["entity_name"],
                "content_type": item["content_type"],
                "content_id": cid,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "raw": item.get("raw", {}),
                "rebuild_target_tables": ["media_items", "tiktok_posts", "tiktok_profiles"],
            }
            artifact = write_atomic_artifact(
                source=self.SOURCE_NAME,
                artifact_id=cid,
                artifact_kind="media_blob",
                data=data,
                extension=item.get("extension", "mp4"),
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

    # =====================================================================
    # Spec API — required by the unified collector interface (Wave 2)
    # =====================================================================

    async def collect_user_profile(self, username: str) -> Optional[str]:
        """Fetch + upsert a single user's profile metadata.

        Returns the ``tiktok_profiles.id`` (UUID string) on success, or
        ``None``. This is a thin wrapper over the existing scrape path so
        callers (scheduler, spider, on-demand admin tools) can target a
        single profile without triggering full video collection.
        """
        try:
            username = validate_username(username)
        except ValueError as e:
            logger.warning("collect_user_profile: invalid username %r: %s", username, e)
            return None
        # Quota check (per-account; if no cookie set we still honour the
        # platform-level cap by passing a synthetic account name).
        if self._quota is not None:
            account_name = Path(self._cookies_file).stem if self._cookies_file else "default"
            try:
                if not await self._quota.has_quota("tiktok", account_name):
                    logger.info("tiktok quota exhausted for %s; skipping %s", account_name, username)
                    return None
            except Exception:
                logger.debug("quota check failed; proceeding", exc_info=True)
        await self.wait_rate_limit("tiktok.com")
        try:
            metadata = await self._scrape_profile_metadata(username)
        except Exception as e:
            logger.warning("collect_user_profile %s: %s", username, e)
            return None
        if str(metadata.get("status") or "") not in {"ok", "private"}:
            await self._record_profile_access(
                username,
                False,
                error=str(metadata.get("error") or metadata.get("status") or "profile_metadata_unavailable"),
            )
            return None
        # Look up the profile we just upserted.
        if self.pool is None:
            return None
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id FROM tiktok_profiles WHERE username = $1", username
                )
                if row:
                    await self._record_profile_access(username, True)
                    return str(row["id"])
                return None
        except Exception:
            return None

    async def collect_user_videos(self, username: str) -> int:
        """Run the full video collection pipeline for one user.

        Returns the number of media items processed (best-effort count from
        the dedupe set delta). Honours the daily quota and uses the
        gallery-dl → yt-dlp → API fallback chain.
        """
        try:
            username = validate_username(username)
        except ValueError:
            return 0
        if self._quota is not None:
            account_name = Path(self._cookies_file).stem if self._cookies_file else "default"
            try:
                if not await self._quota.has_quota("tiktok", account_name):
                    logger.info("tiktok quota exhausted for %s; skipping videos for %s", account_name, username)
                    return 0
                await self._quota.consume("tiktok", account_name, n=1)
            except Exception:
                logger.debug("quota consume failed; proceeding", exc_info=True)
        before = len(self._known_ids)
        try:
            await self._collect_user(username)
        except Exception as e:
            logger.warning("collect_user_videos %s: %s", username, e)
        return max(0, len(self._known_ids) - before)

    async def collect_following(self, username: str) -> AsyncIterator[str]:
        """Yield TikTok usernames that ``username`` follows.

        Best-effort: when neither the API HTML nor a logged-in cookie set
        exposes the following list (TikTok hides it for many accounts), we
        yield nothing. Used by ``TiktokEdgeFetcher`` for spider expansion.
        """
        try:
            username = validate_username(username)
        except ValueError:
            return
        await self.wait_rate_limit("tiktok.com")
        cookies = {"sessionid": self._session_id} if self._session_id else {}
        url = f"https://www.tiktok.com/@{username}"
        try:
            async with httpx.AsyncClient(
                timeout=30, cookies=cookies, follow_redirects=True
            ) as client:
                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": self.user_agents.get_for_domain("tiktok.com")
                    },
                )
                resp.raise_for_status()
                html = resp.text
        except Exception as e:
            logger.debug("collect_following %s: scrape failed: %s", username, e)
            return
        # Best-effort regex scan for ``"uniqueId":"..."`` in the embedded
        # SIGI_STATE JSON. We don't parse the whole blob because it's huge
        # and the structure shifts between TikTok web releases; the regex
        # picks up usernames in any nested user dict including the user
        # module that contains following/follower lists when present.
        seen: set[str] = set()
        for m in re.finditer(r'"uniqueId"\s*:\s*"([a-zA-Z0-9._-]{1,30})"', html):
            uid = m.group(1)
            if uid == username or uid in seen:
                continue
            seen.add(uid)
            yield uid

    async def spider_related_creators(
        self, seed: str, max_hops: int = 2
    ) -> int:
        """BFS-discover related creators from a seed username.

        Uses Wave 0 ``SpiderDiscover`` over our ``TiktokEdgeFetcher``. Returns
        the number of nodes discovered (or 0 if the spider module is
        unavailable). Discovered usernames get queued in the unified
        ``spider_queue`` Postgres table for later processing.
        """
        if SpiderDiscover is None or self.pool is None:
            logger.info("spider_related_creators: SpiderDiscover unavailable")
            return 0
        try:
            seed = validate_username(seed)
        except ValueError as e:
            logger.warning("spider_related_creators: bad seed %r: %s", seed, e)
            return 0
        spider = self.make_spider_discover(max_hops=max_hops)
        try:
            return await spider.run(seeds=[seed])
        except Exception as e:
            logger.warning("spider_related_creators %s: %s", seed, e)
            return 0

    def make_edge_fetcher(self) -> "TiktokEdgeFetcher":
        """Build a Wave 0 ``EdgeFetcher`` over this collector."""
        return TiktokEdgeFetcher(self)

    def make_spider_discover(self, *, max_hops: Optional[int] = None):
        """Build a ``SpiderDiscover`` for the unified spider queue."""
        if SpiderDiscover is None:
            raise RuntimeError("src.core.spider_discover not importable")
        return SpiderDiscover(
            platform="tiktok",
            fetcher=self.make_edge_fetcher(),
            pool=self.pool,
            max_hops=max_hops if max_hops is not None else int(
                os.getenv("TIKTOK_SPIDER_DEPTH", "2")
            ),
            concurrency=int(os.getenv("TIKTOK_SPIDER_CONCURRENCY", "2")),
        )
