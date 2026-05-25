import hashlib
import logging
import os
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _quota_date(reset_hour: int = 0) -> str:
    """Return today's quota-window key, rolling at *reset_hour* UTC.

    Default reset_hour=0 means quota windows align with UTC midnight.
    Set reset_hour to a positive int (0-23) to roll later in the day
    (e.g. reset_hour=4 -> windows roll at 04:00 UTC).
    """
    now = datetime.now(timezone.utc)
    if now.hour < reset_hour:
        # Belongs to yesterday's window
        from datetime import timedelta
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")


@dataclass
class _Quota:
    """Per-account daily-usage counters."""
    quota_date: str = ""
    profile_views: int = 0
    actions: int = 0
    media_downloads: int = 0


@dataclass
class Account:
    name: str
    credentials: dict[str, str]
    fingerprint: dict[str, str] = field(default_factory=dict)
    last_used: float = 0
    locked_until: float = 0
    error_count: int = 0
    success_count: int = 0
    total_requests: int = 0
    quota: _Quota = field(default_factory=_Quota)

    @property
    def health_score(self) -> float:
        total = self.success_count + self.error_count
        if total == 0:
            return 1.0
        return self.success_count / total

    @property
    def is_locked(self) -> bool:
        return time.monotonic() < self.locked_until

    @property
    def is_healthy(self) -> bool:
        return not self.is_locked and self.health_score > 0.3


def _build_fingerprint(name: str) -> dict[str, str]:
    """Deterministic fingerprint derived from MD5(account_name).

    Produces a stable UA string, locale, and device_id so each account always
    presents the same browser identity across sessions.
    """
    digest = hashlib.md5(name.encode()).hexdigest()
    ua_variants = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    ]
    locales = ["en-US", "en-GB", "en-AU", "en-SG"]
    idx = int(digest[:2], 16)
    return {
        "user_agent": ua_variants[idx % len(ua_variants)],
        "locale": locales[idx % len(locales)],
        "device_id": digest[:16],
    }


