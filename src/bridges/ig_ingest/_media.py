"""Media validation, candidate classification and recording.

Split out of ``__init__.py`` (split step 11); re-exported there.
"""
import logging
from ._handles import _tiktok_handle
from ._ids import _verify_url
from ._util import _int
from ._util import _item_meta
from ._util import _reject_count
from ._util import _top_reject_reason
from ._util import _url_hash
from .constants import KNOWN_PLATFORMS
from .constants import _THREADS_SYNTHETIC_MEDIA_ID
from pathlib import Path
from src.core.vault import write_atomic_artifact
from urllib.parse import urlparse
import json
import re

logger = logging.getLogger("social_ingest")


class BridgeMediaRejected(ValueError):
    """Extension-supplied media URL failed the SSRF/allowlist gate."""


# ---------------------------------------------------------------------------
# download + persist (generic over platform)
# ---------------------------------------------------------------------------
def _download_headers(platform: str, url: str, item: dict | None = None) -> dict:
    """Browser-like headers for CDN media fetched from extension-discovered URLs."""
    accept = "image/avif,image/webp,image/apng,image/svg+xml,image/*,video/*,*/*;q=0.8"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        ),
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
    }
    referers = {
        "facebook": "https://www.facebook.com/",
        "instagram": "https://www.instagram.com/",
        "threads": "https://www.threads.com/",
        "tiktok": "https://www.tiktok.com/",
        "x": "https://x.com/",
    }
    referer = referers.get(platform)
    if referer:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "cross-site"
        headers["Sec-Fetch-Mode"] = "no-cors"
        headers["Sec-Fetch-Dest"] = "image"
    return headers


def _is_allowed_media_host(hostname: str) -> bool:
    from . import BRIDGE_MEDIA_ALLOWED_HOSTS
    host = (hostname or "").lower().rstrip(".")
    for pattern in BRIDGE_MEDIA_ALLOWED_HOSTS:
        pattern = pattern.lower().strip().rstrip(".")
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if host.endswith(suffix) and host != suffix[1:]:
                return True
        elif host == pattern:
            return True
    return False


def _validate_media_url(url: str) -> None:
    parsed = urlparse(url or "")
    if parsed.scheme != "https":
        raise BridgeMediaRejected(f"scheme not allowed: {parsed.scheme or 'none'}")
    try:
        hostname = parsed.hostname
    except ValueError as exc:
        raise BridgeMediaRejected(f"invalid host: {exc}") from exc
    if not _is_allowed_media_host(hostname):
        raise BridgeMediaRejected(f"host not allowed: {hostname or 'none'}")


def _threads_media_content_base_id(platform: str, content_id: str, sha256: str | None = None) -> str:
    """Stabilize extension-generated Threads media IDs with the stored bytes.

    The browser bridge sometimes sends short synthetic IDs like ``img_abc123``
    for DOM media candidates. Threads can reuse those IDs for separate carousel
    or feed blobs, so using the raw value as ``media_items.content_id`` lets
    distinct files overwrite each other before the vault consistency check runs.
    Real platform IDs are left untouched.
    """
    base = str(content_id or "")
    digest = str(sha256 or "").strip().lower()
    if platform == "threads" and digest and _THREADS_SYNTHETIC_MEDIA_ID.match(base):
        return f"{base}_{digest[:12]}"
    return base


def _media_store_content_id(media_kind: str, content_base_id: str) -> str:
    return content_base_id if media_kind == "post" else f"{media_kind}_{content_base_id}"


