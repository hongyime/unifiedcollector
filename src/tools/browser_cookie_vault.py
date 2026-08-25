"""Chrome cookie vault — CDP-driven backup/restore for social sessions.

Why this exists
---------------
Every headless collector for Meta / X / TikTok / Lemon8 that we cannot log into
via password (2FA, checkpoints, device trust) rides on cookies scraped from a
long-lived interactive Chrome. Historically each Chrome restart risked a fresh
consent sweep, a corrupted cookie DB, or an OS-level SQLite journal replay that
dropped `sessionid` / `c_user` / other login cookies — silently killing whole
collectors until an operator noticed and re-logged in. See the recent Chrome-
restart cookie-loss incidents that motivated `research/browser-cdp-cookie-
comparison.md`.

This daemon closes that gap by treating Chrome's cookie jar as ephemeral state
and the on-disk snapshots as the source of truth. Every ``BROWSER_COOKIE_VAULT_
INTERVAL_SECONDS`` seconds it snapshots ALL cookies for the seven social
domains via CDP ``Storage.getCookies``, atomically writes them to
``credentials/browser_cookies/latest.json``, and keeps a rolling window of the
last ``BROWSER_COOKIE_VAULT_KEEP_SNAPSHOTS`` timestamped snapshots for rollback.
Restore is an explicit ``python -m src.tools.browser_cookie_vault restore``
so a bad snapshot can never silently clobber a working live session.

Runtime
-------
- Docker service ``browser_cookie_vault`` (preferred, ``extra_hosts:
  host.docker.internal:host-gateway``).
- Fallback: ``scripts/register-cookie-vault-task.ps1`` registers a Windows
  Scheduled Task on the host if the container cannot reach Chrome.

CDP quirks handled here
-----------------------
- Chrome ≥ 128 enforces DNS-rebinding protection on the DevTools HTTP endpoint
  and returns HTTP 500 unless the ``Host`` header is ``localhost:<port>``. When
  we hit ``http://host.docker.internal:9222`` from a container we still send
  ``Host: localhost:9222`` so the fetch succeeds. WebSocket upgrades honour
  ``--remote-allow-origins=*`` so any Origin is accepted.
- ``Storage.getCookies`` returns cookies with read-only fields (``size``,
  ``session``) that ``Storage.setCookies`` rejects; we strip those before
  round-tripping.

Follow the AGENTS.md conventions: type hints everywhere, snake_case, the
existing ``logging.getLogger(__name__)`` pattern, no outbound writes to source
platforms (this observes and archives cookies; it never posts them anywhere).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────

# CDP endpoint. When the daemon runs in Docker it must talk to Chrome on the
# host. `extra_hosts: host.docker.internal:host-gateway` in compose gives us
# that name; the DNS-rebinding guard forces us to keep the URL's Host header
# as "localhost:<port>" — see `_HOST_HEADER` below.
CHROME_CDP_URL: str = os.getenv(
    "CHROME_CDP_URL",
    "http://host.docker.internal:9222",
).rstrip("/")

_DEFAULT_BACKUP_DIR = "/app/credentials/browser_cookies"


def _default_backup_dir() -> Path:
    """Return the cookie-vault directory for the current runtime.

    Containers run from ``/app`` and should use the mounted ``/app/credentials``
    path. Host-side one-shot commands on Windows run from the repository root;
    there, ``/app`` resolves to ``C:\\app`` and can point at an old local copy.
    Prefer the repo-local credentials directory when it exists and the operator
    did not set an explicit override.
    """
    configured = os.getenv("BROWSER_COOKIE_VAULT_DIR")
    if configured:
        return Path(configured)
    repo_local = Path.cwd() / "credentials" / "browser_cookies"
    if repo_local.is_dir():
        return repo_local
    return Path(_DEFAULT_BACKUP_DIR)


BACKUP_DIR: Path = _default_backup_dir()

INTERVAL_SECONDS: int = int(os.getenv("BROWSER_COOKIE_VAULT_INTERVAL_SECONDS", "300"))
KEEP_SNAPSHOTS: int = int(os.getenv("BROWSER_COOKIE_VAULT_KEEP_SNAPSHOTS", "10"))
HEALTH_PORT: int = int(os.getenv("BROWSER_COOKIE_VAULT_HEALTH_PORT", "8790"))
CDP_UNREACHABLE_WARN_EVERY: int = max(
    1,
    int(os.getenv("BROWSER_COOKIE_VAULT_CDP_WARN_EVERY", "12")),
)

# Auto-restore on startup is intentionally OFF by default: an eager restore
# could overwrite a working live session with a stale snapshot. Flip to "1"
# only when you understand the trade-off (e.g. right after a Chrome crash).
AUTORESTORE_ON_START: bool = os.getenv(
    "BROWSER_COOKIE_VAULT_AUTORESTORE", "0"
).strip().lower() in {"1", "true", "yes", "on"}

# Retry policy while Chrome is missing (starting, crashed, etc.). Exponential
# with a cap so a long outage doesn't turn into a tight-loop.
_BACKOFF_MIN = 5.0
_BACKOFF_MAX = 300.0
_BACKOFF_MULT = 2.0

# ─── Domain filter ───────────────────────────────────────────────────────────

# Primary hosts + first-party CDN suffixes for the 7 social platforms. Storing
# CDN cookies matters for Instagram/Meta/X where imagery + video servers set
# their own auth cookies (fbcdn, cdninstagram, twimg) that die alongside the
# main login when the profile SQLite is rewritten.
SOCIAL_COOKIE_DOMAINS: tuple[str, ...] = (
    "instagram.com",
    "threads.com",
    "threads.net",
    "tiktok.com",
    "lemon8-app.com",
    "x.com",
    "twitter.com",
    "facebook.com",
    "strava.com",
    "cdninstagram.com",
    "twimg.com",
    "fbcdn.net",
    "tiktokcdn.com",
    "ttwstatic.com",
)

# Auth markers are deliberately names only. Values are never logged or exposed.
AUTH_COOKIE_MARKERS: dict[str, tuple[str, ...]] = {
    "instagram": ("sessionid",),
    "facebook": ("c_user", "xs"),
    "x": ("auth_token", "ct0"),
    "strava": ("_strava4_session",),
    # `ttwid` is useful device state but can exist on logged-out/challenge pages.
    # Require a session-bearing marker too so weak TikTok snapshots do not replace
    # a restore point that can actually pass the login wall.
    "tiktok": ("ttwid", "sessionid"),
}

AUTH_COOKIE_DOMAIN_ROOTS: dict[str, tuple[str, ...]] = {
    "instagram": ("instagram.com",),
    "facebook": ("facebook.com",),
    "x": ("x.com", "twitter.com"),
    "strava": ("strava.com",),
    "tiktok": ("tiktok.com",),
}

# Fields Storage.setCookies rejects (they're read-only on the write side).
_READONLY_COOKIE_FIELDS = frozenset({"size", "session"})


def _host_of(cdp_url: str) -> tuple[str, int]:
    """Return ``(host, port)`` parsed from the CDP base URL."""
    from urllib.parse import urlparse

    parsed = urlparse(cdp_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 9222
    return host, port


def _host_header(cdp_url: str) -> str:
    """The Host header CDP will accept regardless of the address we dial.

    Chrome's DNS-rebinding guard blocks the DevTools HTTP endpoint when the
    Host header isn't ``localhost`` or ``127.0.0.1``. WebSocket upgrades share
    the same rule but honour ``--remote-allow-origins=*``.
    """
    _, port = _host_of(cdp_url)
    return f"localhost:{port}"


def _matches_social(domain: str) -> bool:
    """True if ``domain`` (with or without leading dot) belongs to a platform we care about."""
    if not domain:
        return False
    canon = domain.lstrip(".").lower()
    return any(canon == d or canon.endswith("." + d) for d in SOCIAL_COOKIE_DOMAINS)


def filter_social_cookies(cookies: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only cookies whose ``domain`` maps onto a known social platform."""
    return [c for c in cookies if _matches_social(str(c.get("domain", "")))]


