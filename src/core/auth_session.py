"""Instagram session capsule.

Wraps cookie-based auth + warmup pacing + health check + challenge detection
into a reusable async object. Replaces the cookie-loading sprawl currently
scattered across ``src/collectors/instagram.py`` (and the 43.5x-bloated
``instagramtoolkit/``).

Wave 0 / Batch 2 cross-cutting module. READ-ONLY against existing collectors;
they will adopt this in Wave 2.

Scope notes
-----------
Out of scope (intentionally):
    * Login flow (username/password) — manual / Wave 2.
    * 2FA SMS / TOTP routing.
    * Challenge solving — we only *flag* ``challenge_required`` and surface it
      for human resolution via the dashboard.

In scope:
    * Load cookies from ``data/instagram/{account}/cookies.pkl`` (existing
      convention) with atomic save.
    * ``refresh()`` — hit a benign warmup endpoint and absorb new cookies.
    * ``is_alive()`` — single auth probe against ``/api/v1/users/{uid}/info/``.
    * ``maybe_warmup(min_interval_s)`` — pacing wrapper around ``refresh``.
    * Challenge detection from response body / status.

A thin :class:`SessionCapsule` Protocol is exported so future TT / Lemon8 /
Threads sessions share the call surface.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pickle
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

import httpx

logger = logging.getLogger(__name__)


# ---------- constants ------------------------------------------------------

LOGGED_IN = "logged_in"
LOGGED_OUT = "logged_out"
CHALLENGE_REQUIRED = "challenge_required"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Instagram 295.0.0.32.119 (iPhone14,3; iOS 16_6; en_US; en-US; "
    "scale=3.00; 1284x2778; 519988420)"
)

WARMUP_URL = "https://www.instagram.com/accounts/edit/?__a=1&__d=dis"
USER_INFO_URL = "https://i.instagram.com/api/v1/users/{uid}/info/"

DEFAULT_MIN_WARMUP_INTERVAL_S = 600.0  # 10 min


# ---------- protocol -------------------------------------------------------


@runtime_checkable
class SessionCapsule(Protocol):
    """Common surface for per-platform session capsules."""

    account_name: str
    login_status: str

    async def refresh(self) -> bool: ...
    async def is_alive(self) -> bool: ...
    async def maybe_warmup(self, min_interval_s: float = ...) -> bool: ...
    async def save(self) -> None: ...


# ---------- helpers --------------------------------------------------------


def default_cookie_path(account_name: str, root: str | os.PathLike = "data/instagram") -> Path:
    """Canonical pickle path for an IG account's cookies."""
    return Path(root) / account_name / "cookies.pkl"


