"""Extension media download + drain loop.

Split out of ``__init__.py`` (split step 11); re-exported there.
"""
import logging
from ._events import _browser_event_duplicate_count
from ._ids import _date_prefix
from ._media import BridgeMediaRejected
from ._media import _download_headers
from ._media import _media_store_content_id
from ._media import _threads_media_content_base_id
from ._media import _validate_media_url
from ._util import _merge_reject_stats
from ._util import _reject_count
from .constants import _SAFE
from .constants import _THREADS_SYNTHETIC_MEDIA_ID
from asyncpg.exceptions import UniqueViolationError
from pathlib import Path
from src.core.collection_surface import normalize_collection_surface
import aiohttp
import asyncio
import base64
import json

logger = logging.getLogger("social_ingest")


async def _record_vault_pause(pool, platform, store_cid, safe_user, reason: str) -> None:
    logger.warning(
        "vault/media unavailable before extension media write platform=%s cid=%s: %s",
        platform,
        store_cid,
        reason,
    )
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO dead_letter_queue (source, entity_id, content_id, error_message)
                VALUES ($1, $2, $3, $4)
                """,
                platform,
                safe_user,
                store_cid,
                f"vault/media unavailable before extension media write: {reason}"[:500],
            )
    except Exception:
        logger.debug(
            "vault/media unavailable DLQ insert failed for %s/%s",
            platform,
            store_cid,
            exc_info=True,
        )


async def _acquire_media_bytes(session, platform, url, item, reject):
    """Fetch or decode the media bytes. Returns (data, ct_header), or None after reject()."""
    from . import BRIDGE_MEDIA_MAX_BYTES

    data_bytes = item.get("data_bytes")
    data_b64 = item.get("data_b64")
    if isinstance(data_bytes, memoryview):
        data = data_bytes.tobytes()
        ct_header = item.get("mime_type") or item.get("content_type_header")
    elif isinstance(data_bytes, bytearray):
        data = bytes(data_bytes)
        ct_header = item.get("mime_type") or item.get("content_type_header")
    elif isinstance(data_bytes, bytes):
        data = data_bytes
        ct_header = item.get("mime_type") or item.get("content_type_header")
    elif data_b64:
        try:
            encoded = str(data_b64)
            if len(encoded) > 4 * ((BRIDGE_MEDIA_MAX_BYTES + 2) // 3):
                reject("media_too_large", f">{BRIDGE_MEDIA_MAX_BYTES} bytes")
                return None
            data = await asyncio.to_thread(base64.b64decode, encoded, validate=True)
        except Exception as exc:
            reject("bad_uploaded_media", exc.__class__.__name__)
            return None
        ct_header = item.get("mime_type") or item.get("content_type_header")
    else:
        try:
            _validate_media_url(url)
        except BridgeMediaRejected as exc:
            reject("blocked_url", str(exc))
            return None
        async with session.get(
            url,
            headers=_download_headers(platform, url, item),
            timeout=aiohttp.ClientTimeout(total=60),
        ) as r:
            if r.status != 200:
                reject("http_status", str(r.status))
                return None
            try:
                _validate_media_url(str(r.url or url))
            except BridgeMediaRejected as exc:
                reject("blocked_url", f"redirect {exc}")
                return None
            ct_header = r.headers.get("content-type")
            limit = BRIDGE_MEDIA_MAX_BYTES
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = await r.content.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    reject("media_too_large", f">{limit} bytes")
                    return None
                chunks.append(chunk)
            data = b"".join(chunks)
    return data, ct_header


async def _write_media_artifacts(pool, platform, safe_user, store_cid, dest, url, item, data, ext, sha, ctype, media_kind, username, reject):
    """Write artifact + sidecar. Returns (artifact, sidecar, meta_json, stored_path), or None after reject()."""
    from . import _write_extension_media_artifacts_sync

    artifact, sidecar, meta_json, stored_path = await asyncio.to_thread(
        _write_extension_media_artifacts_sync,
        platform=platform,
        safe_user=safe_user,
        username=username,
        item=item,
        data=data,
        ext=ext,
        sha=sha,
        ctype=ctype,
        media_kind=media_kind,
        store_cid=store_cid,
        dest=dest,
        url=url,
    )
    if artifact.path is None:
        await _record_vault_pause(pool, platform, store_cid, safe_user, artifact.error or "artifact write failed")
        reject("artifact_write_failed", artifact.error or "artifact write failed")
        return None
    if sidecar is None or meta_json is None or stored_path is None:
        await _record_vault_pause(pool, platform, store_cid, safe_user, "artifact sidecar state incomplete")
        reject("artifact_write_failed", "artifact sidecar state incomplete")
        return None
    return artifact, sidecar, meta_json, stored_path


async def _insert_media_row(pool, platform, safe_user, store_cid, dest, stored_path, data, sha, url, meta_json, media_kind, collection_surface, ctype, item, username, artifact, sidecar, reject) -> bool:
    """Insert the media_items row + DLQ + realtime enqueue. Returns False after reject() on duplicate."""
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO media_items
                  (source, entity_id, entity_name, content_type, content_id,
                   filename, file_path, file_size, sha256, source_url, metadata, kind,
                   ingest_path, collection_surface)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12,'extension',$13)
                ON CONFLICT (source, content_id) DO UPDATE SET
                   file_path = EXCLUDED.file_path,
                   file_size = EXCLUDED.file_size,
                   sha256 = EXCLUDED.sha256,
                   source_url = EXCLUDED.source_url,
                   metadata = EXCLUDED.metadata,
                   kind = EXCLUDED.kind,
                   ingest_path = 'extension',
                   collection_surface = CASE WHEN EXCLUDED.collection_surface = 'unknown'
                                              THEN media_items.collection_surface
                                              ELSE EXCLUDED.collection_surface END
                """,
                platform, safe_user, item.get("entity_name") or username, ctype, store_cid,
                dest.name, str(stored_path), len(data), sha, url, meta_json, media_kind,
                collection_surface,
            )
        except UniqueViolationError:
            # A concurrent same-bytes insert won after the advisory hash check.
            reject("duplicate_sha256")
            return False
        if artifact.partial or not artifact.ok:
            await conn.execute(
                """
                INSERT INTO dead_letter_queue (source, entity_id, content_id, error_message)
                VALUES ($1, $2, $3, $4)
                """,
                platform,
                safe_user,
                store_cid,
                f"vault artifact partial: {artifact.error}",
            )
        if sidecar.enabled and not sidecar.ok:
            await conn.execute(
                """
                INSERT INTO dead_letter_queue (source, entity_id, content_id, error_message)
                VALUES ($1, $2, $3, $4)
                """,
                platform,
                safe_user,
                store_cid,
                f"vault sidecar write failed: {sidecar.error}",
            )
        try:
            metadata_payload = json.loads(meta_json) if isinstance(meta_json, str) else meta_json
        except Exception:
            metadata_payload = {}
        enqueue_result = {"status": "stored_only", "reason": "enqueue_not_attempted"}
        try:
            from src.notifications import realtime_feed
            enqueue_result = realtime_feed.enqueue_from_insert(
                source=platform,
                entity_name=item.get("entity_name") or username,
                content_id=store_cid,
                file_path=str(stored_path),
                source_url=url,
                sha256=sha,
                metadata=metadata_payload if isinstance(metadata_payload, dict) else {},
                kind=media_kind,
                content_type=ctype,
                file_size=len(data),
            )
        except Exception:
            logger.debug(
                "realtime_feed enqueue failed for extension media %s/%s",
                platform,
                store_cid,
                exc_info=True,
            )
            enqueue_result = {"status": "stored_only", "reason": "enqueue_failed"}
        try:
            from src.notifications import realtime_delivery
            await realtime_delivery.record_with_conn(
                conn,
                source=platform,
                content_id=store_cid,
                status=str(enqueue_result.get("status") or "stored_only"),
                reason=enqueue_result.get("reason"),
                file_size=len(data),
                content_type=ctype,
            )
        except Exception:
            logger.debug(
                "realtime delivery enqueue ledger failed for extension media %s/%s",
                platform,
                store_cid,
                exc_info=True,
            )
    return True


