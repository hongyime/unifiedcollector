"""Post-login session state persistence for the instagram_dm collector
(Option A of #39).

`auth.AuthClient.login()` will (once implemented) return a set of session
tokens the MQTT client needs to authenticate. Meta invalidates sessions
after a period of inactivity + on any concurrent login from a different
device fingerprint — so we persist the tokens to disk and reload them on
restart, avoiding a fresh login flow every boot (a full login flow itself
is a bannable signal if done too often).

File layout: `<creds_dir>/<username>.session.json`. Written atomically via
tmp + rename. Human-readable JSON (never encrypted — the credentials dir
already needs OS-level file permissions to be secret; encrypting here
doesn't add protection beyond obscurity).

Local-only. No network activity — safe to import + call even with the
feature flag off.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SessionState:
    username: str
    # Set of cookies IG's mobile API sets on a successful login. Keeping the
    # names aligned with the response headers so it's obvious where each
    # value came from during debugging.
    sessionid: str | None = None
    ig_did: str | None = None
    mid: str | None = None
    ds_user_id: str | None = None
    rur: str | None = None
    csrftoken: str | None = None
    # MQTT-specific auth: the mobile MQTT connect takes a JSON blob in the
    # willmsg / username field. auth.py builds it from these values.
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    # Timestamps for observability. logged_in_at drives the refresh cadence.
    logged_in_at: float = 0.0
    last_seen_at: float = 0.0

    def is_valid(self) -> bool:
        """Cheap sanity check — does this session have the minimum needed
        to attempt an MQTT connect? Does not test the server side."""
        return bool(self.sessionid and self.ds_user_id and self.username)

    def is_stale(self, max_age_seconds: int = 86400) -> bool:
        """Session is treated as stale after 24h idle by default. auth.py's
        refresh() call is expected to re-login before this expires. Meta
        doesn't publish a TTL; 24h is the community-observed working
        window."""
        if not self.logged_in_at:
            return True
        return (time.time() - self.logged_in_at) > max_age_seconds


def _path(creds_dir: Path, username: str) -> Path:
    return Path(creds_dir) / f"{username}.session.json"


def load(creds_dir: Path, username: str) -> SessionState:
    """Return the persisted SessionState for `username`, or an empty one.

    Never fails on missing file / corrupt JSON — a broken session state
    is treated as "not logged in" and a fresh login will replace it.
    """
    if not username:
        raise ValueError("username required")
    p = _path(creds_dir, username)
    if not p.exists():
        return SessionState(username=username)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return SessionState(**{"username": username, **data})
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.warning(
            "instagram_dm session state for %s at %s is corrupt (%s); "
            "starting empty. Fresh login will be attempted on next collect() "
            "call if the feature flag is on.",
            username, p, e,
        )
        return SessionState(username=username)


def save(creds_dir: Path, state: SessionState) -> None:
    """Atomically persist `state` to disk. Called by auth.AuthClient after a
    successful login and periodically to refresh `last_seen_at` so the
    watchdog can detect a dead-session-but-alive-container split."""
    if not state.username:
        raise ValueError("SessionState.username required")
    creds_dir = Path(creds_dir)
    creds_dir.mkdir(parents=True, exist_ok=True)
    p = _path(creds_dir, state.username)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    tmp.replace(p)


def clear(creds_dir: Path, username: str) -> None:
    """Wipe the persisted session. Called on `logout` or when auth.py
    detects a `challenge_required` / `login_required` response (the account
    session has been server-side invalidated; keeping the stale token
    around risks confused-state where MQTT connect uses expired creds and
    triggers another auth attempt in a bannable loop)."""
    p = _path(creds_dir, username)
    if p.exists():
        try:
            p.unlink()
        except OSError as e:
            logger.warning("could not clear session %s: %s", p, e)