def _write_extension_media_artifacts_sync(
    *,
    platform: str,
    safe_user: str,
    username: str,
    item: dict,
    data: bytes,
    ext: str,
    sha: str,
    ctype: str,
    media_kind: str,
    store_cid: str,
    dest: Path,
    url: str,
):
    from . import VAULT_ROOT, write_media_sidecar
    meta = item.get("meta") or {}
    meta_obj = dict(meta) if isinstance(meta, dict) else {}
    artifact = write_atomic_artifact(
        source=platform,
        artifact_id=f"extension/{media_kind}/{store_cid}",
        artifact_kind="media_blob",
        data=data,
        extension=ext,
        expected_sha256=sha,
        metadata={
            **meta_obj,
            "entity_id": safe_user,
            "entity_name": item.get("entity_name") or username,
            "content_type": ctype,
            "content_id": store_cid,
            "kind": media_kind,
            "filename": dest.name,
            "source_url": url,
            "request_url": url,
            "ingest_path": "extension",
            "legacy_path": str(dest),
            "rebuild_target_tables": ["media_items"],
        },
        root=VAULT_ROOT,
    )
    if artifact.path is None:
        return artifact, None, None, None
    stored_path = artifact.path
    meta_obj["vault_artifact"] = {
        "ok": artifact.ok,
        "partial": artifact.partial,
        "path": artifact.relative_path,
        "blob_path": artifact.blob_relative_path,
        "sha256": artifact.sha256,
        "file_size": artifact.file_size,
        "sidecar_ok": artifact.sidecar.ok if artifact.sidecar else None,
        "sidecar_path": artifact.sidecar.relative_path if artifact.sidecar else None,
        "duplicate_blob": artifact.duplicate_blob,
        "error": artifact.error,
    }
    sidecar = write_media_sidecar(
        source=platform,
        entity_id=safe_user,
        entity_name=item.get("entity_name") or username,
        content_type=ctype,
        content_id=store_cid,
        filename=dest.name,
        file_path=str(stored_path),
        file_size=len(data),
        width=None,
        height=None,
        sha256=sha,
        source_url=url,
        metadata=meta_obj,
        ingest_path="extension",
        kind=media_kind,
    )
    meta_obj["vault_sidecar"] = {
        "enabled": sidecar.enabled,
        "ok": sidecar.ok,
        "path": sidecar.relative_path,
        "error": sidecar.error,
    }
    return artifact, sidecar, json.dumps(meta_obj), stored_path


def _candidate_asset_role(item: dict | None, platform: str | None = None) -> str | None:
    meta = _item_meta(item)
    keys = []
    if platform:
        keys.append(f"{platform}_asset_role")
    keys.extend([
        "asset_role",
        "tiktok_asset_role",
        "x_asset_role",
        "facebook_asset_role",
        "threads_asset_role",
        "instagram_asset_role",
        "lemon8_asset_role",
    ])
    role = None
    for key in keys:
        role = meta.get(key)
        if role is None and isinstance(item, dict):
            role = item.get(key)
        if role:
            break
    role_s = str(role or "").strip().lower()
    return role_s or None


def _tiktok_asset_role(item: dict | None) -> str | None:
    return _candidate_asset_role(item, "tiktok")


def _candidate_dimensions(item: dict | None) -> tuple[int | None, int | None]:
    meta = _item_meta(item)
    width = _int(meta.get("width") if meta.get("width") is not None else (item or {}).get("width"))
    height = _int(meta.get("height") if meta.get("height") is not None else (item or {}).get("height"))
    return width, height


def _candidate_is_video(item: dict | None, platform: str | None = None) -> bool:
    role = _candidate_asset_role(item, platform) or ""
    ctype = str((item or {}).get("content_type") or "").lower()
    mime = str((item or {}).get("mime_type") or _item_meta(item).get("browser_upload_mime_type") or "").lower()
    url = str((item or {}).get("url") or "").lower()
    return (
        ctype == "video"
        or ctype.startswith("video/")
        or mime.startswith("video/")
        or "video" in role
        or "playaddr" in role
        or "/video/" in url
        or url.endswith(".mp4")
        or "video.twimg.com" in url
    )


def _tiktok_candidate_is_video(item: dict | None) -> bool:
    return _candidate_is_video(item, "tiktok")


def _looks_like_tiny_thumbnail(item: dict | None, detail: str | None = None) -> bool:
    role = _candidate_asset_role(item) or ""
    url = str((item or {}).get("url") or "").lower()
    detail_l = str(detail or "").lower()
    width, height = _candidate_dimensions(item)
    return (
        "too small" in detail_l
        or "thumbnail" in detail_l
        or "avatar" in role
        or "avatar" in url
        or "icon" in url
        or "logo" in url
        or bool(width and width < 160)
        or bool(height and height < 160)
    )


