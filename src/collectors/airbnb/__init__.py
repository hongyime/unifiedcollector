"""Airbnb profile probe (Do Next #3).

URL: https://www.airbnb.com/users/show/<numeric_id> OR
     https://www.airbnb.com/users/profile/<username>

Note: Airbnb has been extremely SPA-heavy for years - the initial
HTML response contains most profile fields inside a Redux-style JSON
blob (`data-injector`), which we can regex. If Airbnb ships a
Cloudflare challenge / CAPTCHA, we get an HTML wall with no fields
and return exists=False without erroring.

Source: tools.myosint.training "Airbnb User Bookmarklet".
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date

import httpx

from src.core.profile_only_collector import ProfileOnlyCollector, ProfileResult

logger = logging.getLogger(__name__)

# Airbnb serializes user data inside a data-state JSON blob. Field names
# below match what the bookmarklet at tools.myosint.training reads.
_DISPLAY_RE = re.compile(r'"first_name"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_LAST_RE = re.compile(r'"last_name"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_AVATAR_RE = re.compile(r'"picture_url"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_HOST_SINCE_RE = re.compile(r'"created_at"\s*:\s*"(\d{4}-\d{2}-\d{2})')
_SUPERHOST_RE = re.compile(r'"is_superhost"\s*:\s*(true|false)')
_LOCATION_RE = re.compile(r'"location"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_LISTINGS_RE = re.compile(r'"listings_count"\s*:\s*(\d+)')
_REVIEWS_RE = re.compile(r'"reviews_count"\s*:\s*(\d+)')


def _decode_json_str(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except Exception:
        return s


class AirbnbCollector(ProfileOnlyCollector):
    SOURCE_NAME = "airbnb"
    PROFILE_TABLE = "airbnb_profiles"

    async def probe_profile(self, username: str) -> ProfileResult:
        # Prefer the profile path (works with username string); Airbnb
        # will redirect to /users/show/<numeric> internally.
        url = f"https://www.airbnb.com/users/profile/{username}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                r = await client.get(url, headers=headers, timeout=15.0)
        except Exception as exc:
            return ProfileResult(exists=False, username=username, fields={},
                                 error=f"http:{type(exc).__name__}")

        if r.status_code == 404:
            return ProfileResult(exists=False, username=username, fields={})
        if r.status_code >= 400:
            return ProfileResult(exists=False, username=username, fields={},
                                 error=f"http:{r.status_code}")

        html = r.text
        first_m = _DISPLAY_RE.search(html)
        if not first_m:
            # No profile data in the HTML shell = likely gone / anti-bot wall.
            return ProfileResult(exists=False, username=username, fields={})

        last_m = _LAST_RE.search(html)
        avatar_m = _AVATAR_RE.search(html)
        host_since_m = _HOST_SINCE_RE.search(html)
        superhost_m = _SUPERHOST_RE.search(html)
        location_m = _LOCATION_RE.search(html)
        listings_m = _LISTINGS_RE.search(html)
        reviews_m = _REVIEWS_RE.search(html)

        display_name = _decode_json_str(first_m.group(1))
        if last_m:
            display_name = f"{display_name} {_decode_json_str(last_m.group(1))}"

        host_since_parsed: date | None = None
        if host_since_m:
            try:
                host_since_parsed = date.fromisoformat(host_since_m.group(1))
            except ValueError:
                host_since_parsed = None

        return ProfileResult(
            exists=True,
            username=username,
            fields={
                "display_name": display_name,
                "avatar_url": _decode_json_str(avatar_m.group(1)) if avatar_m else None,
                "host_since": host_since_parsed,
                "superhost": (superhost_m.group(1) == "true") if superhost_m else None,
                "location_city": _decode_json_str(location_m.group(1)) if location_m else None,
                "properties_count": int(listings_m.group(1)) if listings_m else None,
                "reviews_count": int(reviews_m.group(1)) if reviews_m else None,
            },
        )


__all__ = ["AirbnbCollector"]
