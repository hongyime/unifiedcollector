"""
Extractor — scans raw message text for Telegram link patterns.

Stateless: no database access, no Telegram API calls.
All methods are pure functions of their inputs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Module-level compiled regex constants (compiled once at import time)
# ---------------------------------------------------------------------------

# Pattern 1: Invite links — t.me/+{hash}
INVITE_LINK_RE = re.compile(
    r'(?:https?://)?t\.me/\+(?P<hash>[A-Za-z0-9]+)',
    re.IGNORECASE,
)

# Pattern 2: Public links via t.me — t.me/{username}
PUBLIC_LINK_RE = re.compile(
    r'(?:https?://)?t\.me/(?!\+)(?P<username>\w+)',
    re.IGNORECASE,
)

# Pattern 3: Public links via telegram.me — telegram.me/{username}
TELEGRAM_ME_RE = re.compile(
    r'(?:https?://)?telegram\.me/(?P<username>\w+)',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ExtractedLink:
    link: str            # normalised (lowercase) canonical link string
    link_type: str       # 'group' for invite links, 'unknown' for public links
    is_bot_link: bool    # True if username contains bot keyword
    raw_message_id: int  # collector.raw_messages.id of the source message


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

class Extractor:
    """
    Stateless extractor.  No database access.  No Telegram API calls.
    """

    def _normalise_link(self, raw: str) -> str:
        """
        Normalise a raw matched link string to its canonical lowercase form.

        Rules (applied in order):
          1. Strip leading/trailing whitespace.
          2. Convert to lowercase.
          3. Strip any trailing slash.
          4. Replace 'telegram.me/' with 't.me/'.
          5. Strip 'https?://' prefix.
        """
        s = raw.strip().lower().rstrip('/')
        s = s.replace('telegram.me/', 't.me/')
        s = re.sub(r'^https?://', '', s)
        return s

    def _is_bot_link(self, username: str) -> bool:
        """
        Return True iff the username contains the substring 'bot' (case-insensitive).
        """
        return 'bot' in username.lower()

    def extract_links(self, payload_text: str) -> list[ExtractedLink]:
        """
        Scan payload_text for Telegram link patterns and return one ExtractedLink
        per unique normalised link found.

        Returns [] for empty/None input or when no links are present.
        raw_message_id is set to 0; the caller is responsible for filling it in.
        """
        if not payload_text:
            return []

        seen: set[str] = set()
        results: list[ExtractedLink] = []

        # --- Invite links (t.me/+hash) ---
        for match in INVITE_LINK_RE.finditer(payload_text):
            raw = f"t.me/+{match.group('hash')}"
            normalised = self._normalise_link(raw)
            if normalised not in seen:
                seen.add(normalised)
                results.append(ExtractedLink(
                    link=normalised,
                    link_type='group',
                    is_bot_link=False,  # invite links have no username
                    raw_message_id=0,
                ))

        # --- Public links (t.me/{username} and telegram.me/{username}) ---
        for pattern in (PUBLIC_LINK_RE, TELEGRAM_ME_RE):
            for match in pattern.finditer(payload_text):
                username = match.group('username')
                raw = f"t.me/{username}"
                normalised = self._normalise_link(raw)
                if normalised not in seen:
                    seen.add(normalised)
                    results.append(ExtractedLink(
                        link=normalised,
                        link_type='unknown',
                        is_bot_link=self._is_bot_link(username),
                        raw_message_id=0,
                    ))

        return results