def _classify_tiktok_candidate_result(
    item: dict | None,
    *,
    saved: bool = False,
    reject_stats: dict | None = None,
    ingest_mode: str = "url",
    browser_result: dict | None = None,
) -> tuple[str, str | None, bool]:
    """Classify a TikTok media candidate into an actionable bucket.

    The aggregate saw/stored counters are useful but too blunt. This turns each
    candidate into a ledger row and queues only likely real video misses for a
    browser detail revisit.
    """
    if saved:
        return "stored", None, False

    result = browser_result if isinstance(browser_result, dict) else {}
    if result:
        if result.get("deduped") is True or _reject_count(result.get("deduped")) > 0:
            return "duplicate", "duplicate_sha256", False
        if _reject_count(result.get("saved")) > 0 or _reject_count(result.get("stored")) > 0:
            return "stored", None, False
        reason = str(result.get("reason") or "browser_upload_failed")
        if reason == "deferred_upload_budget":
            return (
                "deferred",
                reason,
                _tiktok_candidate_is_video(item) or bool((item or {}).get("browser_upload_only")),
            )
        if reason in {"duplicate_content_id", "duplicate_sha256"}:
            return "duplicate", reason, False
        if reason.startswith("http_") or reason in {"timeout", "failed", "browser_upload_failed"}:
            if _tiktok_candidate_is_video(item):
                return "short_lived_url", reason, True
            return "browser_fetch_failed", reason, False
        if reason in {"too_large", "too_large_header"}:
            return "too_large", reason, False
        if reason == "disallowed_host":
            return "disallowed_host", reason, False
        return "browser_upload_failed", reason, _tiktok_candidate_is_video(item)

    reason, detail = _top_reject_reason(reject_stats)
    if not reason:
        return "failed", None, _tiktok_candidate_is_video(item) and ingest_mode == "browser_upload"
    if reason in {"duplicate_content_id", "duplicate_sha256"}:
        return "duplicate", reason, False
    if reason == "invalid_media":
        if _looks_like_tiny_thumbnail(item, detail):
            return "tiny_thumbnail", detail or reason, False
        return "invalid_media", detail or reason, _tiktok_candidate_is_video(item)
    if reason == "http_status":
        if _tiktok_candidate_is_video(item):
            return "short_lived_url", detail or reason, True
        return "http_error", detail or reason, False
    if reason in {"bad_uploaded_media", "exception", "timeout", "artifact_write_failed"}:
        return reason, detail or reason, _tiktok_candidate_is_video(item)
    if reason == "vault_unavailable":
        return "vault_unavailable", detail or reason, False
    return reason, detail or reason, False


def _classify_browser_candidate_result(
    platform: str,
    item: dict | None,
    *,
    saved: bool = False,
    reject_stats: dict | None = None,
    ingest_mode: str = "url",
    browser_result: dict | None = None,
) -> tuple[str, str | None, bool]:
    if platform == "tiktok":
        return _classify_tiktok_candidate_result(
            item,
            saved=saved,
            reject_stats=reject_stats,
            ingest_mode=ingest_mode,
            browser_result=browser_result,
        )

    is_video = _candidate_is_video(item, platform)
    if saved:
        return "stored", None, False

    result = browser_result if isinstance(browser_result, dict) else {}
    if result:
        if result.get("deduped") is True or _reject_count(result.get("deduped")) > 0:
            return "duplicate", "duplicate_sha256", False
        if _reject_count(result.get("saved")) > 0 or _reject_count(result.get("stored")) > 0:
            return "stored", None, False
        reason = str(result.get("reason") or "browser_upload_failed")
        if reason == "deferred_upload_budget":
            return (
                "deferred",
                reason,
                is_video or bool((item or {}).get("browser_upload_only")),
            )
        if reason in {"duplicate_content_id", "duplicate_sha256"}:
            return "duplicate", reason, False
        if reason.startswith("http_") or reason in {"timeout", "failed", "browser_upload_failed"}:
            return "browser_fetch_failed", reason, is_video
        if reason in {"too_large", "too_large_header", "disallowed_host"}:
            return reason, reason, False
        if reason == "invalid_media" and _looks_like_tiny_thumbnail(item):
            return "tiny_thumbnail", reason, False
        return "browser_upload_failed", reason, is_video

    reason, detail = _top_reject_reason(reject_stats)
    if not reason:
        return "failed", None, is_video and ingest_mode == "browser_upload"
    if reason in {"duplicate_content_id", "duplicate_sha256"}:
        return "duplicate", reason, False
    if reason == "invalid_media":
        if _looks_like_tiny_thumbnail(item, detail):
            return "tiny_thumbnail", detail or reason, False
        return "invalid_media", detail or reason, is_video
    if reason == "http_status":
        return "http_error", detail or reason, is_video
    if reason in {"bad_uploaded_media", "exception", "timeout", "artifact_write_failed"}:
        return reason, detail or reason, is_video
    if reason == "vault_unavailable":
        return "vault_unavailable", detail or reason, False
    return reason, detail or reason, False


