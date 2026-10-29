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
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from src.core.profile_only_collector import ProfileOnlyCollector, ProfileResult
from src.collectors.native_platform_scrape import (
    claim_native_target,
    finish_native_target,
    seed_native_targets,
)

logger = logging.getLogger(__name__)

_FULL_NAME_RE = re.compile(r'"full_name"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_BIO_RE = re.compile(r'"about"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_AVATAR_RE = re.compile(r'"image_xlarge_url"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_FOLLOWER_RE = re.compile(r'"follower_count"\s*:\s*(\d+)')
_PIN_COUNT_RE = re.compile(r'"pin_count"\s*:\s*(\d+)')
_BOARD_COUNT_RE = re.compile(r'"board_count"\s*:\s*(\d+)')
_FOLLOWING_COUNT_RE = re.compile(
    r'"(?:following_count|followingCount)"\s*:\s*["\']?(\d[\d,]*(?:\.\d+)?\s*[KMB]?)', re.I,
)
_MONTHLY_VIEWS_RE = re.compile(
    r'"(?:monthly_views|monthlyViews)"\s*:\s*["\']?(\d[\d,]*(?:\.\d+)?\s*[KMB]?)', re.I,
)
_PIN_PATH_RE = re.compile(r"^/pin/(\d+)(?:/|$)", re.IGNORECASE)
_COUNT_TOKEN = r"(\d[\d,]*(?:\.\d+)?\s*[KMB]?)"
_BOARD_EXCLUDED = {"_created", "_saved", "pin", "search", "explore"}
_PROFILE_TAB_PATHS = {
    "_created", "_saved", "_followers", "_following", "_boards", "_pins",
    "created", "saved", "followers", "following", "boards", "pins",
}
_PRIVATE_MARKER_RE = re.compile(
    r"secret\s+board|private\s+board|(?:[\"']?is_secret[\"']?|[\"']?isSecret[\"']?)\s*[:=]\s*true",
    re.IGNORECASE,
)
_PINTEREST_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_PINTEREST_REDIRECTS = 5
_MAX_PINTEREST_PAGE_BYTES = 5 * 1024 * 1024


class _PinterestPublicHttpError(RuntimeError):
    def __init__(self, status_code: int, retry_after_seconds: int | None = None):
        self.status_code = int(status_code)
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"public_http_{self.status_code}")


_PINTEREST_NATIVE_ERROR_CODES = frozenset({
    "unsupported_public_page",
    "public_redirect_rejected",
    "public_redirect_limit",
    "public_redirect_missing_location",
    "public_page_too_large",
    "verified_account_excluded",
    "public_profile_identity_unconfirmed",
    "public_visibility_unconfirmed",
    "public_target_redirected",
    "private_content_rejected",
    "empty_public_page",
    "following_auth_required",
    "following_owner_unconfirmed",
    "following_page_unavailable",
    "following_roster_unconfirmed",
    "following_storage_state_unavailable",
})
_PINTEREST_FOLLOWING_REFRESH_SECONDS = 24 * 60 * 60
_PINTEREST_FOLLOWING_FAILURE_RETRY_SECONDS = 6 * 60 * 60
_PINTEREST_FOLLOWING_HANDLE_CAP = 250


def _pinterest_native_error_code(exc: Exception) -> str:
    if isinstance(exc, _PinterestPublicHttpError):
        return f"public_http_{exc.status_code}"
    message = str(exc)
    if message in _PINTEREST_NATIVE_ERROR_CODES:
        return message
    return type(exc).__name__.lower()


def _retry_after_seconds(value: Any, *, now: datetime | None = None) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        seconds = int(text)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = int((retry_at - (now or datetime.now(timezone.utc))).total_seconds())
    return min(7 * 24 * 60 * 60, max(1, seconds))


def _decode_json_str(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except Exception:
        return s


def _compact_count(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(r"\s*(\d[\d,]*(?:\.\d+)?)\s*([KMB]?)\s*", value, re.I)
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2).upper()]
    result = int(number * multiplier)
    return result if 0 <= result <= 9_223_372_036_854_775_807 else None


def _labeled_count(text: str, label_pattern: str) -> int | None:
    patterns = (
        re.compile(rf"\b{_COUNT_TOKEN}\s+{label_pattern}\b", re.I),
        re.compile(rf"\b{label_pattern}\s*[:·]?\s*{_COUNT_TOKEN}\b", re.I),
    )
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return _compact_count(match.group(1))
    return None


def _load_pinterest_storage_state(path: Path | None = None) -> dict | None:
    """Load Pinterest cookies from the newest vault snapshot containing them.

    The vault may preserve ``latest.json`` when the current browser has fewer
    recognized auth cookies. Timestamped snapshots still contain that newer
    browser jar, so prefer the newest Pinterest-bearing one before falling
    back to the preserved global snapshot.
    """
    if path is None:
        root = Path(os.getenv("BROWSER_COOKIE_VAULT_DIR", "credentials/browser_cookies"))
        paths = sorted(root.glob("snapshot_*.json"), key=lambda item: item.name, reverse=True)
        paths.append(root / "latest.json")
    else:
        paths = [path]

    for snapshot_path in paths:
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        raw_cookies = payload if isinstance(payload, list) else payload.get("cookies", [])
        if not isinstance(raw_cookies, list):
            continue

        cookies = []
        for raw in raw_cookies:
            if not isinstance(raw, dict):
                continue
            domain = str(raw.get("domain") or "").lstrip(".").lower()
            name = str(raw.get("name") or "")
            value = str(raw.get("value") or "")
            if not name or not value or not (domain == "pinterest.com" or domain.endswith(".pinterest.com")):
                continue
            cookie = {
                "name": name,
                "value": value,
                "domain": str(raw.get("domain") or ".pinterest.com"),
                "path": str(raw.get("path") or "/"),
                "httpOnly": bool(raw.get("httpOnly", False)),
                "secure": bool(raw.get("secure", True)),
            }
            expires = raw.get("expires", raw.get("expirationDate"))
            if expires not in (None, "", -1, "-1"):
                try:
                    expiry = int(float(expires))
                except (TypeError, ValueError, OverflowError):
                    expiry = 0
                if expiry > 0:
                    cookie["expires"] = expiry
            same_site = str(raw.get("sameSite") or "").lower()
            if same_site in {"strict", "lax", "none"}:
                cookie["sameSite"] = same_site.title()
            cookies.append(cookie)
        if cookies:
            return {"cookies": cookies, "origins": []}
    return None


def _pinterest_page_kind(url: str) -> tuple[str, str | None, str | None] | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if parsed.scheme != "https" or host != "pinterest.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if (
        len(parts) == 2
        and parts[0].lower() == "pin"
        and re.fullmatch(r"[0-9]{1,32}", parts[1])
    ):
        return "pin", parts[1], None
    if not 1 <= len(parts) <= 2 or any(part.lower() in _BOARD_EXCLUDED for part in parts):
        return None
    username = parts[0]
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", username):
        return None
    if len(parts) == 1:
        return "profile", username, None
    board_slug = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,150}", board_slug):
        return None
    return "board", username, board_slug


def _explicitly_verified_capture(value: Any) -> bool:
    """Return true only for an explicit badge flag from the visible pin card."""
    if value is True or (isinstance(value, int) and not isinstance(value, bool) and value == 1):
        return True
    return isinstance(value, str) and value.strip().lower() in {"true", "1", "yes"}