async def _verify_media_consistency(pool, platform, safe_user, store_cid, stored_path, data, sha, sidecar) -> None:
    """Verify the stored row matches the vault artifact; DLQ on mismatch. Never raises."""
    from . import verify_media_item_db_consistency

    try:
        async with pool.acquire() as conn:
            consistency = await verify_media_item_db_consistency(
                conn,
                source=platform,
                content_id=store_cid,
                file_path=str(stored_path),
                file_size=len(data),
                sha256=sha,
                sidecar_path=sidecar.relative_path if sidecar.enabled else None,
            )
            consistency_meta = {
                "vault_artifact_db_consistency": {
                    "ok": consistency.ok,
                    "errors": list(consistency.errors),
                }
            }
            await conn.execute(
                """
                UPDATE media_items
                SET metadata = COALESCE(metadata, '{}'::jsonb) || $3::jsonb
                WHERE source = $1 AND content_id = $2
                """,
                platform,
                store_cid,
                json.dumps(consistency_meta, default=str),
            )
            if not consistency.ok:
                await conn.execute(
                    """
                    INSERT INTO dead_letter_queue (source, entity_id, content_id, error_message)
                    VALUES ($1, $2, $3, $4)
                    """,
                    platform,
                    safe_user,
                    store_cid,
                    "vault artifact db consistency failed: "
                    + "; ".join(consistency.errors),
                )
    except Exception:
        logger.warning(
            "vault sidecar status update failed for %s/%s",
            platform,
            store_cid,
            exc_info=True,
        )