def _tiktok_candidate_post_url(item: dict | None, username: str | None) -> str | None:
    meta = _item_meta(item)
    for key in ("post_url", "canonical_url", "page_url"):
        value = str(meta.get(key) or (item or {}).get(key) or "").strip()
        if _canonical_browser_revisit_url("tiktok", value):
            return value
    source_url = str((item or {}).get("source_url") or (item or {}).get("url") or "").strip()
    if _canonical_browser_revisit_url("tiktok", source_url):
        return source_url
    handle = _tiktok_handle(username)
    content_id = str((item or {}).get("content_id") or "").strip()
    if handle and content_id.isdigit():
        return f"https://www.tiktok.com/@{handle}/video/{content_id}"
    return None


def _canonical_browser_revisit_url(platform: str, value: str | None) -> str | None:
    """Return only a first-party detail permalink supported by the browser worker."""
    raw = str(value or "").strip()
    if not raw.startswith("https://"):
        return None
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
        path = parsed.path or ""
        port = parsed.port
    except Exception:
        return None
    if parsed.username or parsed.password or port not in (None, 443):
        return None
    if platform == "instagram":
        first_party = host in {"instagram.com", "www.instagram.com"}
        valid_path = re.match(r"^/(?:p|reel|reels|tv|stories)/[^/]+(?:/[^/]+)?/?$", path, re.I)
    elif platform == "threads":
        first_party = host in {"threads.com", "www.threads.com", "threads.net", "www.threads.net"}
        valid_path = re.match(r"^/@[^/]+/post/[^/]+/?$", path, re.I)
    elif platform == "tiktok":
        first_party = host in {"tiktok.com", "www.tiktok.com"}
        valid_path = re.match(r"^/@[^/]+/(?:video|photo)/\d+/?$", path, re.I)
    elif platform == "x":
        first_party = host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
        valid_path = re.match(r"^/[A-Za-z0-9_]{1,20}/status/\d+/?$", path)
    elif platform == "facebook":
        first_party = host == "facebook.com" or host.endswith(".facebook.com")
        valid_path = re.match(r"^/(?:[^/]+/)?(?:posts|photos|videos)/[^/]+/?$|^/(?:photo|permalink|story)\.php(?:/|$)", path, re.I)
    elif platform == "lemon8":
        first_party = "lemon8" in host
        valid_path = re.search(r"/(?:@[^/]+/)?\d{6,}(?:/|$)|/@[^/]+", path, re.I)
    else:
        return None
    return raw if first_party and valid_path else None


def _browser_candidate_post_url(platform: str, item: dict | None, username: str | None) -> str | None:
    meta = _item_meta(item)
    for key in ("post_url", "canonical_url", "verify_url", "page_url", "url"):
        value = str(meta.get(key) or (item or {}).get(key) or "").strip()
        if _canonical_browser_revisit_url(platform, value):
            return value
    if platform == "instagram":
        content_id = str((item or {}).get("content_id") or "").strip()
        media_id = re.split(r"[_:/-]", content_id, maxsplit=1)[0]
        if media_id.isdigit():
            return _verify_url("instagram", media_id)
    if platform == "x":
        meta_post_id = str(meta.get("post_id") or (item or {}).get("post_id") or "").strip()
        author = str(meta.get("author_username") or username or "").strip().lstrip("@")
        if author and meta_post_id:
            return f"https://x.com/{author}/status/{meta_post_id}"
    return None


def _browser_revisit_priority(platform: str, item: dict | None, outcome: str) -> int:
    if _candidate_is_video(item, platform):
        return 90
    if outcome in {"browser_fetch_failed", "http_error", "short_lived_url"}:
        return 75
    return 60


