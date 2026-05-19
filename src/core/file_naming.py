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


def parse_filename(filename: str) -> dict | None:
    """Parse a standard filename back into its components. Returns None on failure.

    The tricky part: entity_name can contain underscores, so we anchor the
    fixed-format fields (source, entity_id on the left; content_type,
    content_id, timestamp on the right) and let entity_name take whatever
    remains in the middle.
    """
    stem, _, ext = filename.rpartition(".")
    if not stem:
        return None
    # source and entity_id are single tokens (no underscores after sanitize).
    # content_type and content_id likewise. timestamp is \d{8}_\d{6}.
    # entity_name gets everything between entity_id and content_type.
    pattern = r"^([a-z0-9]+)_([a-z0-9._-]+?)_(.+)_([a-z0-9]+)_([a-z0-9._-]+)_(\d{8}_\d{6})$"
    m = re.match(pattern, stem)
    if not m:
        return None
    return {
        "source": m.group(1),
        "entity_id": m.group(2),
        "entity_name": m.group(3),
        "content_type": m.group(4),
        "content_id": m.group(5),
        "timestamp": m.group(6),
        "extension": ext,
    }