async def _download_and_save(pool, session, platform, username, item, reject_stats: dict | None = None) -> bool:
    from . import BRIDGE_MEDIA_MAX_BYTES, MEDIA_ROOT, _inspect_media_payload, assert_media_write_allowed
    def _reject(reason: str, detail: str | None = None) -> bool:
        if reject_stats is not None:
            reject_stats[reason] = int(reject_stats.get(reason, 0)) + 1
            if detail:
                examples = reject_stats.setdefault("examples", {})
                if isinstance(examples, dict) and reason not in examples:
                    examples[reason] = detail[:180]
        return False

    url = item.get("url")
    cid = str(item.get("content_id") or "")
    collection_surface = normalize_collection_surface(
        item.get("collection_surface") or item.get("surface"),
        context=(item.get("meta") or {}).get("collection_surface") if isinstance(item.get("meta"), dict) else None,
    )
    if not url or not cid:
        return _reject("missing_url_or_content_id")
    # media kind: post (default) | story | highlight. Stories/highlights live in
    # their own subtree and get a namespaced content_id so they never collide with
    # a feed post that happens to share an id.
    media_kind = (item.get("kind") or "post").lower()
    if media_kind not in ("post", "story", "highlight", "tagged", "profile"):
        media_kind = "post"
    content_base_id = _threads_media_content_base_id(platform, cid)
    needs_content_sha = content_base_id == cid and platform == "threads" and _THREADS_SYNTHETIC_MEDIA_ID.match(cid)
    # DB dedup id stays namespaced so a story/highlight/tagged/profile can't collide.
    store_cid = _media_store_content_id(media_kind, content_base_id)
    safe_user = _SAFE.sub("_", username)[:80] or "unknown"
    # filename kind label (no subfolders anymore — kind is encoded in the name)
    kindtag = {"story": "story_", "highlight": "hl_", "tagged": "tagged_", "profile": "profile_"}.get(media_kind, "")
    datestr = _date_prefix(item, platform)

    try:
        # dedup authority is media_items (source, content_id)
        if not needs_content_sha:
            async with pool.acquire() as conn:
                seen = await conn.fetchval(
                    "SELECT 1 FROM media_items WHERE source=$1 AND content_id=$2", platform, store_cid
                )
            if seen:
                return _reject("duplicate_content_id")

        try:
            assert_media_write_allowed(
                Path(MEDIA_ROOT) / platform / f"account_{safe_user}" / ".extension_write_check",
                media_root=MEDIA_ROOT,
            )
        except RuntimeError as exc:
            await _record_vault_pause(pool, platform, store_cid, safe_user, str(exc))
            return _reject("vault_unavailable", str(exc))

        acquired = await _acquire_media_bytes(session, platform, url, item, _reject)
        if acquired is None:
            return False
        data, ct_header = acquired

        if len(data) > BRIDGE_MEDIA_MAX_BYTES:
            return _reject("media_too_large", f">{BRIDGE_MEDIA_MAX_BYTES} bytes")

        # GATE: keep only real PDF/image/video/audio above min size — drop
        # favicons, thumbnails, tracking pixels, sprite sheets, HTML error pages.
        ok, ext, mtype, reason, sha = await asyncio.to_thread(_inspect_media_payload, data, ct_header)
        if not ok:
            logger.debug("reject %s %s: %s", platform, store_cid, reason)
            return _reject("invalid_media", reason)
        if not sha:
            return _reject("invalid_media", "missing_sha256")
        if needs_content_sha:
            content_base_id = _threads_media_content_base_id(platform, cid, sha)
            store_cid = _media_store_content_id(media_kind, content_base_id)
            async with pool.acquire() as conn:
                seen = await conn.fetchval(
                    "SELECT 1 FROM media_items WHERE source=$1 AND content_id=$2", platform, store_cid
                )
            if seen:
                return _reject("duplicate_content_id")
        # CONTENT DEDUP: if these exact bytes are already stored for this source
        # (e.g. the same For-You image re-scraped under a different DOM content_id),
        # skip — no duplicate file or row. This is what kills the lemon8/tiktok
        # re-scrape duplication. Needs the (source, sha256) index for speed.
        async with pool.acquire() as conn:
            if await conn.fetchval("SELECT 1 FROM media_items WHERE source=$1 AND sha256=$2 LIMIT 1", platform, sha):
                return _reject("duplicate_sha256")
        ctype = "video" if mtype == "video" else ("pdf" if mtype == "pdf" else "photo")
        # Flat layout: /<platform>/account_<user>/<ctype>/  — kind + date live in the
        # filename: <YYYYMMDD>_<platform>_<user>_<kindtag><cid>.<ext> (sortable by date).
        dest_dir = Path(MEDIA_ROOT) / platform / f"account_{safe_user}" / ctype
        raw_cid = _SAFE.sub("_", content_base_id)[:100]
        dest = dest_dir / f"{datestr}_{platform}_{safe_user}_{kindtag}{raw_cid}.{ext}"
        try:
            assert_media_write_allowed(dest, media_root=MEDIA_ROOT)
        except RuntimeError as exc:
            await _record_vault_pause(pool, platform, store_cid, safe_user, str(exc))
            return _reject("vault_unavailable", str(exc))

        written = await _write_media_artifacts(
            pool, platform, safe_user, store_cid, dest, url, item, data, ext, sha,
            ctype, media_kind, username, _reject,
        )
        if written is None:
            return False
        artifact, sidecar, meta_json, stored_path = written
        if not await _insert_media_row(
            pool, platform, safe_user, store_cid, dest, stored_path, data, sha, url,
            meta_json, media_kind, collection_surface, ctype, item, username, artifact,
            sidecar, _reject,
        ):
            return False
        await _verify_media_consistency(pool, platform, safe_user, store_cid, stored_path, data, sha, sidecar)
        return True
    except Exception as exc:
        logger.debug("save failed platform=%s cid=%s", platform, cid, exc_info=True)
        return _reject("exception", f"{exc.__class__.__name__}: {exc}")


