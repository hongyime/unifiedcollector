"""Snapchat profile probe (Do Next #3).

URL: https://snapchat.com/add/<username> (public, unauthenticated).
Extracts display_name, bitmoji URL, and Bitmoji version number
(Griffin's rollback technique - version integer in the URL indicates
which avatar iteration is current; older versions accessible by
decrementing).

Source: tools.myosint.training "Snapchat User ID Bookmarklet" +
"Snapchat Bitmoji Historical Avatar Viewer".
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import httpx

from src.core.profile_only_collector import ProfileOnlyCollector, ProfileResult

logger = logging.getLogger(__name__)

_BITMOJI_URL_RE = re.compile(r'"bitmoji"\s*:\s*{[^}]*"avatar"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_BITMOJI_VER_RE = re.compile(r"_(\d+)\.webp")
_DISPLAY_NAME_RE = re.compile(r'"displayName"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_SNAP_SCORE_RE = re.compile(r'"snapScore"\s*:\s*(\d+)')


def _decode_json_str(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except Exception:
        return s


class SnapchatCollector(ProfileOnlyCollector):
    SOURCE_NAME = "snapchat"
    PROFILE_TABLE = "snapchat_profiles"

    async def probe_profile(self, username: str) -> ProfileResult:
        url = f"https://snapchat.com/add/{username}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        }
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                r = await client.get(url, headers=headers, timeout=10.0)
        except Exception as exc:
            return ProfileResult(exists=False, username=username, fields={}, error=f"http:{type(exc).__name__}")

        if r.status_code == 404:
            return ProfileResult(exists=False, username=username, fields={})
        if r.status_code >= 400:
            return ProfileResult(exists=False, username=username, fields={},
                                 error=f"http:{r.status_code}")

        html = r.text
        display_name_m = _DISPLAY_NAME_RE.search(html)
        bitmoji_m = _BITMOJI_URL_RE.search(html)
        snap_score_m = _SNAP_SCORE_RE.search(html)

        bitmoji_url = _decode_json_str(bitmoji_m.group(1)) if bitmoji_m else None
        bitmoji_version: Optional[int] = None
        if bitmoji_url:
            v = _BITMOJI_VER_RE.search(bitmoji_url)
            if v:
                try:
                    bitmoji_version = int(v.group(1))
                except ValueError:
                    bitmoji_version = None

        # Empty display name + no bitmoji + no snap_score = probably a page
        # that redirected but has no real profile. Treat as not_found.
        if not display_name_m and not bitmoji_url and not snap_score_m:
            return ProfileResult(exists=False, username=username, fields={})

        return ProfileResult(
            exists=True,
            username=username,
            fields={
                "display_name": _decode_json_str(display_name_m.group(1)) if display_name_m else None,
                "bitmoji_url": bitmoji_url,
                "bitmoji_version": bitmoji_version,
                "snap_score": int(snap_score_m.group(1)) if snap_score_m else None,
            },
        )


__all__ = ["SnapchatCollector"]
