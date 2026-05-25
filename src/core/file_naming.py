import re
from datetime import datetime, timezone


def sanitize_name(name: str) -> str:
    """Lowercase, replace spaces/special chars with underscores, collapse runs."""
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9._-]", "_", name)
    name = re.sub(r"_+", "_", name)
    return name.strip("_")


def make_timestamp(dt: datetime | None = None) -> str:
    """Format datetime as YYYYMMDD_HHMMSS. Uses UTC now if not provided."""
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y%m%d_%H%M%S")


def build_filename(
    source: str,
    entity_id: str,
    entity_name: str,
    content_type: str,
    content_id: str,
    timestamp: str | datetime | None = None,
    extension: str = "jpg",
) -> str:
    """Build filename per standard: {source}_{eid}_{ename}_{type}_{cid}_{ts}.{ext}

    Example: instagram_123456789_johndoe_post_987654321_20240115_143022.jpg
    """
    if isinstance(timestamp, datetime):
        timestamp = make_timestamp(timestamp)
    elif timestamp is None:
        timestamp = make_timestamp()

    ext = extension.lstrip(".")
    parts = [
        sanitize_name(source),
        sanitize_name(str(entity_id)),
        sanitize_name(entity_name),
        sanitize_name(content_type),
        sanitize_name(str(content_id)),
        timestamp,
    ]
    return f"{'_'.join(parts)}.{ext}"


def parse_filename(filename: str, *, source_name: str | None = None) -> dict | None:
    """Parse a standard filename back into its components. Returns None on failure.

    Filename format produced by build_filename():
      {source}_{entity_id}_{entity_name}_{content_type}_{content_id}_{ts}.{ext}

    Underscore-disambiguation is fundamentally hard because entity_name,
    content_type, and content_id can each themselves contain underscores
    (sanitize_name() preserves them). We disambiguate by anchoring:

      * timestamp: fixed shape `\\d{8}_\\d{6}` at the end (15 chars).
      * source: caller may pass `source_name` to anchor the left side
        unambiguously. Without it we fall back to the first underscore
        token, which works only when source has no underscores (all real
        sources today do — instagram, youtube, tiktok, …).
      * Then we use a known-content-type allow-list to find the
        content_type/content_id boundary. If both heuristics fail we
        return None rather than silently misattribute fields.

    Callers that store `_known_ids` should always pass source_name to get
    a deterministic parse — without it, content_id could be off by one
    underscore and cause duplicate downloads.
    """
    stem, _, ext = filename.rpartition(".")
    if not stem:
        return None

    # 1. Strip the timestamp off the right.
    ts_match = re.search(r"_(\d{8}_\d{6})$", stem)
    if not ts_match:
        return None
    timestamp = ts_match.group(1)
    head = stem[: ts_match.start()]

    # 2. Strip the source off the left.
    if source_name is not None:
        prefix = sanitize_name(source_name) + "_"
        if not head.startswith(prefix):
            return None
        source = sanitize_name(source_name)
        rest = head[len(prefix):]
    else:
        # Legacy path: assume source has no underscore (true for current
        # collectors). Take everything before the first '_'.
        if "_" not in head:
            return None
        source, rest = head.split("_", 1)

    # 3. Strip entity_id off the next-leftmost '_'. entity_id is a sanitized
    # platform identifier — by convention these are single tokens.
    if "_" not in rest:
        return None
    entity_id, rest = rest.split("_", 1)

    # 4. The remainder is `{entity_name}_{content_type}_{content_id}`.
    # All three may contain underscores (rare for content_type/id, common
    # for entity_name). To find the type/id boundary we use an allow-list
    # of known content types. If none matches, fall back to the rsplit
    # heuristic (last two tokens = type, id) and return whatever we get.
    _KNOWN_TYPES = {
        "post", "image", "video", "story", "reel", "comment",
        "transcript", "metadata", "thumbnail", "audio",
        "profile_photo", "profile",
    }
    entity_name = content_type = content_id = None
    # Try every known type as a candidate boundary.
    for ct in sorted(_KNOWN_TYPES, key=len, reverse=True):
        marker = f"_{ct}_"
        idx = rest.rfind(marker)
        if idx < 0:
            continue
        candidate_name = rest[:idx]
        candidate_cid = rest[idx + len(marker):]
        if candidate_name and candidate_cid:
            entity_name = candidate_name
            content_type = ct
            content_id = candidate_cid
            break

    # Fallback: legacy 2-token rsplit (works when type/id are single tokens).
    if content_type is None:
        parts = rest.rsplit("_", 2)
        if len(parts) != 3:
            return None
        entity_name, content_type, content_id = parts
        if not entity_name:
            return None

    # Validate field shapes — same character classes sanitize_name() emits.
    _TOKEN = re.compile(r"^[a-z0-9._-]+$")
    if not (_TOKEN.match(source) and _TOKEN.match(entity_id)
            and _TOKEN.match(content_id)):
        return None

    return {
        "source": source,
        "entity_id": entity_id,
        "entity_name": entity_name,
        "content_type": content_type,
        "content_id": content_id,
        "timestamp": timestamp,
        "extension": ext,
    }
