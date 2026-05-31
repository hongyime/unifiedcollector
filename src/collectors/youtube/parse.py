"""Pure parsing helpers for the youtube collector.

Extracted from the collector class (STAGE 2 of the per-package refactor). Pure,
side-effect-free string/timestamp transforms — no ``self``, no I/O. The collector
keeps thin staticmethod shims delegating here.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone


def vtt_to_text(vtt: str) -> str:
    """Strip WebVTT timing/header/style blocks and de-duplicate consecutive cue lines.
    YouTube auto-captions carry a rolling overlap; the simple de-dup below drops most of it."""
    lines: list[str] = []
    last = None
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("Kind:") or line.startswith("Language:") or line.startswith("NOTE"):
            continue
        if "-->" in line:
            continue
        if line.isdigit():
            continue
        # Strip in-line VTT tags like <00:00:01.000><c> and </c>
        cleaned = re.sub(r"<[^>]+>", "", line).strip()
        if not cleaned:
            continue
        if cleaned == last:
            continue
        lines.append(cleaned)
        last = cleaned
    return "\n".join(lines)


def parse_relative_timestamp(text: str) -> datetime | None:
    """Best-effort parse for YouTube relative timestamps like '3 days ago' or '1 month ago (edited)'."""
    if not text:
        return None
    m = re.match(r"\s*(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", text.strip(), re.IGNORECASE)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    seconds = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000, "year": 31536000}.get(unit, 0)
    if not seconds:
        return None
    ts = datetime.now(timezone.utc).timestamp() - n * seconds
    return datetime.fromtimestamp(ts, tz=timezone.utc)