async def _drain(
    app, platform, username, items, extension_version: str | None = None,
    collection_surface: str = "unknown",
):
    """Background download worker — bounded concurrency, never blocks the POST."""
    from . import _download_and_save, _record_browser_ingest_event, _record_browser_media_candidate
    pool, session, sem = app["pool"], app["session"], app["sem"]
    saved = 0
    reject_stats: dict = {}

    async def one(it):
        nonlocal saved
        async with sem:
            payload = it
            if extension_version and isinstance(it, dict):
                payload = dict(it)
                meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
                payload["meta"] = {**meta, "extension_version": extension_version}
            elif isinstance(it, dict):
                payload = dict(it)
            if isinstance(payload, dict):
                payload["collection_surface"] = normalize_collection_surface(
                    payload.get("collection_surface") or collection_surface
                )
            item_stats: dict = {}
            item_saved = await _download_and_save(pool, session, platform, username, payload, item_stats)
            _merge_reject_stats(reject_stats, item_stats)
            if item_saved:
                saved += 1
            await _record_browser_media_candidate(
                pool,
                platform,
                username,
                payload if isinstance(payload, dict) else {},
                ingest_mode="url",
                saved=item_saved,
                reject_stats=item_stats,
                extension_version=extension_version,
            )

    await asyncio.gather(*(one(it) for it in items), return_exceptions=True)
    event_meta = {}
    if extension_version:
        event_meta["extension_version"] = extension_version
    if reject_stats:
        event_meta["reject_stats"] = reject_stats
    deduped = _browser_event_duplicate_count({"reject_stats": reject_stats})
    failed = max(0, len(items) - saved - deduped)
    failure_buckets = {
        name: _reject_count(count)
        for name, count in reject_stats.items()
        if name not in {"examples", "duplicate_content_id", "duplicate_sha256"}
    }
    failure_class = max(failure_buckets, key=failure_buckets.get) if failure_buckets else None
    await _record_browser_ingest_event(
        pool,
        platform,
        "media",
        username,
        observed_count=len(items),
        stored_count=saved,
        collection_surface=collection_surface,
        deduped_count=deduped,
        failed_count=failed,
        outcome="partial" if saved and failed else "failed" if failed else "success" if saved or deduped else "empty",
        error_class=failure_class,
        metadata=event_meta or None,
    )
    if platform == "instagram":
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE instagram_spider_targets SET last_scraped_at=now() WHERE username=$1",
                    username,
                )
        except Exception:
            pass
    logger.info("ingest[%s] %s: %d/%d saved", platform, username, saved, len(items))


BRIDGE_MEDIA_MAX_BYTES = 200 * 1024 * 1024
