import hashlib
import logging
import os
import time
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


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
    ):
        self.default_cooldown = default_cooldown
        self.error_cooldown = error_cooldown
        self.max_consecutive_errors = max_consecutive_errors
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
            self._accounts.append(acct)
            logger.info("Loaded account: %s (%s)", name, prefix)
            i += 1

        logger.info("AccountPool loaded %d accounts for prefix %s", len(self._accounts), prefix)

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

    def _find(self, name: str) -> Account | None:
        for a in self._accounts:
            if a.name == name:
                return a
        return None
