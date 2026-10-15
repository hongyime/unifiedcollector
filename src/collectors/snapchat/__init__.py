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

import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup
import httpx
import requests

from src.core.profile_only_collector import ProfileOnlyCollector, ProfileResult
from src.collectors.native_platform_scrape import (
    claim_native_target,
    finish_native_target,
    seed_native_targets,
    snapchat_storage_target as _snapchat_storage_target,
)

logger = logging.getLogger(__name__)

_BITMOJI_URL_RE = re.compile(r'"bitmoji"\s*:\s*{[^}]*"avatar"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_BITMOJI_VER_RE = re.compile(r"_(\d+)\.webp")
_DISPLAY_NAME_RE = re.compile(r'"displayName"\s*:\s*"((?:[^"\\]|\\.)*?)"')
_SNAP_SCORE_RE = re.compile(r'"snapScore"\s*:\s*(\d+)')
_NEXT_DATA_RE = re.compile(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.I | re.S)
_PUBLIC_PATH_RE = re.compile(r"^/(?:discover|spotlight|@[A-Za-z0-9._-]{1,100}|add/[A-Za-z0-9._-]{1,100}|profile/[A-Za-z0-9._-]{1,100})/?$")
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_PUBLIC_REDIRECTS = 5
_VERIFIED_FLAGS = ("verified", "isVerified", "is_verified", "is_blue_verified")


def _explicitly_verified(value: Any) -> bool:
    """Recognize explicit verification values from public Snapchat payloads."""
    if not isinstance(value, dict):
        return False
    return any(
        (flag is True)
        or (isinstance(flag, int) and not isinstance(flag, bool) and flag == 1)
        or (isinstance(flag, str) and flag.strip().lower() in {"true", "1", "yes"})
        for flag in (value.get(key) for key in _VERIFIED_FLAGS)
    )


def _decode_json_str(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except Exception:
        return s


def _allowed_public_page(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme == "https"
        and host in {"snapchat.com", "www.snapchat.com", "story.snapchat.com"}
        and _PUBLIC_PATH_RE.fullmatch(parsed.path) is not None
    )


def _retry_after_seconds(value: Any, *, now: datetime | None = None) -> int | None:
    """Parse either Retry-After delta-seconds or its HTTP-date form."""
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
        seconds = (retry_at - (now or datetime.now(timezone.utc))).total_seconds()
    if seconds <= 0:
        return 1
    return min(7 * 24 * 60 * 60, max(1, int(seconds)))


class _PublicPageHttpError(RuntimeError):
    def __init__(self, status_code: int, retry_after_seconds: int | None = None):
        self.status_code = int(status_code)
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"public_http_{self.status_code}")


def _scalar(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value).strip()
    return None


def _to_timestamp(value: Any, *, milliseconds: bool = False) -> str | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
        if milliseconds or number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _public_media_url(value: Any) -> str | None:
    if isinstance(value, str):
        parsed = urlparse(value)
        if parsed.scheme == "https" and parsed.hostname:
            return value[:4000]
    if isinstance(value, dict):
        for child in value.values():
            found = _public_media_url(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _public_media_url(child)
            if found:
                return found
    return None


def _public_profile_handle(value: Any, fallback: str | None) -> str | None:
    if isinstance(value, dict) and "__ref" in value:
        return fallback
    if isinstance(value, dict):
        for key in ("username", "handle", "publicUsername", "displayName"):
            candidate = _scalar(value.get(key))
            if candidate:
                return candidate.lstrip("@")[:100]
    return fallback[:100] if fallback else None


def _make_public_snap_item(
    identity: str,
    story: dict[str, Any],
    metadata: dict[str, Any],
    *,
    surface: str,
    fallback_username: str | None,
    content_type: str,
    fallback_url: str,
) -> dict[str, Any] | None:
    provider_id = (
        _scalar(story.get("storyId"))
        or _scalar(story.get("id"))
        or _scalar(story.get("storyTapId"))
        or identity
    )
    if not provider_id:
        return None
    video = metadata.get("videoMetadata") if isinstance(metadata.get("videoMetadata"), dict) else {}
    snaps = story.get("snapList") or story.get("snaps") or []
    media_url = _public_media_url(video.get("contentUrl"))
    if not media_url:
        media_url = _public_media_url(snaps)
    thumbnail = _public_media_url(story.get("thumbnailUrl")) or _public_media_url(video.get("thumbnailUrl"))
    title = (
        _scalar(story.get("storyTitle"))
        or _scalar(story.get("title"))
        or _scalar(video.get("name"))
        or _scalar(metadata.get("llmTitle"))
        or _scalar(story.get("storySubtitle"))
    )
    creator = video.get("creator") or story.get("creator")
    if _explicitly_verified(creator):
        return None
    username = _public_profile_handle(creator, fallback_username)
    posted = (
        _to_timestamp(video.get("uploadDateMs"), milliseconds=True)
        or _to_timestamp(story.get("publishedTimeInSec"))
        or _to_timestamp(story.get("timestampInSec"))
    )
    if content_type == "public_story" and posted:
        parsed_posted = datetime.fromisoformat(posted)
        expires = (parsed_posted + timedelta(hours=24)).isoformat()
    else:
        expires = _to_timestamp(story.get("expiresAt"))

    canonical_suffix = _scalar(story.get("canonicalUrlSuffix"))
    if surface == "spotlight" and canonical_suffix:
        story_url = "https://www.snapchat.com/spotlight/" + quote(canonical_suffix.lstrip("/"), safe="/@-._~")
    else:
        story_url = fallback_url
    stable_id = hashlib.sha256(f"{surface}:{provider_id}".encode("utf-8")).hexdigest()[:40]
    item_metadata: dict[str, Any] = {
        "content_type": content_type,
        "surface": surface,
        "provider_id": provider_id[:200],
        "snap_count": len(snaps) if isinstance(snaps, list) else 0,
    }
    stats = metadata.get("engagementStats")
    if isinstance(stats, dict):
        for field in ("viewCount", "shareCount", "commentCount"):
            if isinstance(stats.get(field), (int, float)):
                item_metadata[field] = int(stats[field])
    return {
        "id": stable_id,
        "username": username,
        "title": title[:500] if title else None,
        "url": story_url,
        "media_url": media_url,
        "thumbnail_url": thumbnail,
        "posted_at": posted,
        "expires_at": expires,
        "metadata": item_metadata,
    }


def _extract_public_snap_items(html: str, target_url: str, username: str | None) -> list[dict[str, Any]]:
    tag = BeautifulSoup(html, "html.parser").find("script", id="__NEXT_DATA__")
    if tag is None:
        match = _NEXT_DATA_RE.search(html)
        if not match:
            return []
        raw_json = match.group(1)
    else:
        raw_json = tag.string or tag.get_text()
    try:
        next_data = json.loads(raw_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(next_data, dict):
        return []
    props = next_data.get("props")
    page = props.get("pageProps", {}) if isinstance(props, dict) else {}
    if not isinstance(page, dict):
        return []
    items: dict[str, dict[str, Any]] = {}

    # story.snapchat.com/@<username> carries public profiles, active stories,
    # curated highlights, and Spotlight clips in the same __NEXT_DATA__ blob.
    # Normalize every public Snap separately so multi-Snap stories are not
    # collapsed to the first media URL.
    user_profile = page.get("userProfile") if isinstance(page.get("userProfile"), dict) else {}
    profile_info = (
        user_profile.get("publicProfileInfo")
        if isinstance(user_profile.get("publicProfileInfo"), dict) else {}
    )
    if any(_explicitly_verified(obj) for obj in (user_profile, profile_info)):
        return []
    page_username = _public_profile_handle(profile_info, username)

    def add_snap_list(
        parent: Any,
        *,
        content_type: str,
        surface: str,
        parent_id: str | None = None,
        parent_title: str | None = None,
    ) -> None:
        if not isinstance(parent, dict):
            return
        snap_list = parent.get("snapList")
        if not isinstance(snap_list, list):
            return
        for index, snap in enumerate(snap_list[:25]):
            if not isinstance(snap, dict):
                continue
            snap_urls = snap.get("snapUrls") if isinstance(snap.get("snapUrls"), dict) else {}
            media_url = (
                _public_media_url(snap_urls.get("mediaUrl"))
                or _public_media_url(snap.get("contentUrl"))
            )
            if not media_url:
                continue
            provider_id = (
                _scalar(snap.get("snapId"))
                or _scalar(snap.get("id"))
                or media_url
            )
            timestamp_value = _scalar(snap.get("timestampInSec"))
            posted_at = _to_timestamp(timestamp_value)
            if content_type == "public_story" and posted_at:
                expires_at = (
                    datetime.fromisoformat(posted_at) + timedelta(hours=24)
                ).isoformat()
            else:
                expires_at = _to_timestamp(_scalar(snap.get("expiresAt")))
            title = (
                _scalar(snap.get("storyTitle"))
                or _scalar(snap.get("title"))
                or parent_title
            )
            thumb_urls = snap.get("snapUrls") if isinstance(snap.get("snapUrls"), dict) else {}
            thumbnail = (
                _public_media_url(thumb_urls.get("thumbnailUrl"))
                or _public_media_url(snap.get("thumbnailUrl"))
            )
            stable_id = hashlib.sha256(
                f"{surface}:{parent_id or ''}:{provider_id}".encode("utf-8")
            ).hexdigest()[:40]
            item = {
                "id": stable_id,
                "username": page_username,
                "title": title[:500] if title else None,
                "url": target_url,
                "media_url": media_url,
                "thumbnail_url": thumbnail,
                "posted_at": posted_at,
                "expires_at": expires_at,
                "metadata": {
                    "content_type": content_type,
                    "surface": surface,
                    "provider_id": str(provider_id)[:200],
                    "parent_id": str(parent_id)[:200] if parent_id else None,
                    "snap_media_type": _scalar(snap.get("snapMediaType")),
                    "snap_index": index,
                },
            }
            items[stable_id] = item

    story = page.get("story")
    add_snap_list(story, content_type="public_story", surface="profile_story")

    highlights = page.get("curatedHighlights", [])
    if isinstance(highlights, list):
        for highlight in highlights[:25]:
            if not isinstance(highlight, dict):
                continue
            add_snap_list(
                highlight,
                content_type="highlight",
                surface="profile_highlight",
                parent_id=_scalar(highlight.get("highlightId")),
                parent_title=_scalar(highlight.get("storyTitle")),
            )

    spotlights = page.get("spotlightHighlights", [])
    if isinstance(spotlights, list):
        for spotlight in spotlights[:25]:
            if not isinstance(spotlight, dict):
                continue
            add_snap_list(
                spotlight,
                content_type="spotlight",
                surface="profile_spotlight",
                parent_id=_scalar(spotlight.get("spotlightId") or spotlight.get("id")),
                parent_title=_scalar(spotlight.get("storyTitle") or spotlight.get("title")),
            )

    feed = page.get("spotlightFeed", {})
    spotlight = feed.get("spotlightStories", []) if isinstance(feed, dict) else []
    if isinstance(spotlight, list):
        for index, entry in enumerate(spotlight[:25]):
            if not isinstance(entry, dict):
                continue
            story = entry.get("story") if isinstance(entry.get("story"), dict) else {}
            metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
            item = _make_public_snap_item(
                str(index), story, metadata, surface="spotlight",
                fallback_username=username, content_type="spotlight", fallback_url=target_url,
            )
            if item:
                items[item["id"]] = item

    apollo = page.get("initialApollo", {})
    state = apollo.get("state", {}) if isinstance(apollo, dict) else {}
    if isinstance(state, dict):
        for entity_key, entity in list(state.items())[:2000]:
            if not isinstance(entity, dict) or entity.get("__typename") not in {"SnapProStory", "PremiumPublisherStory"}:
                continue
            raw_type = str(entity.get("type") or entity.get("storyType") or "").lower()
            if "highlight" in raw_type or entity.get("highlightId"):
                content_type = "highlight"
            elif "spotlight" in raw_type:
                content_type = "spotlight"
            else:
                content_type = "public_story"
            story = dict(entity)
            for creator_key in ("creator", "publisher", "profile"):
                creator = story.get(creator_key)
                if isinstance(creator, dict) and creator.get("__ref"):
                    story[creator_key] = state.get(str(creator["__ref"]), creator)
            snaps = story.get("snaps")
            if isinstance(snaps, list):
                story["snapList"] = [
                    state.get(str(ref.get("__ref")), ref) if isinstance(ref, dict) else ref
                    for ref in snaps[:25]
                ]
            item = _make_public_snap_item(
                str(entity_key), story, {}, surface="discover",
                fallback_username=username, content_type=content_type, fallback_url=target_url,
            )
            if item:
                items[item["id"]] = item
    # Keep the response bounded without letting a profile's active story list
    # starve its Highlights or Spotlight items. Preserve source order within
    # each content type and select one item per type per pass.
    by_content_type: dict[str, list[dict[str, Any]]] = {}
    for item in items.values():
        content_type = str(item.get("metadata", {}).get("content_type") or "unknown")
        by_content_type.setdefault(content_type, []).append(item)
    selected: list[dict[str, Any]] = []
    offsets = {content_type: 0 for content_type in by_content_type}
    while len(selected) < 25:
        added = False
        for content_type, candidates in by_content_type.items():
            offset = offsets[content_type]
            if offset >= len(candidates):
                continue
            selected.append(candidates[offset])
            offsets[content_type] = offset + 1
            added = True
            if len(selected) >= 25:
                break
        if not added:
            break
    return selected


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
        if self._page_has_explicit_verified_badge(html):
            return ProfileResult(
                exists=True, username=username, fields={}, excluded_verified=True,
            )
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

    @staticmethod
    def _fetch_public_page(target_url: str) -> str:
        if not _allowed_public_page(target_url):
            raise ValueError("unsupported_public_page")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            with requests.Session() as session:
                current_url = target_url
                for redirect_count in range(_MAX_PUBLIC_REDIRECTS + 1):
                    with session.get(
                        current_url,
                        headers=headers,
                        timeout=(5, 20),
                        stream=True,
                        allow_redirects=False,
                    ) as response:
                        if response.status_code in _REDIRECT_STATUSES:
                            if redirect_count >= _MAX_PUBLIC_REDIRECTS:
                                raise RuntimeError("public_redirect_limit")
                            location = response.headers.get("Location")
                            if not location:
                                raise RuntimeError("public_redirect_missing_location")
                            next_url = urljoin(current_url, location)
                            if not _allowed_public_page(next_url):
                                raise RuntimeError("public_redirect_rejected")
                            current_url = next_url
                            continue
                        if response.status_code >= 400:
                            raise _PublicPageHttpError(
                                response.status_code,
                                _retry_after_seconds(response.headers.get("Retry-After")),
                            )
                        if not _allowed_public_page(getattr(response, "url", None) or current_url):
                            raise RuntimeError("public_redirect_rejected")
                        chunks = bytearray()
                        for chunk in response.iter_content(chunk_size=64 * 1024):
                            if not chunk:
                                continue
                            chunks.extend(chunk)
                            if len(chunks) > 5 * 1024 * 1024:
                                raise RuntimeError("public_page_too_large")
                        return bytes(chunks).decode(response.encoding or "utf-8", "replace")
                raise RuntimeError("public_redirect_limit")
        except requests.RequestException as exc:
            raise RuntimeError("public_request_failed") from exc

    async def run_native_cycle(self) -> dict[str, Any]:
        summary: dict[str, Any] = {"processed": 0, "items": 0}
        if os.getenv("SNAPCHAT_NATIVE_ENABLED", "1").strip().lower() not in {"1", "true", "yes", "on"}:
            summary["skipped"] = "disabled"
            return summary
        if self.pool is None:
            summary["skipped"] = "no_pool"
            return summary

        await seed_native_targets(self.pool, self.SOURCE_NAME)
        target = await claim_native_target(self.pool, self.SOURCE_NAME)
        if target is None:
            return summary
        summary["processed"] = 1
        target_url = str(target["target_url"])
        username = str(target["username"]) if target.get("username") else None
        try:
            html = await asyncio.to_thread(self._fetch_public_page, target_url)
            items = _extract_public_snap_items(html, target_url, username)
            if not items:
                raise RuntimeError("empty_public_page")
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    for item in items:
                        content_type = str(item.get("metadata", {}).get("content_type") or "public_story")
                        table_name, id_column = _snapchat_storage_target(content_type)
                        await conn.execute(
                            f"""
                            INSERT INTO {table_name}
                                ({id_column},username,title,story_url,media_url,thumbnail_url,
                                 posted_at,expires_at,collected_at,metadata)
                            VALUES ($1,$2,$3,$4,$5,$6,$7::text::timestamptz,$8::text::timestamptz,now(),$9::jsonb)
                            ON CONFLICT ({id_column}) DO UPDATE SET
                                username=COALESCE(EXCLUDED.username,{table_name}.username),
                                title=COALESCE(EXCLUDED.title,{table_name}.title),
                                story_url=EXCLUDED.story_url,
                                media_url=COALESCE(EXCLUDED.media_url,{table_name}.media_url),
                                thumbnail_url=COALESCE(EXCLUDED.thumbnail_url,{table_name}.thumbnail_url),
                                posted_at=COALESCE(EXCLUDED.posted_at,{table_name}.posted_at),
                                expires_at=COALESCE(EXCLUDED.expires_at,{table_name}.expires_at),
                                collected_at=now(),metadata={table_name}.metadata||EXCLUDED.metadata
                            """,
                            item["id"], item["username"], item["title"], item["url"],
                            item["media_url"], item["thumbnail_url"], item["posted_at"],
                            item["expires_at"], json.dumps(item["metadata"]),
                        )
            await finish_native_target(
                self.pool, self.SOURCE_NAME, int(target["id"]), success=True,
            )
            summary["items"] = len(items)
        except Exception as exc:
            is_empty = str(exc) == "empty_public_page"
            attempts = int(target.get("attempts") or 1)
            exponential_retry = min(24 * 60 * 60, 15 * 60 * (2 ** min(max(attempts - 1, 0), 6)))
            retry_after_seconds = getattr(exc, "retry_after_seconds", None)
            if isinstance(exc, _PublicPageHttpError) and exc.status_code == 429:
                retry_after_seconds = max(
                    exponential_retry,
                    int(retry_after_seconds or 0),
                )
            elif retry_after_seconds is None and not is_empty:
                retry_after_seconds = max(exponential_retry, 2 * 60 * 60)
            elif is_empty:
                retry_after_seconds = 24 * 60 * 60
            error = str(exc) if str(exc).startswith("public_http_") else (
                "empty_public_page" if is_empty else type(exc).__name__.lower()
            )
            await finish_native_target(
                self.pool, self.SOURCE_NAME, int(target["id"]), success=False,
                error=error,
                retry_after_seconds=retry_after_seconds,
            )
            logger.warning("Snapchat public scrape failed: %s", error)
            summary["error"] = error
        return summary


__all__ = ["SnapchatCollector"]