def _prepare_tiktok_browser_candidate(
    username: str | None,
    item: dict | None,
    *,
    ingest_mode: str,
    saved: bool = False,
    reject_stats: dict | None = None,
    extension_version: str | None = None,
    browser_result: dict | None = None,
) -> tuple[tuple, tuple | None] | None:
    if not isinstance(item, dict):
        return None
    url = str(item.get("url") or "").strip()
    if not url:
        return None
    content_id = str(item.get("content_id") or _url_hash(url)[:32])[:300]
    meta = _item_meta(item)
    ext_version = extension_version or str(meta.get("extension_version") or "") or None
    width, height = _candidate_dimensions(item)
    file_size = _int(item.get("file_size") or meta.get("browser_upload_size"))
    mime_type = item.get("mime_type") or item.get("content_type_header") or meta.get("browser_upload_mime_type")
    outcome, reason, needs_revisit = _classify_tiktok_candidate_result(
        item,
        saved=saved,
        reject_stats=reject_stats,
        ingest_mode=ingest_mode,
        browser_result=browser_result,
    )
    post_url = _tiktok_candidate_post_url(item, username)
    priority = 95 if _tiktok_candidate_is_video(item) else 65
    metadata = {
        "raw_meta": meta,
        "browser_upload": bool(item.get("browser_upload")),
        "browser_upload_only": bool(item.get("browser_upload_only")),
    }
    if reject_stats:
        metadata["reject_stats"] = reject_stats
    if browser_result:
        metadata["browser_result"] = browser_result
    candidate_args = (
        content_id,
        username,
        url[:2000],
        _url_hash(url),
        _tiktok_asset_role(item),
        str(item.get("content_type") or "")[:40] or None,
        width,
        height,
        file_size,
        str(mime_type)[:120] if mime_type else None,
        ext_version,
        ingest_mode,
        outcome,
        reason,
        needs_revisit,
        json.dumps(metadata, default=str),
    )
    revisit_args = None
    if needs_revisit and post_url:
        revisit_args = (
            content_id,
            username,
            post_url,
            url[:2000],
            reason or outcome,
            priority,
            json.dumps(metadata, default=str),
        )
    return candidate_args, revisit_args


async def _record_tiktok_browser_candidate(
    pool,
    username: str | None,
    item: dict | None,
    *,
    ingest_mode: str,
    saved: bool = False,
    reject_stats: dict | None = None,
    extension_version: str | None = None,
    browser_result: dict | None = None,
) -> None:
    prepared = _prepare_tiktok_browser_candidate(
        username,
        item,
        ingest_mode=ingest_mode,
        saved=saved,
        reject_stats=reject_stats,
        extension_version=extension_version,
        browser_result=browser_result,
    )
    if prepared is None:
        return
    candidate_args, revisit_args = prepared
    content_id = candidate_args[0]
    try:
        async with pool.acquire() as conn:
            await conn.execute(_TIKTOK_BROWSER_MEDIA_CANDIDATE_UPSERT_SQL, *candidate_args)
            if revisit_args is not None:
                await conn.execute(_TIKTOK_BROWSER_REVISIT_UPSERT_SQL, *revisit_args)
    except Exception:
        logger.debug(
            "tiktok browser media candidate record failed content_id=%s mode=%s",
            content_id,
            ingest_mode,
            exc_info=True,
        )


