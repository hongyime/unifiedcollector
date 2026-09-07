"""Source-matrix payload cache and fallback-payload helpers.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A step 6.

Scope of this file (this iteration):

* Cache helpers: reading and writing the source-matrix payload cache (in-memory
  + on-disk persistence).
* Fallback payload builders: shaping the "source matrix is unavailable"
  response used when the DB pool is exhausted or the builder times out.
* Empty ``APIRouter()`` — the ``/collectors/source-matrix`` route still lives
  in ``__init__.py`` for this step because the huge ``_collectors_source_matrix_payload``
  builder and its row/blocker/section helpers form a tightly-coupled cluster
  that will be moved in a follow-up refactor. Test monkey-patch semantics
  (``dashboard_api._SOURCE_MATRIX_PAYLOAD_BUILD_TASK`` mutation) are the main
  constraint preventing an atomic move.

Dependencies are strictly leaf-facing: only stdlib, ``src.dashboard.api.helpers``,
and ``src.dashboard.api.browser`` (which itself only depends on helpers).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from fastapi import APIRouter

from src.dashboard.api.helpers import (
    _SOURCE_MATRIX_PAYLOAD_CACHE,
    _SOURCE_MATRIX_PAYLOAD_CACHE_PATH,
    _SOURCE_MATRIX_PAYLOAD_CACHE_TTL_SECONDS,
    _SOURCE_MATRIX_PAYLOAD_STALE_SECONDS,
    _copy_cache_value,
)
from src.dashboard.api.browser import (
    _browser_extension_fallback_payload,
    _browser_extension_fallback_payload_with_fast_ingest,
)

logger = logging.getLogger(__name__)


def _cfg(name: str, default):
    """Look up a name on the parent ``dashboard_api`` module first.

    Enables tests that patch ``dashboard_api._X`` (constants, helper functions,
    the payload builder, and the route handler) to have their patches take
    effect from inside this module. Falls back to whatever the caller passed
    as ``default``.
    """
    root = sys.modules.get("src.dashboard.api")
    if root is not None:
        return getattr(root, name, default)
    return default


# ---------------------------------------------------------------------------
# Router — placeholder. The /collectors/source-matrix route is registered in
# __init__.py this iteration because it needs read/write access to a module-
# level task variable that tests mutate directly. Reserved for the follow-up
# refactor that finishes the split.
# ---------------------------------------------------------------------------

router = APIRouter()


# ---------------------------------------------------------------------------
# Fallback payload builders.
# ---------------------------------------------------------------------------

def _source_matrix_unavailable_payload(
    error: str,
    live_sources: list[dict] | None = None,
    browser_extension: dict | None = None,
) -> dict:
    from datetime import datetime, timedelta, timezone
    generated_at = datetime.now(timezone.utc)
    current_hour_started_at = generated_at.replace(minute=0, second=0, microsecond=0)
    previous_hour_started_at = current_hour_started_at - timedelta(hours=1)
    _fallback_liveness_rows = _cfg("_source_matrix_fallback_liveness_rows", None)
    _source_matrix_row = _cfg("_source_matrix_row", None)
    _source_window_totals = _cfg("_source_window_totals", None)
    if live_sources is None:
        assert _fallback_liveness_rows is not None, "dashboard_api._source_matrix_fallback_liveness_rows must be defined"
        live_sources = _fallback_liveness_rows(
            "source matrix build timed out before a cache was available; showing known source skeleton"
        )
    if browser_extension is None:
        browser_extension = _browser_extension_fallback_payload(error)
    assert _source_matrix_row is not None, "dashboard_api._source_matrix_row must be defined"
    assert _source_window_totals is not None, "dashboard_api._source_window_totals must be defined"
    rows = [
        _source_matrix_row(
            source_row,
            None,
            None,
            None,
            None,
            {"stats_unavailable": True, "stats_error": error},
            None,
            [],
            generated_at,
            None,
        )
        for source_row in live_sources
    ]
    return {
        "generated_at": generated_at,
        "current_hour_started_at": current_hour_started_at,
        "last_complete_hour_started_at": previous_hour_started_at,
        "summary": {
            "current_hour": {
                **_source_window_totals(rows, "current_hour"),
                "started_at": current_hour_started_at,
                "elapsed_seconds": int((generated_at - current_hour_started_at).total_seconds()),
            },
            "last_complete_hour": {
                **_source_window_totals(rows, "last_complete_hour"),
                "started_at": previous_hour_started_at,
                "elapsed_seconds": 3600,
            },
            "last_24h": {
                **_source_window_totals(rows, "last_24h"),
                "started_at": generated_at - timedelta(hours=24),
                "elapsed_seconds": 86400,
            },
        },
        "sources": rows,
        "whatsapp_bridge_health": None,
        "browser_extension": {
            "expected_version": browser_extension.get("expected_version"),
            "extension_id": None,
            "reload_url": None,
            "maintenance": browser_extension.get("maintenance"),
            "ingest_health": browser_extension.get("ingest_health"),
            "issues": browser_extension.get("issues", []),
        },
        "errors": [{"section": "source_matrix", "error": error}],
        "cache": {"status": "unavailable"},
    }


async def _source_matrix_unavailable_payload_with_fast_health(error: str) -> dict:
    detail = (
        "source matrix build timed out before a cache was available; "
        "showing fast source_health fallback"
    )
    _fallback_rows_with_source_health = _cfg("_source_matrix_fallback_liveness_rows_with_source_health", None)
    assert _fallback_rows_with_source_health is not None
    live_sources = await _fallback_rows_with_source_health(detail)
    browser_extension = await _browser_extension_fallback_payload_with_fast_ingest(error)
    return _source_matrix_unavailable_payload(error, live_sources, browser_extension)


# ---------------------------------------------------------------------------
# Payload cache helpers (in-memory + on-disk).
# ---------------------------------------------------------------------------

def _source_matrix_payload_stale_limit_seconds() -> float:
    return max(
        _cfg("_SOURCE_MATRIX_PAYLOAD_CACHE_TTL_SECONDS", _SOURCE_MATRIX_PAYLOAD_CACHE_TTL_SECONDS),
        _cfg("_SOURCE_MATRIX_PAYLOAD_STALE_SECONDS", _SOURCE_MATRIX_PAYLOAD_STALE_SECONDS),
    )


def _load_source_matrix_payload_cache(now: float | None = None) -> tuple[float, dict] | None:
    now = time.time() if now is None else now
    cache = _cfg("_SOURCE_MATRIX_PAYLOAD_CACHE", _SOURCE_MATRIX_PAYLOAD_CACHE)
    cached_payload = cache.get("payload")
    cached_ts = float(cache.get("ts") or 0.0)
    if cached_payload is not None and now - cached_ts <= _source_matrix_payload_stale_limit_seconds():
        return cached_ts, _copy_cache_value(cached_payload)
    return _load_persisted_source_matrix_payload(now)


def _load_persisted_source_matrix_payload(now: float | None = None) -> tuple[float, dict] | None:
    now = time.time() if now is None else now
    cache_path = _cfg("_SOURCE_MATRIX_PAYLOAD_CACHE_PATH", _SOURCE_MATRIX_PAYLOAD_CACHE_PATH)
    path = Path(cache_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - corrupt cache should not break dashboard
        logger.info("source matrix persisted cache ignored: %s", exc.__class__.__name__)
        return None

    try:
        ts = float(raw.get("ts") or 0.0)
    except (TypeError, ValueError):
        return None
    payload = raw.get("payload")
    if not isinstance(payload, dict) or ts <= 0:
        return None
    if now - ts > _source_matrix_payload_stale_limit_seconds():
        return None
    cache = _cfg("_SOURCE_MATRIX_PAYLOAD_CACHE", _SOURCE_MATRIX_PAYLOAD_CACHE)
    cache.update({"ts": ts, "payload": _copy_cache_value(payload)})
    return ts, _copy_cache_value(payload)


def _persist_source_matrix_payload(ts: float, payload: dict) -> None:
    cache_path = _cfg("_SOURCE_MATRIX_PAYLOAD_CACHE_PATH", _SOURCE_MATRIX_PAYLOAD_CACHE_PATH)
    path = Path(cache_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"ts": ts, "payload": payload}, default=str, separators=(",", ":")),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001 - cache persistence is best-effort
        logger.info("source matrix persisted cache write skipped: %s", exc.__class__.__name__)




# ---------------------------------------------------------------------------
# /collectors/source-matrix route.
#
# The ``_SOURCE_MATRIX_PAYLOAD_BUILD_TASK`` module-level global stays in
# ``__init__.py`` (tests assign to ``dashboard_api._SOURCE_MATRIX_PAYLOAD_BUILD_TASK``
# and rebind it via ``global`` semantics). This module reads / writes that
# global via ``sys.modules["src.dashboard.api"]`` so the write is visible to
# both the caller module and the parent namespace.
# ---------------------------------------------------------------------------

from fastapi import Depends
from src.dashboard.api.auth import require_role


def _get_build_task():
    root = sys.modules.get("src.dashboard.api")
    return getattr(root, "_SOURCE_MATRIX_PAYLOAD_BUILD_TASK", None) if root else None


def _set_build_task(task):
    root = sys.modules.get("src.dashboard.api")
    if root is not None:
        setattr(root, "_SOURCE_MATRIX_PAYLOAD_BUILD_TASK", task)


@router.get("/collectors/source-matrix")
async def collectors_source_matrix(
    _user: dict = Depends(require_role("viewer")),
    force_refresh: bool = False,
):
    _copy = _cfg("_copy_cache_value", _copy_cache_value)
    _payload_cache = _cfg("_SOURCE_MATRIX_PAYLOAD_CACHE", _SOURCE_MATRIX_PAYLOAD_CACHE)
    _payload_ttl = _cfg("_SOURCE_MATRIX_PAYLOAD_CACHE_TTL_SECONDS", _SOURCE_MATRIX_PAYLOAD_CACHE_TTL_SECONDS)
    _build_timeout = _cfg("_SOURCE_MATRIX_PAYLOAD_BUILD_TIMEOUT_SECONDS", 15.0)
    _builder = _cfg("_collectors_source_matrix_payload", None)
    _persist = _cfg("_persist_source_matrix_payload", _persist_source_matrix_payload)
    _load_persisted = _cfg("_load_persisted_source_matrix_payload", _load_persisted_source_matrix_payload)
    _stale_limit = _cfg("_source_matrix_payload_stale_limit_seconds", _source_matrix_payload_stale_limit_seconds)
    _fast_health = _cfg(
        "_source_matrix_unavailable_payload_with_fast_health",
        _source_matrix_unavailable_payload_with_fast_health,
    )

    now = time.time()
    cached_payload = _payload_cache.get("payload")
    cached_ts = float(_payload_cache.get("ts") or 0.0)
    if cached_payload is None and not force_refresh:
        persisted = _load_persisted(now)
        if persisted is not None:
            cached_ts, cached_payload = persisted
    if (
        not force_refresh
        and cached_payload is not None
        and _payload_ttl > 0
        and now - cached_ts <= _payload_ttl
    ):
        payload = _copy(cached_payload)
        payload["cache"] = {"status": "fresh", "age_seconds": int(now - cached_ts)}
        return payload

    def _background_build_done(task: asyncio.Task) -> None:
        if _get_build_task() is task:
            _set_build_task(None)
        try:
            value = task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - background cache refresh must fail soft
            logger.warning("source matrix background build failed: %s", exc.__class__.__name__)
            return
        ts = time.time()
        payload_copy = _copy(value)
        _payload_cache.update({"ts": ts, "payload": payload_copy})
        _persist(ts, payload_copy)

    active_task = _get_build_task()
    if active_task is not None and active_task.done():
        _background_build_done(active_task)
        active_task = None
    if active_task is not None:
        if (
            not force_refresh
            and cached_payload is not None
            and now - cached_ts <= _stale_limit()
        ):
            payload = _copy(cached_payload)
            payload["cache"] = {
                "status": "refreshing",
                "age_seconds": int(now - cached_ts),
                "refresh": "in_progress",
            }
            return payload
        return await _fast_health("BuildInProgress")

    if (
        not force_refresh
        and cached_payload is not None
        and now - cached_ts <= _stale_limit()
    ):
        build_task = asyncio.create_task(_builder())
        _set_build_task(build_task)
        build_task.add_done_callback(_background_build_done)
        payload = _copy(cached_payload)
        payload["cache"] = {
            "status": "refreshing",
            "age_seconds": int(now - cached_ts),
            "refresh": "started",
        }
        return payload

    build_task = asyncio.create_task(_builder())
    _set_build_task(build_task)
    build_task.add_done_callback(_background_build_done)
    try:
        done, _pending = await asyncio.wait(
            {build_task},
            timeout=max(1.0, _build_timeout),
        )
        if build_task not in done:
            raise asyncio.TimeoutError
        payload = build_task.result()
    except asyncio.TimeoutError:
        if cached_payload is not None and now - cached_ts <= _stale_limit():
            payload = _copy(cached_payload)
            payload.setdefault("errors", []).append({
                "section": "source_matrix",
                "error": "TimeoutError",
                "stale_cache": True,
                "cache_age_seconds": int(now - cached_ts),
            })
            payload["cache"] = {"status": "stale", "age_seconds": int(now - cached_ts)}
            logger.warning(
                "source matrix build timed out after %.1fs; serving stale payload age=%ds",
                max(1.0, _build_timeout),
                int(now - cached_ts),
            )
            return payload
        logger.warning(
            "source matrix build timed out after %.1fs with no cache",
            max(1.0, _build_timeout),
        )
        return await _fast_health("TimeoutError")
    except Exception as exc:  # noqa: BLE001 - route should not hard-fail during DB pressure
        if _get_build_task() is build_task:
            _set_build_task(None)
        if cached_payload is not None and now - cached_ts <= _stale_limit():
            payload = _copy(cached_payload)
            payload.setdefault("errors", []).append({
                "section": "source_matrix",
                "error": exc.__class__.__name__,
                "stale_cache": True,
                "cache_age_seconds": int(now - cached_ts),
            })
            payload["cache"] = {"status": "stale", "age_seconds": int(now - cached_ts)}
            return payload
        return await _fast_health(exc.__class__.__name__)
    ts = time.time()
    payload_copy = _copy(payload)
    _payload_cache.update({"ts": ts, "payload": payload_copy})
    _persist(ts, payload_copy)
    payload["cache"] = {"status": "rebuilt", "age_seconds": 0}
    return payload
