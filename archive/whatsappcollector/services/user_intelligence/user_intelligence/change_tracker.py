from __future__ import annotations

from typing import Any

from .config import settings


def _normalize_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


class ChangeTracker:
    def detect_changes(self, payload: dict[str, Any], last_known: dict[str, str]) -> list[tuple[str, str, str]]:
        changes: list[tuple[str, str, str]] = []
        for field_name in settings.tracked_fields:
            incoming = _normalize_value(payload.get(field_name))
            previous = _normalize_value(last_known.get(field_name, ""))

            # Never overwrite non-empty with empty.
            if previous and not incoming:
                continue

            if incoming != previous:
                changes.append((field_name, previous, incoming))
                last_known[field_name] = incoming
        return changes


change_tracker = ChangeTracker()