def _atomic_pickle_write(path: Path, payload: Any) -> None:
    """Write pickle atomically: tmp file in same dir, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cookies-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _looks_like_challenge(status_code: int, body: str) -> bool:
    """Heuristic challenge detection.

    IG returns 'challenge_required' either as a status field in JSON or as a
    redirect path inside HTML. We err on the side of flagging.
    """
    if "challenge_required" in body:
        return True
    if status_code in (400, 403) and "/challenge/" in body:
        return True
    return False


# ---------- the capsule ----------------------------------------------------


@dataclass
class IgSession:
    """Instagram session capsule.

    Cookies and login_status are private-ish — :meth:`__repr__` masks them so
    we never accidentally leak ``sessionid`` into logs or test output.

    Parameters
    ----------
    account_name:
        Logical account label. Used for cookie path & logging only.
    cookie_path:
        Override for the pickle path. Defaults to
        ``data/instagram/{account_name}/cookies.pkl``.
    user_agent:
        UA string sent on every request. Defaults to a recent IG iOS UA.
    user_id:
        Numeric IG user id (string). Required for :meth:`is_alive`. Optional
        because ``refresh`` does not need it.
    client:
        Optional injected ``httpx.AsyncClient`` (used for tests / shared
        connection pools). If None, a per-call ephemeral client is created.
    """

    account_name: str
    cookie_path: Optional[Path] = None
    user_agent: str = DEFAULT_USER_AGENT
    user_id: Optional[str] = None
    client: Optional[httpx.AsyncClient] = None

    cookies: dict = field(default_factory=dict)
    login_status: str = LOGGED_OUT
    last_warmup_at: float = 0.0
    last_action_at: float = 0.0

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # ---- repr / safety ----------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover — trivial
        return (
            f"IgSession(account={self.account_name!r} "
            f"status={self.login_status} "
            f"cookies=<{len(self.cookies)} masked> "
            f"last_warmup_at={self.last_warmup_at:.0f})"
        )

    def __post_init__(self) -> None:
        if self.cookie_path is None:
            self.cookie_path = default_cookie_path(self.account_name)
        else:
            self.cookie_path = Path(self.cookie_path)

    # ---- persistence ------------------------------------------------------

    async def load(self) -> bool:
        """Load cookies from pickle. Returns True on success."""
        path = self.cookie_path
        assert path is not None
        if not path.exists():
            logger.debug("No cookie pickle at %s for %s", path, self.account_name)
            return False
        try:
            data = await asyncio.to_thread(_read_pickle, path)
        except Exception as e:
            logger.warning("Failed to load cookies for %s: %s", self.account_name, e)
            return False

        if isinstance(data, dict) and "cookies" in data:
            self.cookies = dict(data.get("cookies") or {})
            self.user_agent = data.get("user_agent") or self.user_agent
            self.login_status = data.get("login_status") or LOGGED_IN
            self.last_warmup_at = float(data.get("last_warmup_at") or 0.0)
            self.user_id = data.get("user_id") or self.user_id
        elif isinstance(data, dict):
            # legacy: bare cookie dict
            self.cookies = dict(data)
            self.login_status = LOGGED_IN
        else:
            logger.warning("Cookie pickle for %s has unexpected shape", self.account_name)
            return False
        return True

    async def save(self) -> None:
        """Persist cookies + meta atomically."""
        path = self.cookie_path
        assert path is not None
        payload = {
            "cookies": self.cookies,
            "user_agent": self.user_agent,
            "login_status": self.login_status,
            "last_warmup_at": self.last_warmup_at,
            "user_id": self.user_id,
        }
        await asyncio.to_thread(_atomic_pickle_write, path, payload)

    # ---- network helpers --------------------------------------------------

    def _headers(self) -> dict:
        h = {
            "User-Agent": self.user_agent,
            "X-IG-App-ID": "936619743392459",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        csrf = self.cookies.get("csrftoken")
        if csrf:
            h["X-CSRFToken"] = csrf
        return h

    async def _request(
        self,
        method: str,
        url: str,
        *,
        timeout: float = 15.0,
    ) -> httpx.Response:
        """Issue a request via injected client or an ephemeral one."""
        if self.client is not None:
            return await self.client.request(
                method,
                url,
                headers=self._headers(),
                cookies=self.cookies,
                timeout=timeout,
                follow_redirects=False,
            )
        async with httpx.AsyncClient(follow_redirects=False) as c:
            return await c.request(
                method,
                url,
                headers=self._headers(),
                cookies=self.cookies,
                timeout=timeout,
            )

    def _absorb_cookies(self, resp: httpx.Response) -> None:
        """Merge any Set-Cookie values into our cookie dict."""
        try:
            jar = resp.cookies
            for name in jar.keys():
                self.cookies[name] = jar.get(name)
        except Exception:  # pragma: no cover — defensive
            pass

    # ---- public surface ---------------------------------------------------

    async def refresh(self) -> bool:
        """Hit a benign warmup endpoint to roll session cookies.

        Returns True on a 200/302 response, False on auth failure / network
        error. Sets ``login_status`` to ``challenge_required`` if the body
        looks like a challenge.
        """
        async with self._lock:
            try:
                resp = await self._request("GET", WARMUP_URL)
            except (httpx.HTTPError, asyncio.TimeoutError) as e:
                logger.warning("warmup failed for %s: %s", self.account_name, e)
                return False

            self._absorb_cookies(resp)
            self.last_action_at = time.time()
            body = _safe_text(resp)

            if _looks_like_challenge(resp.status_code, body):
                self.login_status = CHALLENGE_REQUIRED
                logger.warning("Challenge required for %s", self.account_name)
                return False

            if resp.status_code in (401, 403):
                self.login_status = LOGGED_OUT
                return False

            if resp.status_code in (200, 302):
                self.login_status = LOGGED_IN
                self.last_warmup_at = time.time()
                return True

            logger.info(
                "warmup unexpected status %s for %s", resp.status_code, self.account_name
            )
            return False

    async def is_alive(self) -> bool:
        """Probe ``/api/v1/users/{uid}/info/`` for current auth state.

        Returns True only on HTTP 200 with non-challenge body. Updates
        ``login_status`` as a side effect.
        """
        if not self.user_id:
            logger.debug("is_alive: no user_id set for %s — using warmup probe", self.account_name)
            return await self.refresh()

        url = USER_INFO_URL.format(uid=self.user_id)
        try:
            resp = await self._request("GET", url)
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            logger.warning("is_alive failed for %s: %s", self.account_name, e)
            return False

        self.last_action_at = time.time()
        body = _safe_text(resp)

        if _looks_like_challenge(resp.status_code, body):
            self.login_status = CHALLENGE_REQUIRED
            return False
        if resp.status_code == 200:
            self.login_status = LOGGED_IN
            return True
        if resp.status_code in (401, 403):
            self.login_status = LOGGED_OUT
            return False
        return False

    async def maybe_warmup(self, min_interval_s: float = DEFAULT_MIN_WARMUP_INTERVAL_S) -> bool:
        """Refresh only if our last warmup is older than ``min_interval_s``.

        Returns True if a refresh ran AND succeeded. Returns False if either
        skipped (still fresh) or refresh failed. Callers wanting "is fresh OR
        was just refreshed OK" can compare ``last_warmup_at`` instead.
        """
        now = time.time()
        if (now - self.last_warmup_at) < min_interval_s:
            return False
        return await self.refresh()

    # ---- convenience ------------------------------------------------------

    @property
    def is_challenge(self) -> bool:
        return self.login_status == CHALLENGE_REQUIRED


# ---------- private helpers ------------------------------------------------


def _read_pickle(path: Path) -> Any:
    with open(path, "rb") as f:
        return pickle.load(f)


def _safe_text(resp: httpx.Response) -> str:
    """Return resp.text without raising on weird encodings."""
    try:
        return resp.text or ""
    except Exception:  # pragma: no cover
        try:
            return resp.content.decode("utf-8", errors="replace")
        except Exception:
            return ""


__all__ = [
    "IgSession",
    "SessionCapsule",
    "LOGGED_IN",
    "LOGGED_OUT",
    "CHALLENGE_REQUIRED",
    "DEFAULT_MIN_WARMUP_INTERVAL_S",
    "default_cookie_path",
]
