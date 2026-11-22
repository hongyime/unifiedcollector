"""Small pure helpers shared by the ig_ingest split modules.

Split out of ``__init__.py`` (split step 11); re-exported there.
"""
import logging
from src.core.media_filter import inspect as inspect_media
import hashlib
import json

logger = logging.getLogger("social_ingest")


def _int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


async def _safe_json(request):
    if request.get("_json_cache") is not None:
        return request["_json_cache"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    request["_json_cache"] = body
    return body


def _url_hash(url: str | None) -> str:
    return hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()


def _item_meta(item: dict | None) -> dict:
    if not isinstance(item, dict):
        return {}
    meta = item.get("meta")
    return dict(meta) if isinstance(meta, dict) else {}


def _top_reject_reason(reject_stats: dict | None) -> tuple[str | None, str | None]:
    if not isinstance(reject_stats, dict):
        return None, None
    examples = reject_stats.get("examples") if isinstance(reject_stats.get("examples"), dict) else {}
    best_reason = None
    best_count = -1
    for key, raw_count in reject_stats.items():
        if key == "examples":
            continue
        try:
            count = int(raw_count or 0)
        except (TypeError, ValueError):
            continue
        if count > best_count:
            best_reason = str(key)
            best_count = count
    detail = examples.get(best_reason) if best_reason and isinstance(examples, dict) else None
    return best_reason, str(detail) if detail is not None else None


def _reject_count(value) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _inspect_media_payload(data: bytes, ct_header: str | None) -> tuple[bool, str | None, str | None, str | None, str | None]:
    ok, ext, mtype, reason = inspect_media(data, ct_header)
    if not ok:
        return False, ext, mtype, reason, None
    return True, ext, mtype, reason, hashlib.sha256(data).hexdigest()


def _merge_reject_stats(dest: dict, src: dict | None) -> None:
    if not isinstance(src, dict):
        return
    for key, value in src.items():
        if key == "examples":
            examples = dest.setdefault("examples", {})
            if isinstance(examples, dict) and isinstance(value, dict):
                for reason, detail in value.items():
                    examples.setdefault(reason, detail)
            continue
        try:
            dest[key] = int(dest.get(key, 0) or 0) + int(value or 0)
        except (TypeError, ValueError):
            dest[key] = value


def _stream_values(container, key: str) -> list:
    """Extract a Strava stream array from web or API-shaped payloads."""
    if not container:
        return []
    if isinstance(container, dict):
        raw = container.get(key)
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict) and isinstance(raw.get("data"), list):
            return raw["data"]
    if isinstance(container, list):
        for item in container:
            if not isinstance(item, dict):
                continue
            if item.get("type") == key or item.get("name") == key or item.get("key") == key:
                data = item.get("data")
                return data if isinstance(data, list) else []
    return []


def _json_array_or_none(value: list | None) -> str | None:
    return json.dumps(value) if isinstance(value, list) and value else None


def _queued_browser_upload_response(platform: str, body: dict) -> dict:
    item = body.get("item") if isinstance(body.get("item"), dict) else {}
    return {
        "accepted": 1,
        "stored": 0,
        "saved": 0,
        "deduped": False,
        "queued": True,
        "platform": platform,
        "content_id": str(item.get("content_id") or "")[:200] if item else None,
    }
