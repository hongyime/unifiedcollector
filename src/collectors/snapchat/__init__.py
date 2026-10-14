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
from typing import Any, Optional
from urllib.parse import quote, urljoin, urlparse

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


def _decode_json_str(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except Exception:
        return s


def _allowed_public_page(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    return (
        parsed.scheme == "https"
        and host == "snapchat.com"
        and _PUBLIC_PATH_RE.fullmatch(parsed.path) is not None
    )


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
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return []
    try:
        next_data = json.loads(match.group(1))
    except (TypeError, ValueError):
        return []
    page = next_data.get("props", {}).get("pageProps", {})
    if not isinstance(page, dict):
        return []
    items: dict[str, dict[str, Any]] = {}

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
    return list(items.values())[:25]


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
                            raise RuntimeError(f"public_http_{response.status_code}")
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
                            VALUES ($1,$2,$3,$4,$5,$6,$7::timestamptz,$8::timestamptz,now(),$9::jsonb)
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
            await finish_native_target(
                self.pool, self.SOURCE_NAME, int(target["id"]), success=False,
                error=(str(exc) if str(exc).startswith("public_http_") else type(exc).__name__.lower()),
                retry_after_hours=24 if is_empty or "public_http_429" in str(exc) else 2,
            )
            logger.warning("Snapchat public scrape failed: %s", type(exc).__name__)
            summary["error"] = type(exc).__name__
        return summary


__all__ = ["SnapchatCollector"]
