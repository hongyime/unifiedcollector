"""Browser telemetry endpoints for the ig_ingest bridge.

Extracted from ``__init__.py`` per PERF-003 step 9
(``docs/plans/perf-file-splits.md`` §4B).

Two telemetry hooks the extension calls constantly:

- ``browser_heartbeat_handler`` (POST /social/browser-heartbeat) — the
  main tab-status heartbeat. Records loop health, cycle counts, page-title
  samples, and returns recovery hints (e.g. "your content is 3h stale,
  hard-reload").
- ``sw_crash_handler`` (POST /social/sw-crash) — captures MV3 service-worker
  crashes so operators can trace unstable extension bundles.

Both are best-effort — the tab keeps polling regardless of DB state.
"""
import asyncio
import logging

from aiohttp import web

from .cors import _cors


logger = logging.getLogger("social_ingest")


async def browser_heartbeat_handler(request):
    from . import (
        BROWSER_CONTENT_HINT_RESPONSE_TIMEOUT_SECONDS,
        _browser_content_recovery_hint,
        _browser_content_timeout_hint,
        _extension_reload_hint,
        _norm_platform,
        _record_browser_ingest_event,
        _safe_json,
        _schedule_app_task,
    )

    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"), allow_diagnostics=True)
    running = bool(body.get("running"))
    url = body.get("url")
    label = body.get("label")
    subject = (
        str(body.get("owner") or body.get("account") or body.get("tab_id") or platform)
        .strip()[:128]
    )
    pool = request.app.get("pool")
    telemetry_degraded = pool is None
    try:
        async with asyncio.timeout(BROWSER_CONTENT_HINT_RESPONSE_TIMEOUT_SECONDS):
            recovery_hint = await _browser_content_recovery_hint(pool, platform)
    except TimeoutError:
        recovery_hint = _browser_content_timeout_hint(platform, "content_age_response_budget_exceeded")
    _schedule_app_task(
        request.app,
        _record_browser_ingest_event(
            pool,
            platform,
            "browser_heartbeat",
            subject,
            observed_count=1,
            stored_count=0,
            metadata={
                "running": running,
                "url": url,
                "label": label,
                "tab_id": body.get("tab_id"),
                "extension_version": body.get("extension_version"),
                "health_status": body.get("health_status"),
                "health_reason": body.get("health_reason"),
                "page_title": body.get("page_title"),
                "text_sample": body.get("text_sample"),
                "content_counts": body.get("content_counts"),
                "cycle_reason": body.get("cycle_reason"),
                "message_type": body.get("message_type"),
                "cycle_targets": body.get("cycle_targets"),
                "cycle_saved": body.get("cycle_saved"),
                "cycle_discovered": body.get("cycle_discovered"),
                "cycle_error": body.get("cycle_error"),
                "cooldown_left_ms": body.get("cooldown_left_ms"),
                "loop_running": body.get("loop_running"),
                "one_shot_running": body.get("one_shot_running"),
                "one_shot_age_ms": body.get("one_shot_age_ms"),
                "scrape_pass_running": body.get("scrape_pass_running"),
                "scrape_pass_age_ms": body.get("scrape_pass_age_ms"),
                "scrape_pass_reason": body.get("scrape_pass_reason"),
                "stale_after_ms": body.get("stale_after_ms"),
                "one_shot_timeout": body.get("one_shot_timeout"),
                "timeout_ms": body.get("timeout_ms"),
                "service_worker_recovery": body.get("service_worker_recovery"),
                "content_age_seconds": body.get("content_age_seconds"),
                "forced_age_ms": body.get("forced_age_ms"),
                "hard_reload_ms": body.get("hard_reload_ms"),
                "revived_content_script": body.get("revived_content_script"),
                "recovery_scheduled": body.get("recovery_scheduled"),
                "recovery_pending": body.get("recovery_pending"),
                "recovery_attempt": body.get("recovery_attempt"),
                "recovery_delay_ms": body.get("recovery_delay_ms"),
                "recovery_limit": body.get("recovery_limit"),
                "recovery_nav": body.get("recovery_nav"),
                "recovery_target_url": body.get("recovery_target_url"),
                "scraper_tabs_seen": body.get("scraper_tabs_seen"),
                "scraper_tabs_sent": body.get("scraper_tabs_sent"),
                "scraper_tabs_failed": body.get("scraper_tabs_failed"),
                "scraper_tabs_canonical": body.get("scraper_tabs_canonical"),
                "scraper_tabs_skipped": body.get("scraper_tabs_skipped"),
                "scraper_heartbeat_error": body.get("scraper_heartbeat_error"),
            },
        ),
        "browser_heartbeat_telemetry",
    )
    return _cors(web.json_response({
        "ok": True,
        "platform": platform,
        "running": running,
        "telemetry_degraded": telemetry_degraded,
        **recovery_hint,
        **_extension_reload_hint(body.get("extension_version")),
    }))


async def sw_crash_handler(request):
    """Accept extension MV3 service-worker crash reports."""
    from . import _record_browser_ingest_event, _safe_json, _schedule_app_task

    body = await _safe_json(request)
    kind = str(body.get("kind") or "sw_crash")[:64]
    message = str(body.get("message") or "")[:512]
    ext_version = str(body.get("extension_version") or "unknown")[:32]
    logger.warning(
        "extension SW crash: kind=%s ext=%s msg=%s",
        kind, ext_version, message,
    )
    pool = request.app.get("pool")
    subject = ext_version[:128]
    _schedule_app_task(
        request.app,
        _record_browser_ingest_event(
            pool,
            "bridge",
            "sw_crash",
            subject,
            observed_count=1,
            stored_count=0,
            metadata=body if isinstance(body, dict) else None,
        ),
        label="sw_crash_record",
    )
    return _cors(web.json_response({"ok": True}, status=202))
