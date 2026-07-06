"""Instagram mobile-app device fingerprint (Option A of #39).

Meta actively correlates device fingerprint CHURN as a bot signal — an account
whose device_id / family_device_id / phone_id changes between logins is far
more likely to be flagged than one with stable identifiers. This module owns
generation + persistence of those identifiers so a restart of the collector
container never regenerates them.

Local-only. No network activity — safe to import + call even with the feature
flag off. The `Device` returned here is later handed to `auth.AuthClient` for
inclusion in the `/api/v1/accounts/login/` payload.

Fingerprint structure (see mautrix-meta messagix/session for the canonical
list; instagrapi has an older but broadly compatible schema):

  device_id        — UUID4, sent as the `X-IG-Device-ID` header
  family_device_id — UUID4, sent as `X-IG-Family-Device-ID` (Messenger link)
  phone_id         — UUID4, sent as `phone_id` field in some endpoints
  ig_did           — 16-hex-byte string, seeds the `ig_did` cookie
  advertising_id   — UUID4, mimics Google Play Services advertising ID
  android_id       — 16-hex-byte string, mimics Android SSAID
  hw_model / manufacturer / os_version — hard-coded plausible values so
                     User-Agent + `X-IG-Device-Info` don't drift.

Persisted to `<creds_dir>/<username>.device.json` alongside the credentials
file. Never modifies the credentials file itself (that stays plain-text /
human-editable).
"""
from __future__ import annotations

import json
import logging
import secrets
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Device:
    device_id: str
    family_device_id: str
    phone_id: str
    ig_did: str
    advertising_id: str
    android_id: str
    hw_model: str = "SM-G973F"
    manufacturer: str = "samsung"
    android_release: str = "13"
    android_sdk: int = 33
    dpi: str = "480dpi"
    resolution: str = "1080x2340"
    locale: str = "en_US"
    country_code: int = 1


def _new_device(hw_model: str = "SM-G973F") -> Device:
    """Fresh set of identifiers. Deliberately doesn't take a seed — a bot-
    detected account that reuses an identifier from a previously-banned one
    is worse than fresh churn, so we generate cryptographically random
    UUIDs every time this is called. Existing devices are loaded from disk
    via `load_or_create`, not regenerated."""
    return Device(
        device_id=str(uuid.uuid4()),
        family_device_id=str(uuid.uuid4()),
        phone_id=str(uuid.uuid4()),
        ig_did=secrets.token_hex(16),
        advertising_id=str(uuid.uuid4()),
        android_id=secrets.token_hex(8),
        hw_model=hw_model,
    )


def load_or_create(creds_dir: Path, username: str) -> Device:
    """Return the persisted Device for `username`, or generate + persist one.

    Persistence path: `<creds_dir>/<username>.device.json`. If the file exists
    and parses cleanly, return the parsed Device. Otherwise generate a new
    Device, write it atomically, return it.

    Never generates a Device without also persisting — that would silently
    reset the fingerprint on the next call.
    """
    if not username:
        raise ValueError("username required")
    creds_dir = Path(creds_dir)
    creds_dir.mkdir(parents=True, exist_ok=True)
    path = creds_dir / f"{username}.device.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Fill defaults for any missing fields (schema migration friendly).
            return Device(**{**asdict(_new_device()), **data})
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning(
                "instagram_dm device fingerprint for %s at %s is corrupt (%s); "
                "regenerating. Note: Meta correlates identifier churn as a "
                "bot signal — investigate if this repeats.",
                username, path, e,
            )
    dev = _new_device()
    # Atomic write via tmp + rename so a crash mid-write can't leave a
    # half-serialized file that trips the loader on next boot.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(dev), indent=2), encoding="utf-8")
    tmp.replace(path)
    logger.info("instagram_dm generated fresh device fingerprint for %s -> %s",
                username, path.name)
    return dev