def _prepare_browser_media_candidate(
    platform: str,
    username: str | None,
    item: dict | None,
    *,
    ingest_mode: str,
    saved: bool = False,
    reject_stats: dict | None = None,
    extension_version: str | None = None,
    browser_result: dict | None = None,
) -> tuple[tuple, tuple | None] | None:
    if platform not in KNOWN_PLATFORMS or not isinstance(item, dict):
        return None
    url = str(item.get("url") or "").strip()
    if not url:
        return None
    content_id = str(item.get("content_id") or _url_hash(url)[:32])[:300]
    meta = _item_meta(item)
    ext_version = extension_version or str(meta.get("extension_version") or "") or None
    width, height = _candidate_dimensions(item)
    file_size = _int(item.get("file_size") or meta.get("browser_upload_size"))
    mime_type = item.get("mime_type") or item.get("content_type_header") or meta.get("browser_upload_mime_type")
    outcome, reason, needs_revisit = _classify_browser_candidate_result(
        platform,
        item,
        saved=saved,
        reject_stats=reject_stats,
        ingest_mode=ingest_mode,
        browser_result=browser_result,
    )
    metadata = {
        "raw_meta": meta,
        "browser_upload": bool(item.get("browser_upload")),
        "browser_upload_only": bool(item.get("browser_upload_only")),
    }
    if reject_stats:
        metadata["reject_stats"] = reject_stats
    if browser_result:
        metadata["browser_result"] = browser_result
    candidate_args = (
        platform,
        content_id,
        username,
        url[:2000],
        _url_hash(url),
        _candidate_asset_role(item, platform),
        str(item.get("content_type") or "")[:40] or None,
        width,
        height,
        file_size,
        str(mime_type)[:120] if mime_type else None,
        ext_version,
        ingest_mode,
        outcome,
        reason,
        needs_revisit,
        json.dumps(metadata, default=str),
    )
    revisit_args = None
    if needs_revisit and platform != "tiktok":
        post_url = _browser_candidate_post_url(platform, item, username)
        if post_url:
            revisit_args = (
                platform,
                content_id,
                username,
                post_url,
                url[:2000],
                reason or outcome,
                _browser_revisit_priority(platform, item, outcome),
                json.dumps(metadata, default=str),
            )
    return candidate_args, revisit_args


async def _record_browser_media_candidate(
    pool,
    platform: str,
    username: str | None,
    item: dict | None,
    *,
    ingest_mode: str,
    saved: bool = False,
    reject_stats: dict | None = None,
    extension_version: str | None = None,
    browser_result: dict | None = None,
) -> None:
    prepared = _prepare_browser_media_candidate(
        platform,
        username,
        item,
        ingest_mode=ingest_mode,
        saved=saved,
        reject_stats=reject_stats,
        extension_version=extension_version,
        browser_result=browser_result,
    )
    if prepared is None:
        return
    candidate_args, revisit_args = prepared
    content_id = candidate_args[1]
    try:
        async with pool.acquire() as conn:
            try:
                await conn.execute(_BROWSER_MEDIA_CANDIDATE_UPSERT_SQL, *candidate_args)
            except Exception:
                logger.debug(
                    "browser media candidate record failed platform=%s content_id=%s mode=%s",
                    platform,
                    content_id,
                    ingest_mode,
                    exc_info=True,
                )
            if revisit_args is not None:
                try:
                    await conn.execute(_BROWSER_MEDIA_REVISIT_UPSERT_SQL, *revisit_args)
                except Exception:
                    logger.debug(
                        "browser media revisit queue insert failed platform=%s content_id=%s",
                        platform,
                        content_id,
                        exc_info=True,
                    )
    except Exception:
        logger.debug(
            "browser media candidate pool acquire failed platform=%s content_id=%s mode=%s",
            platform,
            content_id,
            ingest_mode,
            exc_info=True,
        )

    if platform == "tiktok":
        await _record_tiktok_browser_candidate(
            pool,
            username,
            item,
            ingest_mode=ingest_mode,
            saved=saved,
            reject_stats=reject_stats,
            extension_version=extension_version,
            browser_result=browser_result,
        )


