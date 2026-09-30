"""Pinterest profile probe (Do Next #3).

URL: https://www.pinterest.com/<username>/ (public).
Extracts display_name + bio + follower count + pin/board counts.

Special integration: Griffin's Pinterest tip (2025-03-28) - users who
sign up via Google get their Google email USERNAME as their Pinterest
handle. After a successful probe we enqueue a candidate
``<username>@gmail.com`` into the analyzer's email pool with
low-confidence source ``pinterest_google_signup_heuristic``. Holehe
(existing email_recognition pipeline) will validate downstream.

Source: tools.myosint.training "Pinterest User Bookmarklet" +
hatless1der.com/a-tremendously-valuable-osint-tip-for-pinterest.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from src.core.profile_only_collector import ProfileOnlyCollector, ProfileResult

logger = logging.getLogger(__name__)

_FULL_NAME_RE = re.compile(r'"full_name"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_BIO_RE = re.compile(r'"about"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_AVATAR_RE = re.compile(r'"image_xlarge_url"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_FOLLOWER_RE = re.compile(r'"follower_count"\s*:\s*(\d+)')
_PIN_COUNT_RE = re.compile(r'"pin_count"\s*:\s*(\d+)')
_BOARD_COUNT_RE = re.compile(r'"board_count"\s*:\s*(\d+)')


def _decode_json_str(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except Exception:
        return s


class PinterestCollector(ProfileOnlyCollector):
    SOURCE_NAME = "pinterest"
    PROFILE_TABLE = "pinterest_profiles"

    async def probe_profile(self, username: str) -> ProfileResult:
        url = f"https://www.pinterest.com/{username}/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                r = await client.get(url, headers=headers, timeout=10.0)
        except Exception as exc:
            return ProfileResult(exists=False, username=username, fields={},
                                 error=f"http:{type(exc).__name__}")

        if r.status_code == 404:
            return ProfileResult(exists=False, username=username, fields={})
        if r.status_code >= 400:
            return ProfileResult(exists=False, username=username, fields={},
                                 error=f"http:{r.status_code}")

        html = r.text
        name_m = _FULL_NAME_RE.search(html)
        if not name_m:
            return ProfileResult(exists=False, username=username, fields={})

        bio_m = _BIO_RE.search(html)
        avatar_m = _AVATAR_RE.search(html)
        follower_m = _FOLLOWER_RE.search(html)
        pin_count_m = _PIN_COUNT_RE.search(html)
        board_count_m = _BOARD_COUNT_RE.search(html)

        return ProfileResult(
            exists=True,
            username=username,
            fields={
                "display_name": _decode_json_str(name_m.group(1)),
                "bio": _decode_json_str(bio_m.group(1)) if bio_m else None,
                "avatar_url": _decode_json_str(avatar_m.group(1)) if avatar_m else None,
                "follower_count": int(follower_m.group(1)) if follower_m else None,
                "pin_count": int(pin_count_m.group(1)) if pin_count_m else None,
                "board_count": int(board_count_m.group(1)) if board_count_m else None,
            },
        )

    async def _upsert_profile(self, username: str, result: ProfileResult) -> None:
        """Override to also emit a low-confidence Google email heuristic per
        Griffin's Pinterest tip (2025-03-28).
        """
        await super()._upsert_profile(username, result)
        # Only emit the heuristic if the username LOOKS like a plausible
        # email local-part (no whitespace, reasonable length, all ASCII).
        if not (5 <= len(username) <= 64):
            return
        if not username.replace(".", "").replace("_", "").replace("-", "").isalnum():
            return

        candidate = f"{username.lower()}@gmail.com"
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO discovered_links (
                        source, source_table, source_record_id,
                        url, domain, link_type, status,
                        title, description, metadata
                    ) VALUES (
                        'pinterest', 'pinterest_profiles', $1,
                        $2, 'gmail.com', 'email_candidate', 'pending',
                        'Pinterest username -> Gmail heuristic',
                        'Pinterest username often maps to Google email prefix (Griffin 2025-03-28)',
                        $3::jsonb
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    username,
                    f"mailto:{candidate}",
                    json.dumps({
                        "email_candidate": candidate,
                        "source_heuristic": "pinterest_google_signup",
                        "confidence": 0.3,
                    }),
                )
        except Exception:
            logger.debug("pinterest email heuristic write failed", exc_info=True)


__all__ = ["PinterestCollector"]