def _domain_matches_roots(domain: str, roots: Iterable[str]) -> bool:
    canon = domain.lstrip(".").lower()
    return any(canon == root or canon.endswith("." + root) for root in roots)


def summarize_auth_markers(cookies: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """Return auth-cookie names present per platform, never values."""
    found: dict[str, set[str]] = {}
    for cookie in cookies:
        name = str(cookie.get("name", ""))
        domain = str(cookie.get("domain", ""))
        for platform, marker_names in AUTH_COOKIE_MARKERS.items():
            if name not in marker_names:
                continue
            if _domain_matches_roots(domain, AUTH_COOKIE_DOMAIN_ROOTS[platform]):
                found.setdefault(platform, set()).add(name)
    return {platform: sorted(names) for platform, names in sorted(found.items())}


def _auth_quality_score(payload: dict[str, Any]) -> int:
    """Score snapshot usefulness for preserving logged-in sessions."""
    summary = payload.get("auth_summary")
    if not isinstance(summary, dict):
        summary = summarize_auth_markers(payload.get("cookies") or [])
    complete_platforms = 0
    marker_count = 0
    for platform, required in AUTH_COOKIE_MARKERS.items():
        names = set(summary.get(platform) or [])
        marker_count += len(names)
        if all(name in names for name in required):
            complete_platforms += 1
    cookie_count = int(payload.get("cookie_count") or len(payload.get("cookies") or []))
    return complete_platforms * 1000 + marker_count * 10 + min(cookie_count, 999)


def _sanitize_for_set(cookie: dict[str, Any]) -> dict[str, Any]:
    """Strip fields that ``Storage.setCookies`` won't accept."""
    out = {k: v for k, v in cookie.items() if k not in _READONLY_COOKIE_FIELDS}
    ss = out.get("sameSite")
    # CDP returns None as the JSON null when unset; keep the field only if valid.
    if ss not in ("Strict", "Lax", "None"):
        out.pop("sameSite", None)
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _snapshot_name(ts: str) -> str:
    # `snapshot_YYYYMMDDTHHMMSSZ.json` — sortable + filesystem-safe.
    stamp = ts.replace(":", "").replace("-", "")
    return f"snapshot_{stamp}.json"


# ─── CDP client ──────────────────────────────────────────────────────────────


class CDPUnreachable(RuntimeError):
    """Raised when the CDP HTTP endpoint can't be reached (Chrome down)."""


def _should_warn_cdp_unreachable(consecutive_failures: int) -> bool:
    """Warn on the first Chrome-down failure, then periodically.

    The health endpoint already exposes every failure count and last error. A
    long intentional CDP outage should not flood Docker logs every retry.
    """
    if consecutive_failures <= 1:
        return True
    return consecutive_failures % CDP_UNREACHABLE_WARN_EVERY == 0


class CDPClient:
    """Minimal JSON-RPC 2.0 over WebSocket client for Chrome DevTools.

    Not a full CDP client — we only need ``Storage.getCookies`` /
    ``Storage.setCookies`` on the browser target, which does not require
    ``Target.attachToTarget`` gymnastics.
    """

    def __init__(self, cdp_url: str, session: aiohttp.ClientSession):
        self._cdp_url = cdp_url.rstrip("/")
        self._session = session
        self._host_header = _host_header(self._cdp_url)
        self._id = 0
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    async def connect(self) -> None:
        """Fetch ``/json/version`` and upgrade to the browser-level WS target."""
        version_url = f"{self._cdp_url}/json/version"
        try:
            async with self._session.get(
                version_url,
                headers={"Host": self._host_header},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    raise CDPUnreachable(f"{version_url} -> HTTP {resp.status}")
                info = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise CDPUnreachable(f"CDP HTTP fetch failed: {exc!r}") from exc

        ws_url = info.get("webSocketDebuggerUrl")
        if not ws_url:
            raise CDPUnreachable(f"no webSocketDebuggerUrl in {info!r}")

        # Rewrite the WS host so we dial the address we're actually configured
        # for (Chrome always announces `ws://localhost:...`).
        host, port = _host_of(self._cdp_url)
        ws_url = ws_url.replace(f"localhost:{port}", f"{host}:{port}")

        # aiohttp ≥ 3.11 deprecated float `timeout=` on ws_connect in favour of
        # ClientWSTimeout. Fall back to no explicit timeout on older builds; the
        # session-level timeout still bounds the initial handshake.
        ws_kwargs: dict[str, Any] = {
            "headers": {
                "Origin": f"http://localhost:{port}",
                "Host": self._host_header,
            },
            "max_msg_size": 0,  # unbounded; cookie payloads can be >1 MiB.
        }
        if hasattr(aiohttp, "ClientWSTimeout"):
            ws_kwargs["timeout"] = aiohttp.ClientWSTimeout(ws_close=10.0, ws_receive=30.0)
        try:
            self._ws = await self._session.ws_connect(ws_url, **ws_kwargs)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise CDPUnreachable(f"CDP WS upgrade failed: {exc!r}") from exc

    async def close(self) -> None:
        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._ws = None

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Round-trip a single JSON-RPC call and return its ``result``.

        Raises ``RuntimeError`` on protocol errors.  Ignores messages whose id
        doesn't match ours (protocol events, out-of-order replies).
        """
        if self._ws is None or self._ws.closed:
            raise CDPUnreachable("WS not connected")
        self._id += 1
        my_id = self._id
        payload = {"id": my_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self._ws.send_json(payload)
        while True:
            msg = await self._ws.receive(timeout=30)
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("id") != my_id:
                    continue  # event / unrelated response
                if "error" in data:
                    raise RuntimeError(f"CDP error for {method}: {data['error']}")
                return data.get("result", {})
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                raise CDPUnreachable(f"CDP WS closed mid-call ({method})")
            if msg.type == aiohttp.WSMsgType.ERROR:
                raise CDPUnreachable(f"CDP WS error ({method}): {self._ws.exception()!r}")


# ─── Vault ───────────────────────────────────────────────────────────────────


class CookieVault:
    """Backup + restore orchestrator around a :class:`CDPClient`."""

    def __init__(
        self,
        cdp_url: str = CHROME_CDP_URL,
        backup_dir: Path = BACKUP_DIR,
        keep_snapshots: int = KEEP_SNAPSHOTS,
    ):
        self.cdp_url = cdp_url
        self.backup_dir = backup_dir
        self.keep_snapshots = keep_snapshots
        # Health-endpoint state — populated by the daemon loop.
        self.last_backup_ts: str | None = None
        self.last_backup_count: int = 0
        self.last_error: str | None = None
        self.consecutive_failures: int = 0
        self.last_auth_summary: dict[str, list[str]] = {}
        self.last_quality_score: int = 0
        self.last_latest_preserved: bool = False
        self.last_preservation_reason: str | None = None

    # ── on-disk helpers ─────────────────────────────────────────────────────

    def latest_path(self) -> Path:
        return self.backup_dir / "latest.json"

    def _ensure_dir(self) -> None:
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        """Write ``payload`` to ``path`` atomically (tmp file + rename)."""
        self._ensure_dir()
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=".vault_",
            suffix=".json.tmp",
            dir=str(self.backup_dir),
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise

    def _prune_snapshots(self) -> None:
        """Keep only the ``self.keep_snapshots`` newest snapshot files."""
        snapshots = sorted(
            (p for p in self.backup_dir.glob("snapshot_*.json") if p.is_file()),
            key=lambda p: p.name,
        )
        excess = len(snapshots) - self.keep_snapshots
        for old in snapshots[:max(0, excess)]:
            with contextlib.suppress(OSError):
                old.unlink()

    # ── backup ──────────────────────────────────────────────────────────────

    async def backup(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        """Take one snapshot. Returns the payload we wrote."""
        client = CDPClient(self.cdp_url, session)
        await client.connect()
        try:
            result = await client.call("Storage.getCookies")
        finally:
            await client.close()

        all_cookies = result.get("cookies", []) or []
        social = filter_social_cookies(all_cookies)

        ts = _now_iso()
        auth_summary = summarize_auth_markers(social)
        payload = {
            "ts": ts,
            "cdp_url": self.cdp_url,
            "cookie_count": len(social),
            "total_seen": len(all_cookies),
            "auth_summary": auth_summary,
            "cookies": social,
        }
        payload["quality_score"] = _auth_quality_score(payload)

        self._atomic_write(self.backup_dir / _snapshot_name(ts), payload)
        latest_preserved = self._should_preserve_latest(payload)
        if latest_preserved:
            logger.warning(
                "cookie backup: preserved existing latest snapshot because new auth quality is lower "
                "(new_score=%s auth=%s)",
                payload["quality_score"],
                auth_summary,
            )
        else:
            self._atomic_write(self.latest_path(), payload)
        self._prune_snapshots()

        self.last_backup_ts = ts
        self.last_backup_count = len(social)
        self.last_error = None
        self.consecutive_failures = 0
        self.last_auth_summary = auth_summary
        self.last_quality_score = int(payload["quality_score"])
        self.last_latest_preserved = latest_preserved
        self.last_preservation_reason = "new_snapshot_lower_auth_quality" if latest_preserved else None

        # Per-domain summary keeps ops visibility high without dumping raw values.
        summary = self._summarize_by_domain(social)
        logger.info(
            "cookie backup: wrote %d social cookies (from %d total) domains=%s auth=%s latest_preserved=%s",
            len(social),
            len(all_cookies),
            summary,
            auth_summary,
            latest_preserved,
        )
        return payload

    def _should_preserve_latest(self, candidate: dict[str, Any]) -> bool:
        """Keep a better logged-in latest.json when Chrome reports a weaker jar."""
        latest = self.latest_path()
        if not latest.is_file():
            return False
        try:
            existing = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        existing_score = _auth_quality_score(existing)
        candidate_score = _auth_quality_score(candidate)
        if existing_score <= 0:
            return False
        return candidate_score < existing_score

    def latest_snapshot_status(self) -> dict[str, Any]:
        """Return safe metadata for the effective restore snapshot."""
        latest = self.latest_path()
        if not latest.is_file():
            return {
                "exists": False,
                "auth_summary": {},
                "quality_score": 0,
                "cookie_count": 0,
                "ts": None,
                "restorable": False,
                "error": "missing_latest_snapshot",
            }
        try:
            payload = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "exists": True,
                "auth_summary": {},
                "quality_score": 0,
                "cookie_count": 0,
                "ts": None,
                "restorable": False,
                "error": f"unreadable_latest_snapshot:{type(exc).__name__}",
            }

        cookies = payload.get("cookies") or []
        auth_summary = payload.get("auth_summary")
        if not isinstance(auth_summary, dict):
            auth_summary = summarize_auth_markers(cookies)
        quality_score = _auth_quality_score(payload)
        cookie_count = int(payload.get("cookie_count") or len(cookies))
        return {
            "exists": True,
            "auth_summary": auth_summary,
            "quality_score": quality_score,
            "cookie_count": cookie_count,
            "ts": payload.get("ts"),
            "restorable": bool(cookies) and quality_score > 0,
            "error": None,
        }

    def _summarize_by_domain(self, cookies: list[dict[str, Any]]) -> dict[str, int]:
        buckets: dict[str, int] = {}
        for c in cookies:
            dom = str(c.get("domain", "")).lstrip(".").lower()
            for target in SOCIAL_COOKIE_DOMAINS:
                if dom == target or dom.endswith("." + target):
                    buckets[target] = buckets.get(target, 0) + 1
                    break
        return buckets

    # ── restore ─────────────────────────────────────────────────────────────

    async def restore(self, session: aiohttp.ClientSession) -> dict[str, int]:
        """Push ``latest.json`` back into Chrome. Returns per-domain counts."""
        latest = self.latest_path()
        if not latest.is_file():
            raise FileNotFoundError(f"no snapshot at {latest}")
        payload = json.loads(latest.read_text(encoding="utf-8"))
        cookies = payload.get("cookies") or []
        if not cookies:
            logger.warning("restore skipped: snapshot has no cookies (%s)", latest)
            return {}

        client = CDPClient(self.cdp_url, session)
        await client.connect()
        per_domain: dict[str, int] = {}
        try:
            for raw in cookies:
                clean = _sanitize_for_set(raw)
                await client.call("Storage.setCookies", {"cookies": [clean]})
                dom = str(clean.get("domain", "")).lstrip(".").lower()
                per_domain[dom] = per_domain.get(dom, 0) + 1
        finally:
            await client.close()

        logger.info(
            "cookie restore: pushed %d cookies from %s domains=%s",
            sum(per_domain.values()),
            payload.get("ts"),
            per_domain,
        )
        return per_domain


# ─── Health endpoint ────────────────────────────────────────────────────────


def _build_health_app(vault: CookieVault) -> web.Application:
    async def health(_request: web.Request) -> web.Response:
        latest_status = vault.latest_snapshot_status()
        ok = (
            vault.last_error is None
            and vault.last_backup_ts is not None
            and bool(latest_status.get("restorable"))
        )
        return web.json_response(
            {
                "ok": ok,
                "last_backup": vault.last_backup_ts,
                "count": vault.last_backup_count,
                "auth_summary": vault.last_auth_summary,
                "quality_score": vault.last_quality_score,
                "latest_preserved": vault.last_latest_preserved,
                "preservation_reason": vault.last_preservation_reason,
                "effective_latest": latest_status,
                "error": vault.last_error,
                "consecutive_failures": vault.consecutive_failures,
                "cdp_url": vault.cdp_url,
                "backup_dir": str(vault.backup_dir),
                "interval_seconds": INTERVAL_SECONDS,
            }
        )

    app = web.Application()
    app.router.add_get("/health", health)
    return app


async def _serve_health(vault: CookieVault, port: int, stop_event: asyncio.Event) -> None:
    app = _build_health_app(vault)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("health endpoint listening on :%d", port)
    try:
        await stop_event.wait()
    finally:
        await runner.cleanup()


# ─── Daemon loop ────────────────────────────────────────────────────────────


async def _backup_loop(vault: CookieVault, interval: int, stop_event: asyncio.Event) -> None:
    """Backup forever with bounded-exponential backoff on Chrome-down."""
    backoff = _BACKOFF_MIN
    async with aiohttp.ClientSession() as session:
        if AUTORESTORE_ON_START and vault.latest_path().is_file():
            try:
                await vault.restore(session)
            except CDPUnreachable as exc:
                logger.warning("autorestore skipped, Chrome unreachable: %s", exc)
            except Exception:
                logger.exception("autorestore failed")

        while not stop_event.is_set():
            try:
                await vault.backup(session)
                backoff = _BACKOFF_MIN  # success resets the backoff.
                # Sleep the full interval, or wake on shutdown.
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
            except CDPUnreachable as exc:
                vault.consecutive_failures += 1
                vault.last_error = f"cdp_unreachable: {exc}"
                log = logger.warning if _should_warn_cdp_unreachable(vault.consecutive_failures) else logger.info
                log(
                    "backup failed (Chrome unreachable, attempt=%d): %s; sleeping %.1fs",
                    vault.consecutive_failures,
                    exc,
                    backoff,
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(_BACKOFF_MAX, backoff * _BACKOFF_MULT)
            except Exception as exc:
                vault.consecutive_failures += 1
                vault.last_error = f"unexpected: {type(exc).__name__}: {exc}"
                logger.exception("backup raised unexpectedly; sleeping %.1fs", backoff)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(_BACKOFF_MAX, backoff * _BACKOFF_MULT)


async def run_daemon() -> int:
    """Launch the health endpoint and backup loop until SIGTERM/SIGINT."""
    _configure_logging()
    vault = CookieVault()
    vault.backup_dir.mkdir(parents=True, exist_ok=True)
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for signame in ("SIGTERM", "SIGINT"):
        try:
            import signal as _signal

            loop.add_signal_handler(getattr(_signal, signame), stop_event.set)
        except (NotImplementedError, RuntimeError, AttributeError):
            # Windows event loop: no add_signal_handler. Fall back to KeyboardInterrupt.
            pass

    logger.info(
        "browser_cookie_vault starting cdp=%s dir=%s interval=%ds",
        vault.cdp_url,
        vault.backup_dir,
        INTERVAL_SECONDS,
    )
    tasks = [
        asyncio.create_task(_backup_loop(vault, INTERVAL_SECONDS, stop_event)),
        asyncio.create_task(_serve_health(vault, HEALTH_PORT, stop_event)),
    ]
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)
    return 0


async def run_once(mode: str) -> int:
    """One-shot ``backup`` or ``restore`` — the CLI entry points."""
    _configure_logging()
    vault = CookieVault()
    async with aiohttp.ClientSession() as session:
        if mode == "backup":
            payload = await vault.backup(session)
            print(json.dumps({"ok": True, "count": payload["cookie_count"], "ts": payload["ts"]}))
            return 0
        if mode == "restore":
            counts = await vault.restore(session)
            print(json.dumps({"ok": True, "restored": counts, "total": sum(counts.values())}))
            return 0
        print(f"unknown mode: {mode}", file=sys.stderr)
        return 2


# ─── Logging plumbing ───────────────────────────────────────────────────────


def _configure_logging() -> None:
    """Prefer the project's queue-based configurator, fall back to basicConfig.

    ``src.core.logging_config`` gives us the non-blocking QueueHandler pipeline
    used by every collector; if it's unavailable (e.g. someone runs this file
    stand-alone from a wheel), a plain ``basicConfig`` still lets us see logs.
    """
    try:
        from src.core.logging_config import configure_logging as _cfg

        _cfg()
    except Exception:
        logging.basicConfig(
            level=os.getenv("LOG_LEVEL", "INFO").upper(),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )


# ─── CLI ────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.tools.browser_cookie_vault",
        description="Chrome cookie vault: backup and restore social cookies via CDP.",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="daemon",
        choices=("daemon", "backup", "restore"),
        help="daemon (default): run forever; backup: one snapshot; restore: push latest.json.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    if args.mode == "daemon":
        return asyncio.run(run_daemon())
    return asyncio.run(run_once(args.mode))


if __name__ == "__main__":
    sys.exit(main())