_TIKTOK_BROWSER_MEDIA_CANDIDATE_UPSERT_SQL = """
INSERT INTO tiktok_browser_media_candidates
  (content_id, username, source_url, url_hash, asset_role,
   content_type, width, height, file_size, mime_type,
   extension_version, ingest_mode, outcome, reason,
   needs_revisit, metadata)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16::jsonb)
ON CONFLICT (content_id, url_hash, ingest_mode) DO UPDATE SET
  username = COALESCE(EXCLUDED.username, tiktok_browser_media_candidates.username),
  source_url = EXCLUDED.source_url,
  asset_role = COALESCE(EXCLUDED.asset_role, tiktok_browser_media_candidates.asset_role),
  content_type = COALESCE(EXCLUDED.content_type, tiktok_browser_media_candidates.content_type),
  width = COALESCE(EXCLUDED.width, tiktok_browser_media_candidates.width),
  height = COALESCE(EXCLUDED.height, tiktok_browser_media_candidates.height),
  file_size = COALESCE(EXCLUDED.file_size, tiktok_browser_media_candidates.file_size),
  mime_type = COALESCE(EXCLUDED.mime_type, tiktok_browser_media_candidates.mime_type),
  extension_version = COALESCE(EXCLUDED.extension_version, tiktok_browser_media_candidates.extension_version),
  outcome = EXCLUDED.outcome,
  reason = EXCLUDED.reason,
  needs_revisit = EXCLUDED.needs_revisit,
  metadata = tiktok_browser_media_candidates.metadata || EXCLUDED.metadata,
  attempts = tiktok_browser_media_candidates.attempts + 1,
  last_seen = now()
WHERE tiktok_browser_media_candidates.outcome IS DISTINCT FROM 'stored'
   OR EXCLUDED.outcome = 'stored'
"""


_TIKTOK_BROWSER_REVISIT_UPSERT_SQL = """
INSERT INTO tiktok_browser_revisit_queue
  (content_id, username, post_url, source_url, reason, priority, metadata)
VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
ON CONFLICT (content_id) DO UPDATE SET
  username = COALESCE(EXCLUDED.username, tiktok_browser_revisit_queue.username),
  post_url = COALESCE(EXCLUDED.post_url, tiktok_browser_revisit_queue.post_url),
  source_url = COALESCE(EXCLUDED.source_url, tiktok_browser_revisit_queue.source_url),
  reason = EXCLUDED.reason,
  priority = GREATEST(tiktok_browser_revisit_queue.priority, EXCLUDED.priority),
  metadata = tiktok_browser_revisit_queue.metadata || EXCLUDED.metadata,
  updated_at = now()
"""


_BROWSER_MEDIA_CANDIDATE_UPSERT_SQL = """
INSERT INTO browser_media_candidates
  (platform, content_id, username, source_url, url_hash, asset_role,
   content_type, width, height, file_size, mime_type,
   extension_version, ingest_mode, outcome, reason,
   needs_revisit, metadata)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17::jsonb)
ON CONFLICT (platform, content_id, url_hash, ingest_mode) DO UPDATE SET
  username = COALESCE(EXCLUDED.username, browser_media_candidates.username),
  source_url = EXCLUDED.source_url,
  asset_role = COALESCE(EXCLUDED.asset_role, browser_media_candidates.asset_role),
  content_type = COALESCE(EXCLUDED.content_type, browser_media_candidates.content_type),
  width = COALESCE(EXCLUDED.width, browser_media_candidates.width),
  height = COALESCE(EXCLUDED.height, browser_media_candidates.height),
  file_size = COALESCE(EXCLUDED.file_size, browser_media_candidates.file_size),
  mime_type = COALESCE(EXCLUDED.mime_type, browser_media_candidates.mime_type),
  extension_version = COALESCE(EXCLUDED.extension_version, browser_media_candidates.extension_version),
  outcome = EXCLUDED.outcome,
  reason = EXCLUDED.reason,
  needs_revisit = EXCLUDED.needs_revisit,
  metadata = browser_media_candidates.metadata || EXCLUDED.metadata,
  attempts = browser_media_candidates.attempts + 1,
  last_seen = now()
WHERE browser_media_candidates.outcome IS DISTINCT FROM 'stored'
   OR EXCLUDED.outcome = 'stored'
"""


_BROWSER_MEDIA_REVISIT_UPSERT_SQL = """
INSERT INTO browser_media_revisit_queue
  (platform, content_id, username, post_url, source_url, reason,
   priority, metadata)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
ON CONFLICT (platform, content_id) DO UPDATE SET
  username = COALESCE(EXCLUDED.username, browser_media_revisit_queue.username),
  post_url = COALESCE(EXCLUDED.post_url, browser_media_revisit_queue.post_url),
  source_url = COALESCE(EXCLUDED.source_url, browser_media_revisit_queue.source_url),
  reason = EXCLUDED.reason,
  priority = GREATEST(browser_media_revisit_queue.priority, EXCLUDED.priority),
  metadata = browser_media_revisit_queue.metadata || EXCLUDED.metadata,
  updated_at = now()
"""
