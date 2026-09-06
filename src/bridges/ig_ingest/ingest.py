"""Ingest + upload route handlers for the ig_ingest bridge.

Extracted from ``__init__.py`` per PERF-003 step 5
(``docs/plans/perf-file-splits.md`` §4B).

These are thin request-adapter shims — the heavy lifting (``_ingest``,
``_ingest_uploaded_media``, ``_record_browser_media_candidate_batch``,
``_queued_browser_upload_response``) lives in ``__init__.py`` and is
imported lazily to sidestep circulars.

Endpoints owned here:

- POST /social/ingest                    → ``ingest``
- POST /social/ingest-upload             → ``ingest_upload``
- POST /social/ingest-upload-binary      → ``ingest_upload_binary``
- POST /ig/ingest                        → ``ingest_ig``
- POST /social/browser-media-candidates  → ``browser_media_candidates``
"""
import json
import logging

from aiohttp import web

from .cors import _cors


logger = logging.getLogger("social_ingest")


async def ingest(request):
    from . import _ingest, _norm_platform, _safe_json

    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    return _cors(web.json_response(await _ingest(request.app, platform, body)))


async def ingest_upload(request):
    from . import (
        _ingest_uploaded_media,
        _norm_platform,
        _queued_browser_upload_response,
        _safe_json,
        _schedule_app_task,
    )

    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    _schedule_app_task(
        request.app,
        _ingest_uploaded_media(request.app, platform, body),
        "browser_upload_ingest",
    )
    return _cors(web.json_response(_queued_browser_upload_response(platform, body)))


async def ingest_upload_binary(request):
    from . import (
        _ingest_uploaded_media,
        _norm_platform,
        _queued_browser_upload_response,
        _schedule_app_task,
    )

    body: dict = {}
    file_bytes: bytes | None = None
    file_mime: str | None = None
    file_name: str | None = None
    try:
        reader = await request.multipart()
        async for part in reader:
            if part.name == "metadata":
                try:
                    parsed = json.loads(await part.text())
                except Exception:
                    parsed = {}
                if isinstance(parsed, dict):
                    body = parsed
            elif part.name == "file":
                file_name = part.filename
                file_mime = part.headers.get("Content-Type")
                file_bytes = await part.read(decode=False)
    except Exception as exc:
        logger.warning("browser multipart upload parse failed: %s", exc.__class__.__name__)
        return _cors(web.json_response({"ok": False, "error": "bad_multipart"}, status=400))

    if not isinstance(body, dict):
        body = {}
    platform = _norm_platform(body.get("platform"))
    item = body.get("item") if isinstance(body.get("item"), dict) else {}
    item = dict(item)
    if not file_bytes:
        return _cors(web.json_response({"ok": False, "error": "missing_file", "platform": platform}, status=400))

    item["data_bytes"] = file_bytes
    if file_mime and not item.get("mime_type"):
        item["mime_type"] = file_mime
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    item["meta"] = {
        **meta,
        "browser_upload_transport": "multipart",
        "browser_upload_filename": file_name,
    }
    body["item"] = item
    body["file_size"] = len(file_bytes)
    if file_mime and not body.get("mime_type"):
        body["mime_type"] = file_mime

    _schedule_app_task(
        request.app,
        _ingest_uploaded_media(request.app, platform, body),
        "browser_upload_binary_ingest",
    )
    return _cors(web.json_response(_queued_browser_upload_response(platform, body)))


async def ingest_ig(request):  # /ig/ingest alias
    from . import _ingest, _safe_json

    body = await _safe_json(request)
    return _cors(web.json_response(await _ingest(request.app, "instagram", body)))


async def browser_media_candidates(request):
    from . import (
        _norm_platform,
        _record_browser_media_candidate_batch,
        _safe_json,
        _schedule_app_task,
    )

    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    username = body.get("username") or "unknown"
    extension_version = body.get("extension_version")
    raw_items = body.get("items") or []
    if not isinstance(raw_items, list):
        raw_items = []
    queued = min(len(raw_items), 500)
    if queued:
        _schedule_app_task(
            request.app,
            _record_browser_media_candidate_batch(
                request.app,
                platform,
                username,
                raw_items,
                extension_version,
            ),
            "browser_media_candidate_batch",
        )
    return _cors(web.json_response({"ok": True, "queued": queued, "platform": platform}))
