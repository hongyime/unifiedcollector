from __future__ import annotations

import re
from typing import Iterable

WHATSAPP_GROUP_RE = re.compile(r"(?:https?://)?chat\.whatsapp\.com/[A-Za-z0-9]+", re.IGNORECASE)
WHATSAPP_CONTACT_RE = re.compile(r"(?:https?://)?wa\.me/[0-9]+", re.IGNORECASE)


def extract_links(text: str) -> list[tuple[str, str]]:
    if not text:
        return []

    found: list[tuple[str, str]] = []
    for link in WHATSAPP_GROUP_RE.findall(text):
        normalized = link if link.lower().startswith("http") else f"https://{link}"
        found.append((normalized, "group_invite"))

    for link in WHATSAPP_CONTACT_RE.findall(text):
        normalized = link if link.lower().startswith("http") else f"https://{link}"
        found.append((normalized, "contact_link"))

    # preserve order while deduping per message
    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for link, link_type in found:
        if link in seen:
            continue
        seen.add(link)
        unique.append((link, link_type))
    return unique
