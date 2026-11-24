"""Browser ingest event recording.

Split out of ``__init__.py`` (split step 11); re-exported there.
"""
import logging
from .constants import X_ZERO_PROGRESS_PROBES
from src.core.collection_surface import normalize_collection_surface
import asyncio
import json

logger = logging.getLogger("social_ingest")


async def _record_browser_ingest_event(
    pool,
    platform: str,
    endpoint: str,
    subject: str | None = None,
    *,
    observed_count: int = 0,
    stored_count: int = 0,
    collection_surface: str = "unknown",
    deduped_count: int = 0,
    failed_count: int = 0,
    outcome: str | None = None,
    error_class: str | None = None,
    metadata: dict | None = None,
) -> None:
    from . import BROWSER_TELEMETRY_WRITE_TIMEOUT_SECONDS
    try:
        async with asyncio.timeout(BROWSER_TELEMETRY_WRITE_TIMEOUT_SECONDS):
            await _write_browser_ingest_event(
                pool,
                platform,
                endpoint,
                subject,
                observed_count=observed_count,
                stored_count=stored_count,
                collection_surface=collection_surface,
                deduped_count=deduped_count,
                failed_count=failed_count,
                outcome=outcome,
                error_class=error_class,
                metadata=metadata,
            )
    except TimeoutError:
        log = logger.debug if endpoint == "browser_heartbeat" else logger.warning
        log(
            "browser ingest telemetry timed out after %.2fs platform=%s endpoint=%s subject=%s",
            BROWSER_TELEMETRY_WRITE_TIMEOUT_SECONDS,
            platform,
            endpoint,
            subject,
        )
    except Exception:
        logger.debug(
            "browser ingest telemetry insert failed platform=%s endpoint=%s subject=%s",
            platform,
            endpoint,
            subject,
            exc_info=True,
        )


async def _write_browser_ingest_event(
    pool,
    platform: str,
    endpoint: str,
    subject: str | None = None,
    *,
    observed_count: int = 0,
    stored_count: int = 0,
    collection_surface: str = "unknown",
    deduped_count: int = 0,
    failed_count: int = 0,
    outcome: str | None = None,
    error_class: str | None = None,
    metadata: dict | None = None,
) -> None:
    observed = max(0, int(observed_count or 0))
    stored = max(0, int(stored_count or 0))
    deduped = max(0, int(deduped_count or 0))
    failed = max(0, int(failed_count or 0))
    surface = normalize_collection_surface(collection_surface)
    event_outcome = str(outcome or ("partial" if stored and failed else "failed" if failed else "success" if stored or deduped else "empty"))
    if event_outcome not in {"success", "empty", "partial", "failed", "unknown"}:
        event_outcome = "unknown"
    safe_error_class = str(error_class or "").strip()[:64] or None
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO browser_ingest_events
              (platform, endpoint, subject, observed_count, stored_count,
               collection_surface, deduped_count, failed_count, outcome, error_class, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
            """,
            platform,
            endpoint,
            subject,
            observed,
            stored,
            surface,
            deduped,
            failed,
            event_outcome,
            safe_error_class,
            json.dumps(metadata or {}),
        )
        health_status = str((metadata or {}).get("health_status") or "").lower()
        if health_status == "browser_content_silent_zero":
            err_msg = (
                f"browser capture stalled: browser_content_silent_zero - "
                f"{(metadata or {}).get('health_reason') or 'zero candidates on populated feed'} (watchdog)"
            )
            await conn.execute(
                """
                INSERT INTO source_health
                  (source, status, last_error, updated_at)
                VALUES ($1, 'degraded', $2, NOW())
                ON CONFLICT (source) DO UPDATE SET
                  status = 'degraded',
                  last_error = $2,
                  updated_at = NOW()
                """,
                platform,
                err_msg,
            )
        elif _browser_event_marks_source_success(platform, endpoint, observed, stored, metadata):
            await conn.execute(
                """
                INSERT INTO source_health
                  (source, status, last_success_at, last_checked_at, last_error, updated_at)
                VALUES ($1, 'running', NOW(), NOW(), NULL, NOW())
                ON CONFLICT (source) DO UPDATE SET
                  status = 'running',
                  last_success_at = NOW(),
                  last_checked_at = NOW(),
                  last_error = NULL,
                  updated_at = NOW()
                """,
                platform,
            )


def _browser_event_marks_source_success(
    platform: str,
    endpoint: str,
    observed_count: int,
    stored_count: int,
    metadata: dict | None = None,
) -> bool:
    if not platform or platform == "bridge":
        return False
    if endpoint == "browser_heartbeat":
        if not isinstance(metadata, dict) or not metadata.get("running"):
            return False
        return str(metadata.get("health_status") or "").lower() not in {
            "recoverable_error_shell",
            "loop_cycle_timeout",
            "forced_cycle_error",
            "browser_content_silent_zero",
        }
    if endpoint == "media":
        if stored_count > 0:
            return True
        duplicate_count = _browser_event_duplicate_count(metadata)
        if observed_count > 0 and duplicate_count >= max(1, int(observed_count * 0.8)):
            return True
    elif observed_count > 0 or stored_count > 0:
        return True
    if not isinstance(metadata, dict):
        return False
    probe_reason = str(metadata.get("probe_reason") or "").strip()
    non_progress_probes = {
        "manual_backend_probe",
        "forced_recovery_started",
        "recoverable_error_shell",
    }
    if (
        platform == "x"
        and observed_count <= 0
        and stored_count <= 0
        and probe_reason in X_ZERO_PROGRESS_PROBES
    ):
        return False
    return bool(probe_reason and probe_reason not in non_progress_probes)


def _browser_event_duplicate_count(metadata: dict | None) -> int:
    if not isinstance(metadata, dict):
        return 0
    reject_stats = metadata.get("reject_stats")
    if not isinstance(reject_stats, dict):
        return 0
    total = 0
    for key in ("duplicate_content_id", "duplicate_sha256"):
        try:
            total += max(0, int(reject_stats.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return total
