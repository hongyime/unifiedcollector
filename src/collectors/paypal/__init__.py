"""PayPal.me profile probe (Do Next #3).

URL: https://www.paypal.com/paypalme/<username> (public).
Extracts display_name + avatar_url + currency preference (if shown).

**Ethics gate:** PAYPAL_PROBE_ENABLED defaults to 0. This is a
finance-adjacent surface; operator opts in per case, not always-on.
See POLICY.md.

Source: tools.myosint.training "PayPal Profile Bookmarklet".
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from src.core.profile_only_collector import ProfileOnlyCollector, ProfileResult

logger = logging.getLogger(__name__)

_DISPLAY_NAME_RE = re.compile(r'"displayName"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_AVATAR_RE = re.compile(r'"profilePicture"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_CURRENCY_RE = re.compile(r'"currencyCode"\s*:\s*"([A-Z]{3})"')
_BUSINESS_RE = re.compile(r'"accountType"\s*:\s*"(BUSINESS|MERCHANT|PREMIER)"')


def _decode_json_str(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except Exception:
        return s


class PayPalCollector(ProfileOnlyCollector):
    SOURCE_NAME = "paypal"
    PROFILE_TABLE = "paypal_profiles"

    @property
    def enabled(self) -> bool:
        # Explicit override: default DISABLED regardless of the base class's
        # default 1. Ethics gate per POLICY.md.
        import os
        return os.getenv("PAYPAL_PROBE_ENABLED", "0") == "1"

    async def probe_profile(self, username: str) -> ProfileResult:
        url = f"https://www.paypal.com/paypalme/{username}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
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
        # PayPal serves an SPA shell; if we didn't see the display name, treat as no profile.
        name_m = _DISPLAY_NAME_RE.search(html)
        if not name_m:
            return ProfileResult(exists=False, username=username, fields={})

        avatar_m = _AVATAR_RE.search(html)
        currency_m = _CURRENCY_RE.search(html)
        business_m = _BUSINESS_RE.search(html)

        return ProfileResult(
            exists=True,
            username=username,
            fields={
                "display_name": _decode_json_str(name_m.group(1)),
                "avatar_url": _decode_json_str(avatar_m.group(1)) if avatar_m else None,
                "paypalme_currency": currency_m.group(1) if currency_m else None,
                "paypalme_business": bool(business_m),
            },
        )


__all__ = ["PayPalCollector"]
