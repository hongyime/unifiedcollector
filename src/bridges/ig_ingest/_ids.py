"""ID / shortcode / platform / verified-capture helpers.

Split out of ``__init__.py`` (split step 11); re-exported there.
"""
import logging
from .constants import BROWSER_DIAGNOSTIC_PLATFORMS
from .constants import KNOWN_PLATFORMS
from datetime import datetime
from datetime import timezone
import re

logger = logging.getLogger("social_ingest")


async def _execute_ddl_script(conn, script: str) -> None:
    for statement in (s.strip() for s in str(script or "").split(";")):
        if statement:
            await conn.execute(statement)


def _ig_date_from_id(content_id: str):
    """Instagram media ids encode creation time: (id>>23)+epoch. Return YYYYMMDD."""
    m = _IG_LONG.search(content_id or "")
    if not m:
        return None
    try:
        d = datetime.fromtimestamp(((int(m.group(0)) >> 23) + _IG_EPOCH) / 1000, tz=timezone.utc)
        if 2010 <= d.year <= datetime.now(tz=timezone.utc).year + 1:
            return d.strftime("%Y%m%d")
    except Exception:
        return None
    return None


def _shortcode_from_id(media_id: str):
    """Reverse Instagram's base64 media-id encoding into the public shortcode."""
    m = _IG_LONG.search(str(media_id or ""))
    if not m:
        return None
    try:
        n = int(m.group(0))
    except Exception:
        return None
    if n <= 0:
        return None
    out = []
    while n > 0:
        out.append(_SC_ALPHABET[n & 63])
        n >>= 6
    return "".join(reversed(out))


def _verify_url(platform, media_id):
    """Canonical openable URL for a post, for manual spot-checking."""
    sc = _shortcode_from_id(media_id)
    if not sc:
        return None
    if platform == "instagram":
        return f"https://instagram.com/p/{sc}/"
    if platform == "threads":
        return f"https://threads.com/post/{sc}/"
    return None


def _date_prefix(item, platform="") -> str:
    """YYYYMMDD for the filename so files sort chronologically. Prefer the post's
    own taken_at; for instagram fall back to the date encoded in the media id;
    finally fall back to today (collection)."""
    epoch = item.get("taken_at")
    if epoch is None:
        meta = item.get("meta") or {}
        epoch = meta.get("taken_at") if isinstance(meta, dict) else None
    try:
        if epoch:
            return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y%m%d")
    except (TypeError, ValueError, OSError):
        pass
    if platform == "instagram":
        d = _ig_date_from_id(str(item.get("content_id") or ""))
        if d:
            return d
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d")


def _norm_platform(p, *, allow_diagnostics: bool = False):
    p = (p or "instagram").strip().lower()
    if p in {"twitter", "twitter / x", "twitter/x", "x.com"}:
        p = "x"
    if allow_diagnostics and p in BROWSER_DIAGNOSTIC_PLATFORMS:
        return p
    return p if p in KNOWN_PLATFORMS else "instagram"


def _explicitly_verified_capture(value: object) -> bool:
    """Recognize only explicit platform badge/verification fields."""
    if not isinstance(value, dict):
        return False
    for key in _VERIFIED_CAPTURE_KEYS:
        flag = value.get(key)
        if flag is True or (isinstance(flag, int) and flag == 1) or (
            isinstance(flag, str) and flag.strip().lower() in {"true", "1", "yes"}
        ):
            return True
    # `owner` is commonly the logged-in collector account in extension bodies;
    # do not let that account's badge suppress unrelated targets.
    for key in ("author", "creator", "user", "profile", "channel", "meta", "metadata"):
        nested = value.get(key)
        if isinstance(nested, dict) and _explicitly_verified_capture(nested):
            return True
    return False


_IG_EPOCH = 1314220021721  # Instagram media-id custom epoch (ms)


_IG_LONG = re.compile(r"\d{8,}")


# Instagram/Threads shortcode alphabet — numeric media pk → the ~11-char code in
# the public URL (instagram.com/p/<code>, threads.com/post/<code>). The extension
# sometimes stores a wrong/long "code"; we derive the canonical one from the pk so
# verification URLs always open.
_SC_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


_VERIFIED_CAPTURE_KEYS = (
    "is_verified", "verified", "isVerified", "is_blue_verified", "isBlueVerified",
    "blue_verified", "blueVerified", "author_is_verified", "author_verified",
    "verified_author", "is_verified_merchant", "isVerifiedMerchant", "verifiedMerchant",
    "channel_is_verified", "channelIsVerified", "verified_badge", "verifiedBadge",
)


_VERIFIED_FILTER_PLATFORMS = frozenset({
    "instagram", "snapchat", "pinterest", "x", "threads", "tiktok", "youtube",
})
