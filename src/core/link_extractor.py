import re
from urllib.parse import urlparse

_WHATSAPP_LINK_RE = re.compile(
    r'https?://(?:chat\.whatsapp\.com|wa\.me)/([A-Za-z0-9_\-]+)',
    re.IGNORECASE,
)

_GROUP_INVITE_PREFIXES = {"chat.whatsapp.com"}
_CONTACT_PREFIXES = {"wa.me"}


def extract_whatsapp_links(text: str) -> list[tuple[str, str]]:
    if not text:
        return []

    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    for match in _WHATSAPP_LINK_RE.finditer(text):
        url = match.group(0)
        normalized = url.rstrip("/").lower()
        if normalized in seen:
            continue
        seen.add(normalized)

        host = urlparse(url).netloc.lower()
        if host in _GROUP_INVITE_PREFIXES:
            link_type = "group_invite"
        elif host in _CONTACT_PREFIXES:
            code = match.group(1)
            if len(code) >= 20:
                link_type = "group_invite"
            else:
                link_type = "contact_link"
        else:
            link_type = "unknown"

        results.append((url, link_type))

    return results