def _pinterest_pin_author_is_verified(data: dict[str, Any], pin_id: str) -> bool:
    candidates = [data.get("pin_detail"), *(data.get("pin_cards") or [])]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        href = urlparse(str(candidate.get("href") or "")).path
        href_match = _PIN_PATH_RE.match(href)
        candidate_id = str(
            candidate.get("pin_id") or candidate.get("id")
            or (href_match.group(1) if href_match else "")
        )
        if (
            candidate_id == pin_id
            and _explicitly_verified_capture(candidate.get("author_is_verified"))
        ):
            return True
    return False


def _contains_private_marker(data: dict[str, Any]) -> bool:
    return bool(_PRIVATE_MARKER_RE.search(
        f"{data.get('body_text') or ''} {data.get('serialized_state') or ''}"
    ))


def _same_pinterest_page_kind(url: str, target_kind: tuple[str, str | None, str | None]) -> bool:
    if target_kind[0] == "profile":
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        parts = [part for part in parsed.path.split("/") if part]
        if (
            parsed.scheme != "https"
            or host != "pinterest.com"
            or not parts
            or parts[0].lower() != str(target_kind[1] or "").lower()
            or len(parts) > 2
        ):
            return False
        if len(parts) == 1 or parts[1].lower() in _PROFILE_TAB_PATHS:
            return True
        actual_kind = _pinterest_page_kind(url)
        return (
            actual_kind is not None
            and actual_kind[0] == "board"
            and (actual_kind[1] or "").lower() == str(target_kind[1] or "").lower()
            and (actual_kind[2] or "").lower() == parts[1].lower()
        )
    actual_kind = _pinterest_page_kind(url)
    return (
        actual_kind is not None
        and tuple((part or "").lower() for part in actual_kind)
        == tuple((part or "").lower() for part in target_kind)
    )


def _pinterest_following_handles(
    page_url: str,
    owner_username: str,
    profile_links: list[dict[str, Any]],
    *,
    following_view_confirmed: bool = False,
) -> list[str]:
    """Extract handles only from the authenticated owner's confirmed Following view."""
    parsed_page = urlparse(page_url)
    page_host = (parsed_page.hostname or "").lower().removeprefix("www.")
    page_parts = [part for part in parsed_page.path.split("/") if part]
    owner = str(owner_username or "").strip().lstrip("@").lower()
    owner_profile_page = len(page_parts) == 1 and page_parts[0].lower() == owner
    following_tab_page = (
        len(page_parts) == 2
        and page_parts[0].lower() == owner
        and page_parts[1].lower() == "_following"
    )
    if (
        parsed_page.scheme != "https"
        or page_host != "pinterest.com"
        or not (following_tab_page or (owner_profile_page and following_view_confirmed))
        or not re.fullmatch(r"[a-z0-9._-]{1,100}", owner)
    ):
        return []

    handles: list[str] = []
    seen: set[str] = set()
    for link in profile_links[:1000]:
        if not isinstance(link, dict) or link.get("is_following") is not True:
            continue
        if any(
            _explicitly_verified_capture(link.get(key))
            for key in ("profile_is_verified", "is_verified", "verified")
        ):
            continue
        parsed_link = urlparse(str(link.get("href") or ""))
        link_host = (parsed_link.hostname or "").lower().removeprefix("www.")
        parts = [part for part in parsed_link.path.split("/") if part]
        if (
            parsed_link.scheme != "https"
            or link_host != "pinterest.com"
            or len(parts) != 1
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", parts[0])
            or parts[0].lower() == owner
            or parts[0].lower() in _BOARD_EXCLUDED
        ):
            continue
        normalized = parts[0].lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        handles.append(parts[0])
    return handles


def _external_pin_destination(value: Any) -> str | None:
    """Keep only explicit HTTPS destinations linked from the same pin card."""
    try:
        parsed = urlparse(str(value or "").strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme.lower() != "https"
            or not host
            or host == "pinterest.com"
            or host.endswith(".pinterest.com")
            or host == "pin.it"
            or parsed.username
            or parsed.password
        ):
            return None
        destination = parsed.geturl()
        return destination if len(destination) <= 2000 else None
    except (TypeError, ValueError):
        return None


def _pin_membership_matches(
    url: str,
    page_kind: str,
    owner_username: str,
    board_slug: str | None,
) -> bool:
    membership = _pinterest_page_kind(url)
    if membership is None:
        return False
    membership_kind, membership_owner, membership_board = membership
    if (membership_owner or "").lower() != owner_username.lower():
        return False
    if page_kind == "profile":
        return membership_kind in {"profile", "board"}
    return (
        page_kind == "board"
        and membership_kind == "board"
        and (membership_board or "").lower() == (board_slug or "").lower()
    )


def _verified_board_link_conflict(column: str) -> str:
    if column not in {"board_id", "board_url"}:
        raise ValueError("unsupported_board_link_column")
    return f"""CASE
        WHEN EXCLUDED.metadata->>'owner_verified'='true' AND EXCLUDED.{column} IS NOT NULL
            THEN EXCLUDED.{column}
        WHEN EXCLUDED.metadata->>'owner_verified'='true'
            AND pinterest_pins.metadata->>'owner_verified'='true'
            AND pinterest_pins.username IS NOT DISTINCT FROM EXCLUDED.username
            THEN pinterest_pins.{column}
        WHEN EXCLUDED.metadata->>'owner_verified'='true' THEN NULL
        WHEN pinterest_pins.metadata->>'owner_verified'='true' THEN pinterest_pins.{column}
        ELSE NULL END"""


def _checked_pinterest_redirect(
    current_url: str,
    location: str,
    target_kind: tuple[str, str | None, str | None],
) -> str:
    next_url = httpx.URL(current_url).join(location)
    if not _same_pinterest_page_kind(str(next_url), target_kind):
        raise RuntimeError("public_redirect_rejected")
    return str(next_url)


async def _fetch_pinterest_html(
    target_url: str,
    target_kind: tuple[str, str | None, str | None],
    storage_state: dict | None,
) -> tuple[str, str]:
    cookies = httpx.Cookies()
    for cookie in (storage_state or {}).get("cookies", []):
        expires = cookie.get("expires")
        if expires and float(expires) <= time.time():
            continue
        cookies.set(
            cookie["name"], cookie["value"],
            domain=cookie.get("domain"), path=cookie.get("path") or "/",
        )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    async with httpx.AsyncClient(
        follow_redirects=False, cookies=cookies, timeout=30.0,
    ) as client:
        current_url = target_url
        for redirect_count in range(_MAX_PINTEREST_REDIRECTS + 1):
            async with client.stream("GET", current_url, headers=headers) as response:
                if response.status_code in _PINTEREST_REDIRECT_STATUSES:
                    if redirect_count >= _MAX_PINTEREST_REDIRECTS:
                        raise RuntimeError("public_redirect_limit")
                    location = response.headers.get("location")
                    if not location:
                        raise RuntimeError("public_redirect_missing_location")
                    current_url = _checked_pinterest_redirect(
                        current_url, location, target_kind,
                    )
                    continue
                if response.status_code >= 400:
                    raise _PinterestPublicHttpError(
                        response.status_code,
                        _retry_after_seconds(response.headers.get("Retry-After")),
                    )
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > _MAX_PINTEREST_PAGE_BYTES:
                        raise RuntimeError("public_page_too_large")
                encoding = response.encoding or "utf-8"
                return str(response.url), bytes(chunks).decode(encoding, "replace")
    raise RuntimeError("public_redirect_limit")