class AccountPool:
    """Multi-account rotation manager with LRU selection, cooldown, and health tracking."""

    def __init__(
        self,
        default_cooldown: float = 900.0,
        error_cooldown: float = 1800.0,
        max_consecutive_errors: int = 5,
        daily_quota_profile_views: int = 0,
        daily_quota_actions: int = 0,
        daily_quota_media_downloads: int = 0,
        quota_reset_hour: int = 0,
    ):
        self.default_cooldown = default_cooldown
        self.error_cooldown = error_cooldown
        self.max_consecutive_errors = max_consecutive_errors
        # Daily quota limits. 0 = unlimited.
        self.daily_quota_profile_views = daily_quota_profile_views
        self.daily_quota_actions = daily_quota_actions
        self.daily_quota_media_downloads = daily_quota_media_downloads
        self.quota_reset_hour = quota_reset_hour
        self._accounts: list[Account] = []
        self._lock = threading.Lock()
        self._current_index = 0

    def load_from_env(self, prefix: str, fields: list[str] | None = None):
        """Load accounts from environment variables.

        Reads ACCOUNT_N_<field> pattern.  For example, with prefix="INSTA" and
        fields=["NAME","USER","PASS"], reads INSTA_ACCOUNT_1_NAME, etc.
        """
        if fields is None:
            fields = ["NAME", "USER", "PASS"]

        loaded: list[Account] = []
        i = 1
        while True:
            creds = {}
            found_any = False
            for f in fields:
                key = f"{prefix}_ACCOUNT_{i}_{f}"
                val = os.environ.get(key, "")
                if val:
                    found_any = True
                creds[f.lower()] = val
            if not found_any:
                break
            name = creds.get("name", f"account_{i}")
            acct = Account(
                name=name,
                credentials=creds,
                fingerprint=_build_fingerprint(name),
            )
            loaded.append(acct)
            logger.info("Loaded account: %s (%s)", name, prefix)
            i += 1

        # Append under the lock so concurrent get_next/record_* don't see
        # a partially-loaded list.
        with self._lock:
            self._accounts.extend(loaded)
            count = len(self._accounts)
        logger.info("AccountPool loaded %d accounts for prefix %s", count, prefix)

    def add_account(self, name: str, credentials: dict[str, str]):
        acct = Account(
            name=name,
            credentials=credentials,
            fingerprint=_build_fingerprint(name),
        )
        with self._lock:
            self._accounts.append(acct)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._accounts)

    @property
    def available_count(self) -> int:
        with self._lock:
            return sum(1 for a in self._accounts if a.is_healthy)

    def get_next(self, exclude: str | None = None) -> Account | None:
        """Return the least-recently-used healthy account, optionally excluding one by name."""
        with self._lock:
            candidates = [
                a for a in self._accounts
                if a.is_healthy and a.name != exclude
            ]
            if not candidates:
                candidates = [a for a in self._accounts if not a.is_locked and a.name != exclude]
            if not candidates:
                logger.warning("No available accounts in pool")
                return None
            candidates.sort(key=lambda a: a.last_used)
            chosen = candidates[0]
            chosen.last_used = time.monotonic()
            chosen.total_requests += 1
            return chosen

    def record_success(self, name: str):
        with self._lock:
            acct = self._find(name)
            if acct:
                acct.success_count += 1
                acct.error_count = max(0, acct.error_count - 1)

    def record_error(self, name: str, cooldown: float | None = None):
        with self._lock:
            acct = self._find(name)
            if not acct:
                return
            acct.error_count += 1
            cd = cooldown or self.error_cooldown
            if acct.error_count >= self.max_consecutive_errors:
                acct.locked_until = time.monotonic() + cd
                logger.warning(
                    "Account %s locked for %.0fs after %d errors",
                    name, cd, acct.error_count,
                )

    def cooldown(self, name: str, seconds: float | None = None):
        with self._lock:
            acct = self._find(name)
            if acct:
                acct.locked_until = time.monotonic() + (seconds or self.default_cooldown)

    def get_recommendation(self, exclude_name: str) -> Account | None:
        return self.get_next(exclude=exclude_name)

    def get_status(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "name": a.name,
                    "healthy": a.is_healthy,
                    "locked": a.is_locked,
                    "health_score": round(a.health_score, 2),
                    "total_requests": a.total_requests,
                    "error_count": a.error_count,
                }
                for a in self._accounts
            ]

    # -- Daily quota tracking --------------------------------------------
    #
    # Each Account carries a ``_Quota`` dataclass with counters. The window
    # rolls automatically at ``quota_reset_hour`` UTC. Setting any of the
    # ``daily_quota_*`` ctor args to 0 means that counter has no ceiling.

    def _reset_quota_if_new_day(self, acct: Account):
        """CALLER must hold self._lock."""
        today = _quota_date(self.quota_reset_hour)
        if acct.quota.quota_date != today:
            acct.quota = _Quota(quota_date=today)

    def record_profile_view(self, name: str, count: int = 1):
        """Increment per-account profile-view counter (resets daily)."""
        with self._lock:
            acct = self._find(name)
            if acct is None:
                return
            self._reset_quota_if_new_day(acct)
            acct.quota.profile_views += count

    def record_action(self, name: str, count: int = 1):
        """Increment per-account generic action counter (likes, follows, etc.)."""
        with self._lock:
            acct = self._find(name)
            if acct is None:
                return
            self._reset_quota_if_new_day(acct)
            acct.quota.actions += count

    def record_media_download(self, name: str, count: int = 1):
        """Increment per-account media-download counter."""
        with self._lock:
            acct = self._find(name)
            if acct is None:
                return
            self._reset_quota_if_new_day(acct)
            acct.quota.media_downloads += count

    def can_view_profiles(self, name: str) -> bool:
        """Return True if ``name`` has not hit profile-view ceiling today."""
        if self.daily_quota_profile_views <= 0:
            return True
        with self._lock:
            acct = self._find(name)
            if acct is None:
                return False
            self._reset_quota_if_new_day(acct)
            return acct.quota.profile_views < self.daily_quota_profile_views

    def can_perform_action(self, name: str) -> bool:
        """Return True if ``name`` has not hit action ceiling today."""
        if self.daily_quota_actions <= 0:
            return True
        with self._lock:
            acct = self._find(name)
            if acct is None:
                return False
            self._reset_quota_if_new_day(acct)
            return acct.quota.actions < self.daily_quota_actions

    def can_download_media(self, name: str) -> bool:
        """Return True if ``name`` has not hit media-download ceiling today."""
        if self.daily_quota_media_downloads <= 0:
            return True
        with self._lock:
            acct = self._find(name)
            if acct is None:
                return False
            self._reset_quota_if_new_day(acct)
            return acct.quota.media_downloads < self.daily_quota_media_downloads

    def get_quota_usage(self, name: str) -> dict:
        """Return raw quota counters for one account (post-reset)."""
        with self._lock:
            acct = self._find(name)
            if acct is None:
                return {}
            self._reset_quota_if_new_day(acct)
            q = acct.quota
            return {
                "quota_date": q.quota_date,
                "profile_views": q.profile_views,
                "actions": q.actions,
                "media_downloads": q.media_downloads,
            }

    def get_quota_summary(self, name: str) -> dict[str, str]:
        """Return human-readable quota summary like '13/200'."""
        usage = self.get_quota_usage(name)
        if not usage:
            return {}
        def fmt(used: int, ceil: int) -> str:
            return f"{used}/{ceil if ceil > 0 else 'inf'}"
        return {
            "date": usage["quota_date"],
            "profile_views": fmt(usage["profile_views"], self.daily_quota_profile_views),
            "actions": fmt(usage["actions"], self.daily_quota_actions),
            "media_downloads": fmt(usage["media_downloads"], self.daily_quota_media_downloads),
        }

    def get_next_with_quota(
        self,
        require: str = "any",
        exclude: str | None = None,
    ) -> Account | None:
        """LRU select the next healthy account that ALSO has quota for *require*.

        ``require`` is one of: 'any', 'profile_view', 'action',
        'media_download'. 'any' = no quota check (same as ``get_next``).
        Filters quota under the same lock as the LRU pick to avoid TOCTOU.
        """
        if require == "any":
            return self.get_next(exclude=exclude)

        with self._lock:
            check_map = {
                "profile_view": (self.daily_quota_profile_views, lambda a: a.quota.profile_views),
                "action": (self.daily_quota_actions, lambda a: a.quota.actions),
                "media_download": (self.daily_quota_media_downloads, lambda a: a.quota.media_downloads),
            }
            if require not in check_map:
                raise ValueError(f"unknown quota requirement: {require!r}")
            ceiling, getter = check_map[require]

            candidates = []
            for a in self._accounts:
                if a.name == exclude or not a.is_healthy:
                    continue
                self._reset_quota_if_new_day(a)
                if ceiling <= 0 or getter(a) < ceiling:
                    candidates.append(a)

            if not candidates:
                logger.warning(
                    "No accounts with %s quota available (excluded=%s)",
                    require, exclude,
                )
                return None

            candidates.sort(key=lambda a: a.last_used)
            chosen = candidates[0]
            chosen.last_used = time.monotonic()
            chosen.total_requests += 1
            return chosen

    def _find(self, name: str) -> Account | None:
        for a in self._accounts:
            if a.name == name:
                return a
        return None
