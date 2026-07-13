import re
from urllib.parse import urlparse

_WHATSAPP_LINK_RE = re.compile(
    r'https?://(?:chat\.whatsapp\.com|wa\.me)/([A-Za-z0-9_\-]+)',
    re.IGNORECASE,
)

_GROUP_INVITE_PREFIXES = {"chat.whatsapp.com"}
_CONTACT_PREFIXES = {"wa.me"}

# Any http(s) URL — for Tier 6 cross-platform link discovery (feed the spider),
# not just WhatsApp invites. Trailing punctuation is trimmed by the caller.
_ANY_URL_RE = re.compile(r'https?://[^\s<>"\'\)\]]+', re.IGNORECASE)


def extract_all_links(text: str) -> list[tuple[str, str]]:
    """Extract EVERY http(s) URL from text with a coarse link_type: WhatsApp
    group invites -> 'group_invite', wa.me -> 'contact_link', all else -> 'url'.
    Deduped. Used for message-text link discovery (Tier 6)."""
    if not text:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _ANY_URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;)]}\"'")
        norm = url.rstrip("/").lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        try:
            host = urlparse(url).netloc.lower()
        except ValueError:
            host = ""  # malformed URL (e.g. bad IPv6) — still store as a plain url
        if host in _GROUP_INVITE_PREFIXES:
            link_type = "group_invite"
        elif host in _CONTACT_PREFIXES:
            link_type = "contact_link"
        else:
            link_type = "url"
        out.append((url, link_type))
    return out


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