class PinterestCollector(ProfileOnlyCollector):
    SOURCE_NAME = "pinterest"
    PROFILE_TABLE = "pinterest_profiles"

    async def _read_own_following_roster(
        self,
        storage_state: dict[str, Any],
    ) -> tuple[str, list[str], int, bool]:
        'Read followed accounts from the authenticated profile Following dialog.'
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        browser = None
        context = None
        rate_limit_responses: list[tuple[int, int | None]] = []
        try:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-zygote",
                ],
            )
            context = await browser.new_context(
                locale="en-US", storage_state=storage_state,
                service_workers="block",
            )
            page = await context.new_page()

            def note_rate_limit(response: Any) -> None:
                parsed = urlparse(response.url)
                host = (parsed.hostname or "").lower().removeprefix("www.")
                if host != "pinterest.com" or response.status not in {403, 429}:
                    return
                rate_limit_responses.append((
                    int(response.status),
                    _retry_after_seconds(response.headers.get("retry-after")),
                ))

            page.on("response", note_rate_limit)
            home_response = await page.goto(
                "https://www.pinterest.com/",
                wait_until="domcontentloaded",
                timeout=20_000,
            )
            if home_response is None:
                raise RuntimeError("following_owner_unconfirmed")
            if home_response.status >= 400:
                raise _PinterestPublicHttpError(
                    home_response.status,
                    _retry_after_seconds(home_response.headers.get("retry-after")),
                )
            own_profile_links = await page.evaluate(
                r'''() => [...document.querySelectorAll('a[href]')].flatMap((anchor) => {
                  const url = new URL(anchor.href, location.href);
                  const parts = url.pathname.split('/').filter(Boolean);
                  const labels = [anchor.getAttribute('aria-label'), anchor.title,
                    ...Array.from(anchor.querySelectorAll('img')).map((image) => image.alt)]
                    .filter(Boolean);
                  return url.protocol === 'https:'
                    && url.hostname.replace(/^www\./, '') === 'pinterest.com'
                    && parts.length === 1
                    && labels.some((label) => /^your profile$/i.test(label.trim()))
                    ? [parts[0]] : [];
                })'''
            )
            owner_handles = {
                str(handle).strip().lstrip("@").lower(): str(handle).strip().lstrip("@")
                for handle in own_profile_links
                if re.fullmatch(
                    r"[A-Za-z0-9._-]{1,100}",
                    str(handle).strip().lstrip("@"),
                )
            }
            if len(owner_handles) != 1:
                raise RuntimeError("following_owner_unconfirmed")
            owner = next(iter(owner_handles.values()))

            profile_response = await page.goto(
                f"https://www.pinterest.com/{owner}/",
                wait_until="domcontentloaded",
                timeout=20_000,
            )
            if profile_response is None:
                raise RuntimeError("following_page_unavailable")
            if profile_response.status >= 400:
                raise _PinterestPublicHttpError(
                    profile_response.status,
                    _retry_after_seconds(profile_response.headers.get("retry-after")),
                )
            await page.wait_for_timeout(3_000)
            profile_state = await page.evaluate(
                r'''(expectedOwner) => {
                  const text = document.body?.innerText || '';
                  const path = location.pathname.split('/').filter(Boolean);
                  const control = document.querySelector('[data-test-id="profile-following-count"]');
                  const countMatch = (control?.innerText || '').match(/[0-9][0-9,.]*\s*[KMB]?/i);
                  return {
                    pageMatches: location.protocol === 'https:'
                      && location.hostname.replace(/^www\./, '') === 'pinterest.com'
                      && path.length === 1 && path[0].toLowerCase() === expectedOwner.toLowerCase(),
                    loginPrompt: /log in|sign up/i.test(text),
                    controlVisible: !!control && !!control.getClientRects().length,
                    followingCount: countMatch ? countMatch[0].replace(/\s+/g, '') : null,
                  };
                }''',
                owner,
            )
            if rate_limit_responses:
                status, retry_after = rate_limit_responses[0]
                raise _PinterestPublicHttpError(status, retry_after)
            if not profile_state.get("pageMatches"):
                raise RuntimeError("following_page_unavailable")
            if profile_state.get("loginPrompt"):
                raise RuntimeError("following_auth_required")
            if not profile_state.get("controlVisible"):
                raise RuntimeError("following_roster_unconfirmed")
            expected_count = _compact_count(profile_state.get("followingCount"))

            following_control = page.locator('[data-test-id="profile-following-count"]').first
            await following_control.click(timeout=5_000)
            try:
                await page.wait_for_selector('[role="dialog"]', state="visible", timeout=10_000)
            except Exception as exc:
                raise RuntimeError("following_roster_unconfirmed") from exc
            if expected_count and expected_count <= _PINTEREST_FOLLOWING_HANDLE_CAP:
                try:
                    await page.wait_for_function(
                        r'''(expected) => {
                          const dialog = document.querySelector('[role="dialog"]');
                          return !!dialog
                            && dialog.querySelectorAll('[data-test-id="profile-following"]').length >= expected;
                        }''',
                        arg=expected_count,
                        timeout=10_000,
                    )
                except Exception:
                    pass

            handles: dict[str, str] = {}
            verified_handles: set[str] = set()
            last_height = -1
            last_count = -1
            stable_rounds = 0
            complete = False
            for _ in range(25):
                dialog_state = await page.evaluate(
                    r'''() => {
                      const dialog = [...document.querySelectorAll('[role="dialog"]')]
                        .find((candidate) => candidate.getClientRects().length);
                      if (!dialog) return {visible: false, links: [], rowCount: 0, panelHeight: 0,
                        followingText: false, emptyRoster: false};
                      const hasVerifiedBadge = (node) => {
                        if (!node) return false;
                        const candidates = [node, ...Array.from(node.querySelectorAll('[aria-label],[title]'))];
                        return candidates.some((element) => [
                          element.getAttribute('aria-label') || '',
                          element.getAttribute('title') || '',
                        ].some((label) => /\bverified\b/i.test(label)
                          && !/\bnot\s+verified\b/i.test(label)));
                      };
                      const rows = Array.from(dialog.querySelectorAll('[data-test-id="profile-following"]'));
                      const links = rows.flatMap((row) => {
                        const isFollowing = Array.from(row.querySelectorAll('button,[role="button"]')).some((control) => {
                          const label = `${control.innerText || ''} ${control.getAttribute('aria-label') || ''} ${control.title || ''}`;
                          return /\b(unfollow|following)\b/i.test(label);
                        });
                        if (!isFollowing) return [];
                        return Array.from(row.querySelectorAll('a[href]'))
                          .filter((anchor) => anchor.getClientRects().length)
                          .map((anchor) => ({href: anchor.href, is_following: true,
                            profile_is_verified: hasVerifiedBadge(row)}));
                      }).slice(0, 1000);
                      const panel = dialog.querySelector('[data-test-id="modal-with-tabs-panel"]') || dialog;
                      const text = dialog.innerText || '';
                      return {visible: true, links, rowCount: rows.length,
                        panelHeight: panel.scrollHeight || 0,
                        followingText: /following/i.test(text),
                        emptyRoster: /no results|not following anyone|following no one/i.test(text)};
                    }'''
                )
                if not dialog_state.get("visible"):
                    raise RuntimeError("following_roster_unconfirmed")
                if not dialog_state.get("followingText") and not (
                    expected_count == 0 and dialog_state.get("emptyRoster")
                ):
                    raise RuntimeError("following_roster_unconfirmed")

                link_rows = dialog_state.get("links") or []
                for link in link_rows:
                    if not isinstance(link, dict) or not _explicitly_verified_capture(
                        link.get("profile_is_verified")
                    ):
                        continue
                    parsed_link = urlparse(str(link.get("href") or ""))
                    link_host = (parsed_link.hostname or "").lower().removeprefix("www.")
                    parts = [part for part in parsed_link.path.split("/") if part]
                    if (
                        link.get("is_following") is True
                        and parsed_link.scheme == "https"
                        and link_host == "pinterest.com"
                        and len(parts) == 1
                        and parts[0].lower() != owner.lower()
                        and re.fullmatch(r"[A-Za-z0-9._-]{1,100}", parts[0])
                    ):
                        verified_handles.add(parts[0].lower())
                        handles.pop(parts[0].lower(), None)
                candidate_handles = _pinterest_following_handles(
                    page.url, owner, link_rows, following_view_confirmed=True,
                )
                for handle in candidate_handles:
                    if len(handles) + len(verified_handles) >= _PINTEREST_FOLLOWING_HANDLE_CAP:
                        break
                    if handle.lower() not in verified_handles:
                        handles.setdefault(handle.lower(), handle)

                observed_count = len(handles) + len(verified_handles)
                panel_height = int(dialog_state.get("panelHeight") or 0)
                if expected_count is not None and observed_count == expected_count:
                    complete = True
                    break
                if expected_count is not None and observed_count > expected_count:
                    break
                if observed_count >= _PINTEREST_FOLLOWING_HANDLE_CAP:
                    break
                if observed_count == last_count and panel_height == last_height:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                if stable_rounds >= 2:
                    complete = expected_count is not None and observed_count == expected_count
                    break
                last_count = observed_count
                last_height = panel_height
                await page.evaluate(
                    r'''() => {
                      const dialog = document.querySelector('[role="dialog"]');
                      const panel = dialog?.querySelector('[data-test-id="modal-with-tabs-panel"]') || dialog;
                      if (panel) panel.scrollTop = panel.scrollHeight;
                    }'''
                )
                await page.wait_for_timeout(1_000)
                if rate_limit_responses:
                    status, retry_after = rate_limit_responses[0]
                    raise _PinterestPublicHttpError(status, retry_after)

            if not (handles or verified_handles or expected_count == 0):
                raise RuntimeError("following_roster_unconfirmed")
            return owner, list(handles.values()), len(verified_handles), complete
        finally:
            try:
                if context is not None:
                    await context.close()
            finally:
                try:
                    if browser is not None:
                        await browser.close()
                finally:
                    await playwright.stop()

    async def _refresh_own_following_edges(self) -> int:
        """Refresh Pinterest-owned follow edges before seeding native targets."""
        if self.pool is None:
            return 0
        now = time.monotonic()
        if now < getattr(self, "_pinterest_following_next_refresh_at", 0.0):
            return 0
        self._pinterest_following_next_refresh_at = (
            now + _PINTEREST_FOLLOWING_REFRESH_SECONDS
        )
        storage_state = _load_pinterest_storage_state()
        if not storage_state:
            self._pinterest_following_next_refresh_at = (
                now + _PINTEREST_FOLLOWING_FAILURE_RETRY_SECONDS
            )
            return 0
        try:
            owner, handles, verified_count, complete = await self._read_own_following_roster(
                storage_state,
            )
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    if complete:
                        await conn.execute(
                            """
                            DELETE FROM follow_edges
                             WHERE platform='pinterest' AND owner_account=$1
                               AND direction='following'
                            """,
                            owner,
                        )
                    if handles:
                        await conn.executemany(
                            """
                            INSERT INTO follow_edges
                                (platform, owner_account, target_uid, direction,
                                 target_username, first_seen, last_seen)
                            VALUES ('pinterest', $1, $2, 'following', $3, now(), now())
                            ON CONFLICT (platform, owner_account, target_uid, direction)
                            DO UPDATE SET last_seen=now(), target_username=EXCLUDED.target_username
                            """,
                            [(owner, handle.lower(), handle) for handle in handles],
                        )
        except Exception as exc:
            retry_seconds = _PINTEREST_FOLLOWING_FAILURE_RETRY_SECONDS
            if isinstance(exc, _PinterestPublicHttpError):
                if exc.status_code == 429:
                    retry_seconds = max(retry_seconds, exc.retry_after_seconds or 0)
                elif exc.status_code == 403:
                    retry_seconds = _PINTEREST_FOLLOWING_REFRESH_SECONDS
            self._pinterest_following_next_refresh_at = time.monotonic() + retry_seconds
            logger.warning(
                "Pinterest own-following refresh deferred: %s",
                _pinterest_native_error_code(exc),
            )
            return 0
        logger.info(
            "Pinterest own-following graph refreshed: handles=%d verified_skipped=%d complete=%s",
            len(handles), verified_count, complete,
        )
        return len(handles)

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

        expected_kind = _pinterest_page_kind(url)
        if expected_kind is None or not _same_pinterest_page_kind(
            str(r.url), expected_kind,
        ):
            return ProfileResult(
                exists=False, username=username, fields={},
                error="redirect:unexpected_target",
            )

        html = r.text
        if self._page_has_explicit_verified_badge(html):
            return ProfileResult(
                exists=True, username=username, fields={}, excluded_verified=True,
            )
        name_m = _FULL_NAME_RE.search(html)
        if not name_m:
            return ProfileResult(exists=False, username=username, fields={})

        bio_m = _BIO_RE.search(html)
        avatar_m = _AVATAR_RE.search(html)
        follower_m = _FOLLOWER_RE.search(html)
        pin_count_m = _PIN_COUNT_RE.search(html)
        board_count_m = _BOARD_COUNT_RE.search(html)
        following_count_m = _FOLLOWING_COUNT_RE.search(html)
        monthly_views_m = _MONTHLY_VIEWS_RE.search(html)
        visible_text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        following_count = (
            _compact_count(following_count_m.group(1)) if following_count_m
            else _labeled_count(visible_text, r"following")
        )
        monthly_views = (
            _compact_count(monthly_views_m.group(1)) if monthly_views_m
            else _labeled_count(visible_text, r"monthly\s+views?")
        )

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
                "following_count": following_count,
                "monthly_views": monthly_views,
            },
        )

    async def _scrape_public_page(
        self,
        target_url: str,
        *,
        target_source: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        target_kind = _pinterest_page_kind(target_url)
        if target_kind is None:
            raise ValueError("unsupported_public_page")

        from playwright.async_api import async_playwright

        storage_state = _load_pinterest_storage_state()
        playwright = await async_playwright().start()
        browser = None
        contexts = []
        try:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-zygote",
                ],
            )
            # Followed handles are the user's explicit Pinterest targets. Use
            # their signed-in session first so login/canonical routing cannot
            # block the preferred queue before the authenticated page is tried.
            if target_source == "followed_handle_candidate" and storage_state:
                try:
                    authenticated_context = await browser.new_context(
                        locale="en-US", storage_state=storage_state,
                        service_workers="block",
                    )
                except Exception:
                    logger.warning("Pinterest authenticated context unavailable; falling back to public page")
                else:
                    contexts.append(authenticated_context)
                    authenticated_data = await self._read_public_page(
                        authenticated_context, target_url, target_kind, None,
                        allow_authenticated_private=True,
                        direct_navigation=True,
                    )
                    if (
                        target_kind[0] == "pin"
                        and _pinterest_pin_author_is_verified(
                            authenticated_data, str(target_kind[1] or ""),
                        )
                    ):
                        raise RuntimeError("verified_account_excluded")
                    if authenticated_data.get("profile_is_verified") is True:
                        raise RuntimeError("verified_account_excluded")
                    if target_kind[0] == "profile" and not authenticated_data.get("profile_identity_verified"):
                        raise RuntimeError("public_profile_identity_unconfirmed")
                    boards, pins = self._parse_native_page(target_url, authenticated_data)
                    if not self._has_public_content(target_kind, boards, pins):
                        raise RuntimeError("empty_public_page")
                    return boards, pins

            anonymous_context = await browser.new_context(
                locale="en-US", service_workers="block",
            )
            contexts.append(anonymous_context)
            anonymous_url, anonymous_html = await _fetch_pinterest_html(
                target_url, target_kind, storage_state=None,
            )
            anonymous_data = await self._read_public_page(
                anonymous_context, anonymous_url, target_kind, anonymous_html,
            )
            if (
                target_kind[0] == "pin"
                and _pinterest_pin_author_is_verified(
                    anonymous_data, str(target_kind[1] or ""),
                )
            ):
                raise RuntimeError("verified_account_excluded")
            if anonymous_data.get("profile_is_verified") is True:
                raise RuntimeError("verified_account_excluded")
            if target_kind[0] == "profile" and not anonymous_data.get("profile_identity_verified"):
                raise RuntimeError("public_profile_identity_unconfirmed")
            public_boards, public_pins = self._parse_native_page(target_url, anonymous_data)
            if not self._has_public_content(target_kind, public_boards, public_pins):
                raise RuntimeError("public_visibility_unconfirmed")

            # Cookie-rendered content is eligible only when the same board/pin
            # identifiers were visible in the clean anonymous context.
            if not storage_state:
                return public_boards, public_pins
            try:
                authenticated_context = await browser.new_context(
                    locale="en-US", storage_state=storage_state,
                    service_workers="block",
                )
            except Exception:
                return public_boards, public_pins
            contexts.append(authenticated_context)
            authenticated_url, authenticated_html = await _fetch_pinterest_html(
                target_url, target_kind, storage_state,
            )
            authenticated_data = await self._read_public_page(
                authenticated_context, authenticated_url, target_kind, authenticated_html,
            )
            if (
                target_kind[0] == "pin"
                and _pinterest_pin_author_is_verified(
                    authenticated_data, str(target_kind[1] or ""),
                )
            ):
                raise RuntimeError("verified_account_excluded")
            if authenticated_data.get("profile_is_verified") is True:
                raise RuntimeError("verified_account_excluded")
            if target_kind[0] == "profile" and not authenticated_data.get("profile_identity_verified"):
                return public_boards, public_pins
            auth_boards, auth_pins = self._parse_native_page(target_url, authenticated_data)
            return self._intersect_public_content(
                public_boards, public_pins, auth_boards, auth_pins,
            )
        finally:
            try:
                for context in reversed(contexts):
                    await context.close()
            finally:
                try:
                    if browser is not None:
                        await browser.close()
                finally:
                    await playwright.stop()

    async def _read_public_page(
        self,
        context: Any,
        target_url: str,
        target_kind: tuple[str, str | None, str | None],
        page_html: str | None,
        *,
        allow_authenticated_private: bool = False,
        direct_navigation: bool = False,
    ) -> dict[str, Any]:
        page = await context.new_page()
        first_navigation = True
        rejected_navigation = False

        async def guard_navigation(route: Any) -> None:
            nonlocal first_navigation, rejected_navigation
            request = route.request
            if request.is_navigation_request() and request.frame == page.main_frame:
                if not _same_pinterest_page_kind(request.url, target_kind):
                    rejected_navigation = True
                    await route.abort()
                    return
                if direct_navigation:
                    first_navigation = False
                    await route.continue_()
                    return
                if first_navigation:
                    first_navigation = False
                    await route.fulfill(
                        status=200,
                        headers={"content-type": "text/html; charset=utf-8"},
                        body=page_html or "",
                    )
                    return
                await route.abort()
                return
            await route.continue_()

        await page.route("**/*", guard_navigation)
        try:
            response = await page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
        except Exception as exc:
            if rejected_navigation:
                raise RuntimeError("public_redirect_rejected") from exc
            raise
        if response is None or response.status >= 400:
            status = response.status if response is not None else 0
            raise RuntimeError(f"public_http_{status}")
        await page.wait_for_timeout(1500)
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(500)
        data = await page.evaluate(
            """({expectedUsername, expectedPageKind}) => {
              const text = (value) => String(value || '').replace(/\\s+/g, ' ').trim().slice(0, 500);
              const fullText = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
              const hasExplicitVerifiedBadge = (node) => {
                if (!node) return false;
                const candidates = [node, ...Array.from(node.querySelectorAll('[aria-label],[title]'))];
                return candidates.some((element) => [
                  element.getAttribute('aria-label') || '',
                  element.getAttribute('title') || '',
                ].some((label) => /\\bverified\\b/i.test(label) && !/\\bnot\\s+verified\\b/i.test(label)));
              };
              const heading = document.querySelector('h1');
              let boardHeader = heading;
              for (let depth = 0; boardHeader && depth < 3 && boardHeader.parentElement; depth++) {
                boardHeader = boardHeader.parentElement;
              }
              const anchors = Array.from(document.querySelectorAll('a[href]')).slice(0, 1200).map((a) => {
                const image = a.querySelector('img');
                const card = a.closest('[data-test-id="pin"], [data-test-id*="pinWrapper" i], [data-pin-id], [data-pinid]');
                const cardLinks = Array.from((card || a).querySelectorAll('a[href]'));
                const membershipLinks = Array.from(new Set([
                  a.href,
                  ...cardLinks.map((link) => link.href),
                ]));
                const destinationUrl = membershipLinks.find((href) => {
                  try {
                    const parsed = new URL(href, location.href);
                    return parsed.protocol === 'https:'
                      && !/(^|\\.)pinterest\\.com$/i.test(parsed.hostname)
                      && parsed.hostname.toLowerCase() !== 'pin.it';
                  } catch { return false; }
                }) || null;
                return {
                  href: a.href,
                  text: text(a.innerText || a.getAttribute('aria-label')),
                  image_alt: text(image?.alt),
                  image_url: image?.currentSrc || image?.src || null,
                  pin_id: card?.getAttribute('data-pin-id') || card?.getAttribute('data-pinid') || null,
                  membership_links: membershipLinks,
                  destination_url: destinationUrl,
                  card_text: fullText(card?.innerText || a.innerText || '').slice(0, 2000),
                  author_is_verified: hasExplicitVerifiedBadge(card || a),
                  control_labels: Array.from(card?.querySelectorAll('button,[role="button"]') || [])
                    .map((control) => control.getAttribute('aria-label') || control.getAttribute('title') || '')
                    .filter(Boolean).join(' ').slice(0, 1000),
                };
              });
              const pinCards = Array.from(document.querySelectorAll(
                '[data-test-id="pin"], [data-test-id*="pinWrapper" i], [data-pin-id], [data-pinid]'
              )).slice(0, 1200).map((card) => {
                const pinElement = card.matches('[data-pin-id], [data-pinid]')
                  ? card : card.querySelector('[data-pin-id], [data-pinid]');
                const pinId = pinElement?.getAttribute('data-pin-id')
                  || pinElement?.getAttribute('data-pinid') || null;
                const links = Array.from(card.querySelectorAll('a[href]')).map((link) => link.href);
                const destinationUrl = links.find((href) => {
                  try {
                    const parsed = new URL(href, location.href);
                    return parsed.protocol === 'https:'
                      && !/(^|\\.)pinterest\\.com$/i.test(parsed.hostname)
                      && parsed.hostname.toLowerCase() !== 'pin.it';
                  } catch { return false; }
                }) || null;
                const pinHref = links.find((href) => {
                  const parts = new URL(href).pathname.split('/').filter(Boolean);
                  return parts.length >= 2 && parts[0].toLowerCase() === 'pin'
                    && /^[0-9]+$/.test(parts[1]);
                }) || (pinId && /^[0-9]+$/.test(pinId)
                    ? `https://www.pinterest.com/pin/${pinId}/` : null);
                const image = card.querySelector('img');
                return {
                  href: pinHref,
                  pin_id: pinId,
                  destination_url: destinationUrl,
                  membership_links: Array.from(new Set([
                    ...links,
                    ...(expectedPageKind === 'board' ? [location.href] : []),
                  ])),
                  text: text(card.innerText || card.getAttribute('aria-label')),
                  image_alt: text(image?.alt),
                  image_url: image?.currentSrc || image?.src || null,
                  card_text: fullText(card.innerText || '').slice(0, 2000),
                  author_is_verified: hasExplicitVerifiedBadge(card),
                  control_labels: Array.from(card.querySelectorAll('button,[role="button"]'))
                    .map((control) => control.getAttribute('aria-label') || control.getAttribute('title') || '')
                    .filter(Boolean).join(' ').slice(0, 1000),
                };
              });
              const identityRegion = boardHeader;
              const identityText = fullText(identityRegion?.innerText);
              const expectedHandle = String(expectedUsername || '').toLowerCase();
              const currentPath = location.pathname.split('/').filter(Boolean);
              const identityByUrl = location.protocol === 'https:'
                && location.hostname.replace(/^www\\./, '') === 'pinterest.com'
                && currentPath.length >= 1 && currentPath.length <= 2
                && currentPath[0].toLowerCase() === expectedHandle;
              const identityLinks = Array.from(identityRegion?.querySelectorAll('a[href]') || [])
                .map((link) => link.href);
              const identityByLink = identityLinks.some((href) => {
                const parsed = new URL(href, location.href);
                const parts = parsed.pathname.split('/').filter(Boolean);
                return parsed.protocol === 'https:'
                  && parsed.hostname.replace(/^www\\./, '') === 'pinterest.com'
                  && parts.length === 1
                  && parts[0].toLowerCase() === expectedHandle;
              });
              const identityTokens = identityText.toLowerCase().match(/@[a-z0-9._-]+/g) || [];
              const profileIdentityVerified = identityByUrl || identityByLink
                || identityTokens.includes('@' + expectedHandle);
              const profileIsVerified = hasExplicitVerifiedBadge(identityRegion);
              const pinDetailCard = expectedPageKind === 'pin'
                ? pinCards.find((card) => String(card.pin_id || '') === String(expectedUsername || ''))
                : null;
              const pinDetailAuthor = document.querySelector(
                '[data-test-id*="creator" i], [data-test-id*="author" i]'
              );
              const pinDetail = expectedPageKind === 'pin' ? {
                id: String(expectedUsername || ''),
                pin_id: String(expectedUsername || ''),
                href: `https://www.pinterest.com/pin/${String(expectedUsername || '')}/`,
                title: text(document.querySelector('meta[property="og:title"]')?.content
                  || heading?.innerText || document.title),
                description: fullText(document.querySelector('meta[property="og:description"]')?.content
                  || document.querySelector('meta[name="description"]')?.content).slice(0, 4000),
                image_url: document.querySelector('meta[property="og:image"]')?.content
                  || pinDetailCard?.image_url || null,
                destination_url: pinDetailCard?.destination_url || null,
                card_text: pinDetailCard?.card_text || '',
                control_labels: pinDetailCard?.control_labels || '',
                author_is_verified: Boolean(pinDetailCard?.author_is_verified
                  || hasExplicitVerifiedBadge(pinDetailAuthor)),
              } : null;
              return {
                url: location.href,
                title: text(heading?.innerText || document.title),
                primary_heading: text(heading?.innerText),
                board_header_text: fullText(boardHeader?.innerText).slice(0, 2000),
                profile_identity_verified: profileIdentityVerified,
                profile_is_verified: profileIsVerified,
                description: text(document.querySelector('meta[property="og:description"]')?.content
                  || document.querySelector('meta[name="description"]')?.content),
                body_text: fullText(document.body?.innerText).slice(0, 12000),
                serialized_state: Array.from(document.scripts)
                  .map((script) => script.textContent || '').join(' ').slice(0, 2000000),
                anchors,
                pin_cards: pinCards,
                pin_detail: pinDetail,
              };
            }""",
            {"expectedUsername": target_kind[1], "expectedPageKind": target_kind[0]},
        )
        final_url = str(data.get("url") or "")
        if not _same_pinterest_page_kind(final_url, target_kind):
            parsed_final = urlparse(final_url)
            final_host = (parsed_final.hostname or "").lower().removeprefix("www.")
            final_parts = [part for part in parsed_final.path.split("/") if part]
            final_first = final_parts[0].lower() if final_parts else ""
            target_owner_matches = bool(
                final_first
                and final_first == str(target_kind[1] or "").lower()
            )
            body_text = str(data.get("body_text") or "")
            login_prompt_visible = bool(
                re.search(r"\b(log\s*in|sign\s*in|join pinterest)\b", body_text, re.IGNORECASE)
            )
            pin_card_count = len(data.get("pin_cards") or [])
            if final_host != "pinterest.com":
                redirect_class = "offsite"
            elif not final_parts:
                redirect_class = "pinterest_root"
            elif final_first in {"login", "session", "auth", "challenge", "_login", "_auth"}:
                redirect_class = "authentication_route"
            elif target_owner_matches:
                redirect_class = "same_owner_noncanonical_route"
            else:
                redirect_class = "other_pinterest_route"
            logger.warning(
                "Pinterest final page mismatch: class=%s host_is_pinterest=%s "
                "target_owner_matches=%s path_depth=%d login_prompt=%s pin_cards=%d",
                redirect_class,
                final_host == "pinterest.com",
                target_owner_matches,
                len(final_parts),
                login_prompt_visible,
                pin_card_count,
            )
            raise RuntimeError("public_target_redirected")
        if not allow_authenticated_private and _contains_private_marker(data):
            raise RuntimeError("private_content_rejected")
        return data

    @staticmethod
    def _has_public_content(
        target_kind: tuple[str, str | None, str | None],
        boards: list[dict[str, Any]],
        pins: list[dict[str, Any]],
    ) -> bool:
        if target_kind[0] == "board":
            # The requested URL alone creates a board-shaped parse result. Only
            # an anonymous heading and count from that heading's own DOM region
            # establish that the board itself is public.
            return any(
                board.get("public_header_visible") is True
                and board.get("pin_count") is not None
                for board in boards
            )
        if target_kind[0] == "pin":
            # The requested numeric ID and canonical URL alone are not
            # evidence that Pinterest exposed the pin. Login/removed pages
            # can still retain that route, so require a rendered pin field.
            return any(
                any(pin.get(field) is not None for field in (
                    "description", "image_url", "destination_url",
                    "repin_count", "comment_count",
                ))
                for pin in pins
            )
        return bool(boards or pins)

    @staticmethod
    def _intersect_public_content(
        public_boards: list[dict[str, Any]],
        public_pins: list[dict[str, Any]],
        authenticated_boards: list[dict[str, Any]],
        authenticated_pins: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        authenticated_board_ids = {str(board["id"]) for board in authenticated_boards}
        public_pin_ids = {str(pin["id"]) for pin in public_pins}
        boards = [board for board in public_boards if str(board["id"]) in authenticated_board_ids]
        authenticated_by_id = {
            str(pin["id"]): pin
            for pin in authenticated_pins
            if str(pin.get("id")) in public_pin_ids
        }
        pins = []
        enrichment_fields = (
            "title", "description", "image_url", "destination_url",
            "repin_count", "comment_count",
        )
        for public_pin in public_pins:
            authenticated_pin = authenticated_by_id.get(str(public_pin["id"]))
            if authenticated_pin is None:
                continue
            merged = dict(public_pin)
            for field in enrichment_fields:
                if merged.get(field) is None and authenticated_pin.get(field) is not None:
                    merged[field] = authenticated_pin[field]
            pins.append(merged)
        return boards or public_boards, pins or public_pins

    @staticmethod
    def _parse_native_page(target_url: str, data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        page_kind = _pinterest_page_kind(target_url)
        if page_kind is None:
            return [], []
        kind, username, board_slug = page_kind
        owner_username = str(username or "").lower()
        profile_page_url = str(data.get("url") or "")
        profile_page_url_verified = (
            kind == "profile"
            and _same_pinterest_page_kind(
                profile_page_url, ("profile", username, None),
            )
        )
        profile_identity_verified = data.get("profile_identity_verified") is True or (
            profile_page_url_verified
        )
        pin_detail = data.get("pin_detail") if kind == "pin" else None
        if not isinstance(pin_detail, dict):
            pin_detail = {}
        title = str(data.get("title") or "").strip()[:500] or None
        body_text = str(data.get("body_text") or "")[:12_000]
        board_header_text = str(data.get("board_header_text") or "")[:2_000]
        pin_count_source = board_header_text if kind == "board" else body_text
        pin_count_match = re.search(r"([\d,]+)\s+pins?\b", pin_count_source, re.IGNORECASE)
        pin_count = int(pin_count_match.group(1).replace(",", "")) if pin_count_match else None
        board_description = str(data.get("description") or "").strip()[:4000] or None
        board_follower_count = _labeled_count(board_header_text, r"followers?")
        primary_heading = str(data.get("primary_heading") or "").strip()[:500]
        boards: dict[str, dict[str, Any]] = {}
        if kind == "board" and board_slug:
            board_id = f"{username}/{board_slug}".lower()
            board_url = f"https://www.pinterest.com/{username}/{board_slug}/"
            boards[board_id] = {
                "id": board_id,
                "username": username,
                "name": title or board_slug.replace("_", " "),
                "url": board_url,
                "pin_count": pin_count,
                "description": board_description,
                "follower_count": board_follower_count,
                "public_header_visible": bool(
                    primary_heading
                    and primary_heading.lower() not in {"pinterest", "home", "log in", "sign up"}
                    and pin_count is not None
                ),
            }

        pins: dict[str, dict[str, Any]] = {}
        candidates = [
            *data.get("anchors", [])[:1200],
            *data.get("pin_cards", [])[:1200],
        ]
        if pin_detail:
            candidates.append(pin_detail)
        verified_pin_ids: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict) or not _explicitly_verified_capture(
                candidate.get("author_is_verified")
            ):
                continue
            href = str(candidate.get("href") or "")
            pin_match = _PIN_PATH_RE.match(urlparse(href).path)
            raw_id = str(candidate.get("pin_id") or "")
            pin_id = pin_match.group(1) if pin_match else raw_id if raw_id.isdigit() else ""
            if pin_id:
                verified_pin_ids.add(pin_id)
        for anchor in candidates:
            if not isinstance(anchor, dict):
                continue
            href = str(anchor.get("href") or "")
            parsed = urlparse(href)
            host = (parsed.hostname or "").lower().removeprefix("www.")
            if parsed.scheme != "https" or host != "pinterest.com":
                continue
            pin_match = _PIN_PATH_RE.match(parsed.path)
            raw_id = str(anchor.get("pin_id") or "")
            pin_id = pin_match.group(1) if pin_match else (raw_id if raw_id.isdigit() else "")
            if (
                not pin_id
                or (kind == "pin" and pin_id != str(username or ""))
                or pin_id in verified_pin_ids
                or _explicitly_verified_capture(anchor.get("author_is_verified"))
            ):
                continue
            page_identity_verified = (
                kind != "profile" or profile_identity_verified
            )
            membership_links = [
                link for link in anchor.get("membership_links", [])
                if isinstance(link, str)
            ]
            if kind == "profile" and profile_identity_verified and profile_page_url_verified:
                # The visible profile page itself is evidence that its pin cards
                # belong to this followed handle even when cards link only to pins.
                membership_links.append(profile_page_url)
            ownership_verified = page_identity_verified and any(
                _pin_membership_matches(link, kind, owner_username, board_slug)
                for link in membership_links
            )
            pin_url = f"https://www.pinterest.com/pin/{pin_id}/"
            card_text = " ".join((
                str(anchor.get("card_text") or ""),
                str(anchor.get("control_labels") or ""),
            ))[:3000]
            if kind == "pin":
                card_text = " ".join((
                    card_text,
                    str(pin_detail.get("card_text") or ""),
                    str(pin_detail.get("control_labels") or ""),
                ))[:3000]
            repin_count = _labeled_count(card_text, r"re-?pins?")
            comment_count = _labeled_count(card_text, r"comments?")
            parsed_pin = {
                "id": pin_id,
                "username": username if ownership_verified else None,
                "board_id": f"{username}/{board_slug}".lower() if ownership_verified and board_slug else None,
                "board_url": f"https://www.pinterest.com/{username}/{board_slug}/" if ownership_verified and board_slug else None,
                "owner_verified": ownership_verified,
                "title": str(
                    anchor.get("image_alt") or anchor.get("text")
                    or (pin_detail.get("title") if kind == "pin" else "") or ""
                ).strip()[:500] or None,
                "description": str(
                    (pin_detail.get("description") if kind == "pin" else None)
                    or anchor.get("description") or anchor.get("text") or ""
                ).strip()[:4000] or None,
                "image_url": str(
                    anchor.get("image_url")
                    or (pin_detail.get("image_url") if kind == "pin" else "") or ""
                )[:2000] or None,
                "repin_count": repin_count if repin_count is not None else pin_detail.get("repin_count"),
                "comment_count": comment_count if comment_count is not None else pin_detail.get("comment_count"),
                "destination_url": _external_pin_destination(
                    anchor.get("destination_url")
                    or (pin_detail.get("destination_url") if kind == "pin" else None)
                ),
                "url": pin_url,
            }
            existing = pins.get(pin_id)
            if existing:
                parsed_pin = {
                    key: value if value is not None else existing.get(key)
                    for key, value in parsed_pin.items()
                }
            pins[pin_id] = parsed_pin

        if kind == "profile":
            for anchor in data.get("anchors", [])[:1200]:
                if not isinstance(anchor, dict):
                    continue
                parsed = urlparse(str(anchor.get("href") or ""))
                host = (parsed.hostname or "").lower().removeprefix("www.")
                parts = [part for part in parsed.path.split("/") if part]
                if (
                    parsed.scheme != "https"
                    or host != "pinterest.com"
                    or len(parts) != 2
                    or parts[0].lower() != username.lower()
                    or parts[1].lower() in _BOARD_EXCLUDED
                    or not re.fullmatch(r"[A-Za-z0-9._-]{1,150}", parts[1])
                ):
                    continue
                board_id = f"{username}/{parts[1]}".lower()
                boards[board_id] = {
                    "id": board_id,
                    "username": username,
                    "name": str(anchor.get("text") or parts[1].replace("_", " ")).strip()[:500],
                    "url": f"https://www.pinterest.com/{username}/{parts[1]}/",
                    "pin_count": None,
                }
        return list(boards.values())[:100], list(pins.values())[:200]

    async def run_native_cycle(self) -> dict[str, Any]:
        summary: dict[str, Any] = {"processed": 0, "boards": 0, "pins": 0}
        if os.getenv("PINTEREST_NATIVE_ENABLED", "1").strip().lower() not in {"1", "true", "yes", "on"}:
            summary["skipped"] = "disabled"
            return summary
        if self.pool is None:
            summary["skipped"] = "no_pool"
            return summary

        summary["followed_handles"] = await self._refresh_own_following_edges()
        await seed_native_targets(self.pool, self.SOURCE_NAME)
        target = await claim_native_target(self.pool, self.SOURCE_NAME)
        if target is None:
            return summary
        summary["processed"] = 1
        try:
            boards, pins = await self._scrape_public_page(
                str(target["target_url"]),
                target_source=str(target.get("source") or ""),
            )
            if not boards and not pins:
                raise RuntimeError("empty_public_page")
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    for board in boards:
                        await conn.execute(
                            """
                            INSERT INTO pinterest_boards
                                (board_id,username,name,board_url,pin_count,description,follower_count,collected_at,metadata)
                            VALUES ($1,$2,$3,$4,$5,$6,$7,now(),$8::jsonb)
                            ON CONFLICT (board_id) DO UPDATE SET
                                username=COALESCE(EXCLUDED.username,pinterest_boards.username),
                                name=COALESCE(EXCLUDED.name,pinterest_boards.name),
                                board_url=EXCLUDED.board_url,
                                pin_count=COALESCE(EXCLUDED.pin_count,pinterest_boards.pin_count),
                                description=COALESCE(EXCLUDED.description,pinterest_boards.description),
                                follower_count=COALESCE(EXCLUDED.follower_count,pinterest_boards.follower_count),
                                collected_at=now(),metadata=pinterest_boards.metadata||EXCLUDED.metadata
                            """,
                            board["id"], board["username"], board["name"], board["url"],
                            board["pin_count"], board["description"], board["follower_count"],
                            json.dumps({"collector": "playwright"}),
                        )
                    for pin in pins:
                        await conn.execute(
                            f"""
                            INSERT INTO pinterest_pins
                                (pin_id,username,board_id,board_url,title,description,image_url,
                                 destination_url,pin_url,repin_count,comment_count,collected_at,metadata)
                            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,now(),$12::jsonb)
                            ON CONFLICT (pin_id) DO UPDATE SET
                                username=CASE
                                    WHEN EXCLUDED.metadata->>'owner_verified'='true' THEN EXCLUDED.username
                                    WHEN pinterest_pins.metadata->>'owner_verified'='true' THEN pinterest_pins.username
                                    ELSE NULL END,
                                board_id={_verified_board_link_conflict('board_id')},
                                board_url={_verified_board_link_conflict('board_url')},
                                title=COALESCE(EXCLUDED.title,pinterest_pins.title),
                                description=COALESCE(EXCLUDED.description,pinterest_pins.description),
                                image_url=COALESCE(EXCLUDED.image_url,pinterest_pins.image_url),
                                destination_url=COALESCE(EXCLUDED.destination_url,pinterest_pins.destination_url),
                                repin_count=COALESCE(EXCLUDED.repin_count,pinterest_pins.repin_count),
                                comment_count=COALESCE(EXCLUDED.comment_count,pinterest_pins.comment_count),
                                pin_url=EXCLUDED.pin_url,collected_at=now(),
                                metadata=COALESCE(pinterest_pins.metadata,'{{}}'::jsonb)||EXCLUDED.metadata||
                                    jsonb_build_object('owner_verified',
                                        (pinterest_pins.metadata->>'owner_verified'='true') OR
                                        (EXCLUDED.metadata->>'owner_verified'='true'))
                            """,
                            pin["id"], pin["username"], pin["board_id"], pin["board_url"],
                            pin["title"], pin["description"], pin["image_url"],
                            pin["destination_url"], pin["url"],
                            pin["repin_count"], pin["comment_count"],
                            json.dumps({
                                "collector": "playwright",
                                "owner_verified": pin["owner_verified"],
                            }),
                        )
            await finish_native_target(
                self.pool, self.SOURCE_NAME, int(target["id"]),
                claim_token=target["claim_token"], success=True,
            )
            summary["boards"] = len(boards)
            summary["pins"] = len(pins)
        except Exception as exc:
            if str(exc) == "verified_account_excluded":
                await finish_native_target(
                    self.pool, self.SOURCE_NAME, int(target["id"]),
                    claim_token=target["claim_token"], success=True,
                )
                summary["excluded_verified"] = 1
                return summary
            attempts = int(target.get("attempts") or 1)
            exponential_retry = min(24 * 60 * 60, 15 * 60 * (2 ** min(max(attempts - 1, 0), 6)))
            retry_after_seconds = getattr(exc, "retry_after_seconds", None)
            if isinstance(exc, _PinterestPublicHttpError) and exc.status_code == 429:
                retry_after_seconds = max(exponential_retry, int(retry_after_seconds or 0))
            elif isinstance(exc, _PinterestPublicHttpError) and exc.status_code == 403:
                retry_after_seconds = max(24 * 60 * 60, int(retry_after_seconds or 0))
            elif str(exc) == "empty_public_page":
                retry_after_seconds = 24 * 60 * 60
            elif retry_after_seconds is None:
                retry_after_seconds = max(exponential_retry, 2 * 60 * 60)
            error_code = _pinterest_native_error_code(exc)
            await finish_native_target(
                self.pool, self.SOURCE_NAME, int(target["id"]),
                claim_token=target["claim_token"], success=False,
                error=error_code,
                retry_after_seconds=retry_after_seconds,
            )
            logger.warning("Pinterest native scrape failed: %s", error_code)
            summary["error"] = error_code
        return summary

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
