"""Guarded, source-scoped checkpoint reset primitives for recovery replay.

The command path is intentionally opt-in: a manifest must carry an explicit
owner approval and exact cursor keys. Production replay execution is owned by
the later cutover task, not this module.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import stat
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any


class ReplayManifestError(ValueError):
    """A replay manifest is missing approval or exceeds its declared scope."""


class ReplayPreconditionError(RuntimeError):
    """A source is not quiesced or a cursor no longer matches its before-image."""


class ReconciliationManifestError(ValueError):
    """A task-12 broker/Redis reconciliation manifest is unsafe to apply."""


@asynccontextmanager
async def replay_ownership(conn):
    """Hold the global PostgreSQL replay lock for the full replay interval."""
    await conn.fetchval("SELECT pg_advisory_lock($1)", 731904182)
    try:
        yield
    finally:
        await conn.fetchval("SELECT pg_advisory_unlock($1)", 731904182)


def _persist_private_before_image(
    path: Path,
    payload: dict[str, Any],
    expected_state: dict[str, list[dict[str, Any]]],
    *,
    must_exist: bool,
) -> None:
    if not path.parent.is_dir() or stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        raise ReplayPreconditionError("before-image directory must exist with mode 700")
    if path.parent.stat().st_uid != os.getuid():
        raise ReplayPreconditionError("before-image directory must be owned by the current user")
    encoded = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    if path.exists():
        info = path.stat()
        if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise ReplayPreconditionError("before-image file must have mode 600")
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReplayPreconditionError("existing before-image is invalid") from exc
        if not isinstance(existing, dict):
            raise ReplayPreconditionError("existing before-image must be a JSON object")
        current = json.loads(encoded)
        for key in ("schema_version", "manifest_id", "source_identity", "account_identity", "target_keys", "historical_bounds", "maximum_work", "excluded_state"):
            if existing.get(key) != current.get(key):
                raise ReplayPreconditionError("existing before-image does not match this reset")
        for section, expected_rows in expected_state.items():
            existing_rows = existing.get(section, [])
            if not isinstance(existing_rows, list):
                raise ReplayPreconditionError("before-image state section is invalid")
            # Compare only the declared identity and before-image fields. The
            # full private row remains preserved for forensic readback.
            identity_fields = {
                "cursor_rows": ("service", "last_processed_id"),
                "worker_rows": ("service", "status"),
                "page_rows": ("term", "template_cursor"),
                "queue_rows": ("id", "source", "entity_id", "content_id", "status", "retry_count"),
            }[section]
            projected = [
                {key: row.get(key) for key in identity_fields}
                for row in existing_rows
            ]
            if projected != expected_rows:
                raise ReplayPreconditionError("before-image values do not match the approved manifest")
        return
    if must_exist:
        raise ReplayPreconditionError("apply requires a before-image created by dry-run")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _aware_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ReplayManifestError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReplayManifestError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ReplayManifestError(f"{field} must include a timezone")
    return parsed


def validate_replay_manifest(manifest: dict[str, Any]) -> None:
    """Validate exact source/account/target scope before any reset is possible."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ReplayManifestError("unsupported replay manifest schema")
    if not isinstance(manifest.get("manifest_id"), str) or not manifest["manifest_id"].strip():
        raise ReplayManifestError("manifest_id is required")
    approval = manifest.get("approval")
    if not isinstance(approval, dict) or approval.get("status") != "approved":
        raise ReplayManifestError("replay manifest is not preapproved")
    if not all(isinstance(approval.get(key), str) and approval[key].strip()
               for key in ("owner_identity", "approver_identity", "approved_at_utc", "approval_reference")):
        raise ReplayManifestError("owner approval identity, timestamp, and reference are required")
    if approval["owner_identity"] != approval["approver_identity"]:
        raise ReplayManifestError("only the recorded owner may approve a replay manifest")
    _aware_time(approval["approved_at_utc"], "approval.approved_at_utc")
    scopes = manifest.get("sources")
    if not isinstance(scopes, list) or not scopes:
        raise ReplayManifestError("at least one explicitly approved source is required")
    seen_sources: set[str] = set()
    for scope in scopes:
        if not isinstance(scope, dict):
            raise ReplayManifestError("source scope must be an object")
        source = scope.get("source_identity")
        account = scope.get("account_identity")
        if not isinstance(source, str) or not source.strip() or "*" in source:
            raise ReplayManifestError("source_identity must be exact and nonempty")
        if source in seen_sources:
            raise ReplayManifestError("source identities must be unique")
        seen_sources.add(source)
        if account is not None and (not isinstance(account, str) or not account.strip() or "*" in account):
            raise ReplayManifestError("account_identity must be null or exact")
        target_keys = scope.get("target_keys")
        if not isinstance(target_keys, list) or not target_keys or any(not isinstance(k, str) or not k.strip() or "*" in k for k in target_keys):
            raise ReplayManifestError("target_keys must be a nonempty exact list")
        if len(target_keys) != len(set(target_keys)):
            raise ReplayManifestError("target_keys must be unique")
        bounds = scope.get("historical_bounds")
        if not isinstance(bounds, dict):
            raise ReplayManifestError("historical_bounds are required")
        start = _aware_time(bounds.get("start_utc"), "historical_bounds.start_utc")
        end = _aware_time(bounds.get("end_utc"), "historical_bounds.end_utc")
        if start >= end:
            raise ReplayManifestError("historical bounds must have a positive interval")
        max_work = scope.get("maximum_work")
        if not isinstance(max_work, int) or isinstance(max_work, bool) or not 1 <= max_work <= len(target_keys):
            raise ReplayManifestError("maximum_work must be positive and bounded by target_keys")
        excluded = scope.get("excluded_state")
        if not isinstance(excluded, list) or not excluded or any(not isinstance(v, str) or not v.strip() for v in excluded):
            raise ReplayManifestError("excluded_state must explicitly name out-of-scope state")
        changes = scope.get("cursor_changes")
        if not isinstance(changes, list) or not changes:
            raise ReplayManifestError("cursor_changes must name exact reset keys")
        changed_keys: set[str] = set()
        for change in changes:
            if not isinstance(change, dict):
                raise ReplayManifestError("cursor change must be an object")
            key = change.get("service")
            if not isinstance(key, str) or not key.strip() or "*" in key:
                raise ReplayManifestError("cursor service key must be exact")
            if key in changed_keys:
                raise ReplayManifestError("cursor service keys must be unique within a source")
            changed_keys.add(key)
            if "expected_last_processed_id" not in change or "reset_to" not in change:
                raise ReplayManifestError("cursor changes require expected_last_processed_id and reset_to")
        worker_keys = scope.get("worker_service_keys")
        if not isinstance(worker_keys, list) or not worker_keys or any(
            not isinstance(key, str) or not key.strip() or "*" in key for key in worker_keys
        ):
            raise ReplayManifestError("worker_service_keys must explicitly name active-worker probes")
        if len(worker_keys) != len(set(worker_keys)):
            raise ReplayManifestError("worker service keys must be unique")
        page_changes = scope.get("page_state_changes", [])
        if not isinstance(page_changes, list):
            raise ReplayManifestError("page_state_changes must be a list")
        seen_pages: set[str] = set()
        for change in page_changes:
            if not isinstance(change, dict):
                raise ReplayManifestError("page-state change must be an object")
            term = change.get("term")
            if not isinstance(term, str) or term not in target_keys or term in seen_pages:
                raise ReplayManifestError("page-state term must be one unique approved target key")
            seen_pages.add(term)
            expected, reset_to = change.get("expected_template_cursor"), change.get("reset_to")
            if not isinstance(expected, int) or isinstance(expected, bool) or not 0 <= expected <= 31:
                raise ReplayManifestError("page-state before-image must be between 0 and 31")
            if not isinstance(reset_to, int) or isinstance(reset_to, bool) or not 0 <= reset_to <= 31:
                raise ReplayManifestError("page-state reset must be between 0 and 31")
        queue_changes = scope.get("queue_state_changes", [])
        if not isinstance(queue_changes, list):
            raise ReplayManifestError("queue_state_changes must be a list")
        seen_queue: set[tuple[str, str, str]] = set()
        for change in queue_changes:
            if not isinstance(change, dict):
                raise ReplayManifestError("queue-state change must be an object")
            source, entity_id, content_id = (change.get("source"), change.get("entity_id"), change.get("content_id"))
            if not all(isinstance(value, str) and value.strip() and "*" not in value for value in (source, entity_id, content_id)):
                raise ReplayManifestError("queue source/entity/content keys must be exact")
            if content_id not in target_keys:
                raise ReplayManifestError("queue content_id must be one approved target key")
            if not isinstance(change.get("id"), int) or isinstance(change.get("id"), bool) or change["id"] < 1:
                raise ReplayManifestError("queue row id must be a positive exact integer")
            identity = (source, entity_id, content_id)
            if identity in seen_queue:
                raise ReplayManifestError("queue state keys must be unique")
            seen_queue.add(identity)
            if any(
                not isinstance(change.get(field), int)
                or isinstance(change.get(field), bool)
                or change[field] < 0
                for field in ("expected_retry_count", "reset_retry_count")
            ):
                raise ReplayManifestError("queue retry-count before-image and reset are required")
            statuses = {"pending", "in_progress", "failed", "succeeded"}
            if change.get("expected_status") not in statuses or change.get("reset_status") not in statuses:
                raise ReplayManifestError("queue status before-image/reset is invalid")
    gaps = manifest.get("owner_accepted_gaps")
    if not isinstance(gaps, list):
        raise ReplayManifestError("owner_accepted_gaps must be explicit, including when empty")


async def reset_source_cursors(
    conn,
    manifest: dict[str, Any],
    source_identity: str,
    *,
    account_identity: str | None,
    workers_quiesced: bool,
    queue_drained: bool,
    leases_invalidated: bool,
    before_image_path: Path,
    apply: bool = False,
) -> dict[str, Any]:
    """Compare-and-swap only the named source's cursor rows.

    The caller must first capture a private before-image. Database status checks
    supplement, but do not replace, the explicit worker/queue/lease preconditions.
    """
    validate_replay_manifest(manifest)
    scopes = [s for s in manifest["sources"] if s["source_identity"] == source_identity]
    if len(scopes) != 1 or scopes[0]["account_identity"] != account_identity:
        raise ReplayManifestError("source/account pair is not listed in the approved manifest")
    if not (workers_quiesced and queue_drained and leases_invalidated):
        raise ReplayPreconditionError("worker, queue, and source lease must be quiesced before reset")
    scope = scopes[0]
    rows: list[dict[str, Any]] = []
    worker_rows: list[dict[str, Any]] = []
    page_rows: list[dict[str, Any]] = []
    queue_rows: list[dict[str, Any]] = []
    cursor_changed_count = 0
    page_changed_count = 0
    queue_changed_count = 0
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", 731904182)
        for service in scope["worker_service_keys"]:
            row = await conn.fetchrow(
                "SELECT service, status FROM service_cursors WHERE service = $1 FOR UPDATE",
                service,
            )
            if row is None:
                raise ReplayPreconditionError(f"worker service status row is missing: {service}")
            item = dict(row)
            if item["status"] == "running":
                raise ReplayPreconditionError(f"source worker is still running: {service}")
            worker_rows.append(item)
        for change in scope["cursor_changes"]:
            row = await conn.fetchrow(
                "SELECT service, last_processed_id, last_processed_at, status "
                "FROM service_cursors WHERE service = $1 FOR UPDATE",
                change["service"],
            )
            if row is None:
                raise ReplayPreconditionError(f"listed cursor row is missing: {change['service']}")
            item = dict(row)
            if item["status"] == "running":
                raise ReplayPreconditionError(f"source still reports a running worker: {change['service']}")
            if item["last_processed_id"] != change["expected_last_processed_id"]:
                if item["last_processed_id"] == change["reset_to"]:
                    rows.append(item)
                    continue
                raise ReplayPreconditionError(f"cursor before-image changed: {change['service']}")
            rows.append(item)
        for change in scope.get("page_state_changes", []):
            row = await conn.fetchrow(
                "SELECT term, template_cursor FROM search_dynamic_terms WHERE term = $1 FOR UPDATE",
                change["term"],
            )
            if row is None:
                raise ReplayPreconditionError(f"listed page state is missing: {change['term']}")
            item = dict(row)
            if item["template_cursor"] not in {change["expected_template_cursor"], change["reset_to"]}:
                raise ReplayPreconditionError(f"page-state before-image changed: {change['term']}")
            page_rows.append(item)
        for change in scope.get("queue_state_changes", []):
            row = await conn.fetchrow(
                """
                SELECT id, source, entity_id, content_id, status, retry_count, next_retry_at
                  FROM dead_letter_queue
                 WHERE id = $1 AND source = $2 AND entity_id IS NOT DISTINCT FROM $3
                   AND content_id = $4 FOR UPDATE
                """,
                change["id"], change["source"], change["entity_id"], change["content_id"],
            )
            if row is None:
                raise ReplayPreconditionError(f"listed queue state is missing: {change['id']}")
            item = dict(row)
            expected = (change["expected_status"], change["expected_retry_count"])
            reset = (change["reset_status"], change["reset_retry_count"])
            if (item["status"], item["retry_count"]) not in {expected, reset}:
                raise ReplayPreconditionError(f"queue-state before-image changed: {change['id']}")
            queue_rows.append(item)
        if not before_image_path.exists() and not apply:
            already_reset = any(
                change["expected_last_processed_id"] != change["reset_to"]
                and row["last_processed_id"] == change["reset_to"]
                for change, row in zip(scope["cursor_changes"], rows, strict=True)
            ) or any(
                change["expected_template_cursor"] != change["reset_to"]
                and row["template_cursor"] == change["reset_to"]
                for change, row in zip(scope.get("page_state_changes", []), page_rows, strict=True)
            ) or any(
                (change["expected_status"], change["expected_retry_count"])
                != (change["reset_status"], change["reset_retry_count"])
                and (row["status"], row["retry_count"])
                == (change["reset_status"], change["reset_retry_count"])
                for change, row in zip(scope.get("queue_state_changes", []), queue_rows, strict=True)
            )
            if already_reset:
                raise ReplayPreconditionError("cannot create a before-image after an unrecorded reset")
        before = {
            "schema_version": 1,
            "manifest_id": manifest["manifest_id"],
            "source_identity": source_identity,
            "account_identity": account_identity,
            "target_keys": scope["target_keys"],
            "historical_bounds": scope["historical_bounds"],
            "maximum_work": scope["maximum_work"],
            "excluded_state": scope["excluded_state"],
            "cursor_rows": rows,
            "worker_rows": worker_rows,
            "page_rows": page_rows,
            "queue_rows": queue_rows,
        }
        expected_state = {
            "cursor_rows": [
                {"service": change["service"], "last_processed_id": change["expected_last_processed_id"]}
                for change in scope["cursor_changes"]
            ],
            "worker_rows": [
                {"service": service, "status": "idle"}
                for service in scope["worker_service_keys"]
            ],
            "page_rows": [
                {"term": change["term"], "template_cursor": change["expected_template_cursor"]}
                for change in scope.get("page_state_changes", [])
            ],
            "queue_rows": [
                {key: change[key] for key in ("id", "source", "entity_id", "content_id", "expected_status", "expected_retry_count")}
                for change in scope.get("queue_state_changes", [])
            ],
        }
        # Store the original queue fields with their DB column names in the
        # expected projection used to verify an existing before-image.
        for projected, change in zip(expected_state["queue_rows"], scope.get("queue_state_changes", []), strict=True):
            projected["status"] = projected.pop("expected_status")
            projected["retry_count"] = projected.pop("expected_retry_count")
        _persist_private_before_image(
            before_image_path, before, expected_state, must_exist=apply,
        )
        if apply:
            for change, before in zip(scope["cursor_changes"], rows, strict=True):
                if before["last_processed_id"] == change["reset_to"]:
                    continue
                updated = await conn.fetchrow(
                    """
                    UPDATE service_cursors
                       SET last_processed_id = $3, last_processed_at = now(), status = 'idle'
                     WHERE service = $1
                       AND last_processed_id IS NOT DISTINCT FROM $2
                       AND status <> 'running'
                    RETURNING service
                    """,
                    change["service"], change["expected_last_processed_id"], change["reset_to"],
                )
                if updated is None:
                    raise ReplayPreconditionError(f"cursor compare-and-swap failed: {change['service']}")
                cursor_changed_count += 1
            for change, before_row in zip(scope.get("page_state_changes", []), page_rows, strict=True):
                if before_row["template_cursor"] == change["reset_to"]:
                    continue
                updated = await conn.fetchrow(
                    """
                    UPDATE search_dynamic_terms SET template_cursor = $2, updated_at = now()
                     WHERE term = $1 AND template_cursor = $3
                    RETURNING term
                    """,
                    change["term"], change["reset_to"], change["expected_template_cursor"],
                )
                if updated is None:
                    raise ReplayPreconditionError(f"page-state compare-and-swap failed: {change['term']}")
                page_changed_count += 1
            for change, before_row in zip(scope.get("queue_state_changes", []), queue_rows, strict=True):
                if (before_row["status"], before_row["retry_count"]) == (change["reset_status"], change["reset_retry_count"]):
                    continue
                updated = await conn.fetchrow(
                    """
                    UPDATE dead_letter_queue
                       SET status = $5, retry_count = $6, next_retry_at = now()
                     WHERE id = $1 AND source = $2 AND entity_id IS NOT DISTINCT FROM $3
                       AND content_id = $4 AND status = $7 AND retry_count = $8
                    RETURNING id
                    """,
                    change["id"], change["source"], change["entity_id"], change["content_id"],
                    change["reset_status"], change["reset_retry_count"],
                    change["expected_status"], change["expected_retry_count"],
                )
                if updated is None:
                    raise ReplayPreconditionError(f"queue-state compare-and-swap failed: {change['id']}")
                queue_changed_count += 1
    return {
        "manifest_id": manifest["manifest_id"],
        "source_identity": source_identity,
        "account_identity": account_identity,
        "mode": "apply" if apply else "dry-run",
        "cursor_rows_checked": len(rows),
        "cursor_rows_reset": cursor_changed_count,
        "page_rows_reset": page_changed_count,
        "queue_rows_reset": queue_changed_count,
        "state_rows_reset": cursor_changed_count + page_changed_count + queue_changed_count,
        "attempted": 0,
        "stored": 0,
        "duplicate": 0,
        "failed": 0,
        "residual_inaccessible_history": scope.get("residual_inaccessible_history", "unmeasured"),
    }


async def readback_source_cursors(conn, manifest: dict[str, Any], source_identity: str, *, account_identity: str | None) -> dict[str, Any]:
    """Read back only cursor keys named by one approved source/account scope."""
    validate_replay_manifest(manifest)
    scopes = [s for s in manifest["sources"] if s["source_identity"] == source_identity]
    if len(scopes) != 1 or scopes[0]["account_identity"] != account_identity:
        raise ReplayManifestError("source/account pair is not listed in the approved manifest")
    keys = [change["service"] for change in scopes[0]["cursor_changes"]]
    rows = await conn.fetch(
        "SELECT service, last_processed_id, last_processed_at, status "
        "FROM service_cursors WHERE service = ANY($1::text[]) ORDER BY service",
        keys,
    )
    by_key = {row["service"]: dict(row) for row in rows}
    return {
        "manifest_id": manifest["manifest_id"],
        "source_identity": source_identity,
        "account_identity": account_identity,
        "cursor_rows": [by_key.get(key) for key in keys],
        "missing_cursor_keys": [key for key in keys if key not in by_key],
    }


def _exact_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "*" in value:
        raise ReconciliationManifestError(f"{field} must be exact and nonempty")
    return value


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_artifact_path(value: Any) -> str:
    path = _exact_string(value, "artifact.path")
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ReconciliationManifestError("artifact.path must be relative to the approved artifact root")
    return path


def validate_reconciliation_manifest(manifest: dict[str, Any]) -> None:
    """Validate task 12's exact, source-scoped broker/Redis change set.

    The broker archive and replay payload are distinct artifacts.  A Mnesia
    archive alone is deliberately insufficient: task 12 must name an extracted
    bounded payload artifact before this tool can publish anything.
    """
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("task") != 12:
        raise ReconciliationManifestError("unsupported task-12 reconciliation manifest schema")
    approval = manifest.get("approval")
    if not isinstance(approval, dict) or approval.get("status") != "approved":
        raise ReconciliationManifestError("reconciliation manifest is not owner-approved")
    owner = _exact_string(approval.get("owner_identity"), "approval.owner_identity")
    if approval.get("approver_identity") != owner:
        raise ReconciliationManifestError("only the recorded owner may approve reconciliation changes")
    _aware_time(approval.get("approved_at_utc"), "approval.approved_at_utc")
    _exact_string(approval.get("approval_reference"), "approval.approval_reference")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ReconciliationManifestError("approved artifacts are required")
    known_artifacts: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ReconciliationManifestError("artifact must be an object")
        artifact_id = _exact_string(artifact.get("artifact_id"), "artifact_id")
        if artifact_id in known_artifacts:
            raise ReconciliationManifestError("artifact ids must be unique")
        kind = artifact.get("kind")
        if kind not in {"rabbitmq_mnesia", "rabbitmq_payload_ndjson", "redis_snapshot"}:
            raise ReconciliationManifestError("artifact kind is not allowed")
        _validate_artifact_path(artifact.get("path"))
        digest = artifact.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ReconciliationManifestError("artifact sha256 must be a lowercase SHA-256 digest")
        known_artifacts[artifact_id] = artifact

    gaps = manifest.get("owner_accepted_gaps")
    if not isinstance(gaps, list):
        raise ReconciliationManifestError("owner_accepted_gaps must be explicit, including when empty")
    for gap in gaps:
        if not isinstance(gap, dict):
            raise ReconciliationManifestError("owner accepted gap must be an object")
        _exact_string(gap.get("gap_id"), "owner_accepted_gaps.gap_id")
        _exact_string(gap.get("source_identity"), "owner_accepted_gaps.source_identity")
        if gap.get("status") != "accepted" or gap.get("owner_identity") != owner:
            raise ReconciliationManifestError("every recorded gap must be accepted by the recorded owner")
    if manifest.get("unaccepted_gaps"):
        raise ReconciliationManifestError("unaccepted reconciliation gaps cannot be applied")

    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ReconciliationManifestError("at least one source-scoped reconciliation change is required")
    seen_sources: set[tuple[str, str | None]] = set()
    for scope in sources:
        if not isinstance(scope, dict):
            raise ReconciliationManifestError("reconciliation source scope must be an object")
        source = _exact_string(scope.get("source_identity"), "source_identity")
        account = scope.get("account_identity")
        if account is not None:
            _exact_string(account, "account_identity")
        identity = (source, account)
        if identity in seen_sources:
            raise ReconciliationManifestError("source/account identities must be unique")
        seen_sources.add(identity)
        broker = scope.get("broker_replay")
        if not isinstance(broker, dict):
            raise ReconciliationManifestError("each source must name its broker replay and payload source")
        archive_id = _exact_string(broker.get("archive_artifact_id"), "broker_replay.archive_artifact_id")
        payload_id = _exact_string(broker.get("payload_artifact_id"), "broker_replay.payload_artifact_id")
        if archive_id not in known_artifacts or known_artifacts[archive_id]["kind"] != "rabbitmq_mnesia":
            raise ReconciliationManifestError("broker archive must name an approved RabbitMQ Mnesia artifact")
        if payload_id not in known_artifacts or known_artifacts[payload_id]["kind"] != "rabbitmq_payload_ndjson":
            raise ReconciliationManifestError("broker replay requires an approved extracted payload artifact")
        _exact_string(broker.get("queue"), "broker_replay.queue")
        _exact_string(broker.get("routing_key"), "broker_replay.routing_key")
        event_ids = broker.get("event_identities")
        if not isinstance(event_ids, list) or not event_ids:
            raise ReconciliationManifestError("broker replay must name exact event identities")
        if any(not isinstance(event_id, str) or not event_id.strip() or "*" in event_id for event_id in event_ids):
            raise ReconciliationManifestError("event identities must be exact")
        if len(event_ids) != len(set(event_ids)):
            raise ReconciliationManifestError("event identities must be unique")
        max_work = broker.get("maximum_work")
        if not isinstance(max_work, int) or isinstance(max_work, bool) or not 1 <= max_work < len(event_ids) + 1:
            raise ReconciliationManifestError("broker maximum_work must bound the named event identities")

        redis_changes = scope.get("redis_changes")
        if not isinstance(redis_changes, list):
            raise ReconciliationManifestError("redis_changes must be explicit, including when empty")
        seen_keys: set[str] = set()
        for change in redis_changes:
            if not isinstance(change, dict):
                raise ReconciliationManifestError("Redis change must be an object")
            if change.get("source_identity") != source:
                raise ReconciliationManifestError("Redis change source must match its source scope")
            key = _exact_string(change.get("key"), "redis key")
            if key in seen_keys:
                raise ReconciliationManifestError("Redis keys must be unique within a source scope")
            seen_keys.add(key)
            operation = change.get("operation")
            if operation not in {"delete", "set"}:
                raise ReconciliationManifestError("Redis operation must be delete or set")
            expected = change.get("expected_value_sha256")
            if expected is not None and (not isinstance(expected, str) or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected)):
                raise ReconciliationManifestError("Redis expected_value_sha256 must be a lowercase SHA-256 digest or null")
            if not isinstance(change.get("expected_missing"), bool):
                raise ReconciliationManifestError("Redis changes require expected_missing")
            if expected is None and not change["expected_missing"]:
                raise ReconciliationManifestError("present Redis state requires an expected value digest")
            if expected is not None and change["expected_missing"]:
                raise ReconciliationManifestError("Redis change cannot expect both a value and missing state")
            if operation == "set":
                encoded = change.get("value_base64")
                if not isinstance(encoded, str) or not encoded:
                    raise ReconciliationManifestError("Redis set requires a value_base64 payload")
                try:
                    base64.b64decode(encoded, validate=True)
                except ValueError as exc:
                    raise ReconciliationManifestError("Redis value_base64 is invalid") from exc
            elif "value_base64" in change:
                raise ReconciliationManifestError("Redis delete must not carry a replacement value")


def _read_approved_artifact(root: Path, artifact: dict[str, Any]) -> bytes:
    root = root.resolve()
    path = (root / str(artifact["path"])).resolve()
    if root not in path.parents:
        raise ReconciliationManifestError("artifact path escapes its approved root")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ReconciliationManifestError(f"approved artifact is unavailable: {artifact['artifact_id']}") from exc
    if _sha256(data) != artifact["sha256"]:
        raise ReconciliationManifestError(f"approved artifact digest mismatch: {artifact['artifact_id']}")
    return data


def _approved_reconciliation_scope(
    manifest: dict[str, Any], source_identity: str, account_identity: str | None,
) -> dict[str, Any]:
    validate_reconciliation_manifest(manifest)
    scopes = [
        scope for scope in manifest["sources"]
        if scope["source_identity"] == source_identity and scope.get("account_identity") == account_identity
    ]
    if len(scopes) != 1:
        raise ReconciliationManifestError("source/account pair is not listed in the approved reconciliation manifest")
    return scopes[0]


def _load_approved_payload(
    payload: bytes, broker: dict[str, Any], source_identity: str,
) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ReconciliationManifestError("approved replay payload must be UTF-8 NDJSON") from exc
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReconciliationManifestError("approved replay payload contains invalid JSON") from exc
        if not isinstance(record, dict):
            raise ReconciliationManifestError("approved replay payload rows must be objects")
        event_id = record.get("event_identity")
        if not isinstance(event_id, str) or event_id in records:
            raise ReconciliationManifestError("approved replay payload has duplicate or missing event identity")
        if record.get("source_identity") != source_identity:
            raise ReconciliationManifestError("approved replay payload event is outside its source scope")
        if record.get("queue") != broker["queue"] or record.get("routing_key") != broker["routing_key"]:
            raise ReconciliationManifestError("approved replay payload does not match the named queue/routing key")
        encoded = record.get("body_base64")
        if not isinstance(encoded, str):
            raise ReconciliationManifestError("approved replay payload event is missing body_base64")
        try:
            base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ReconciliationManifestError("approved replay payload body_base64 is invalid") from exc
        records[event_id] = record
    wanted = broker["event_identities"]
    if set(records) != set(wanted):
        raise ReconciliationManifestError("approved replay payload does not exactly match named event identities")
    return [records[event_id] for event_id in wanted]


async def _redis_state(redis_client: Any, change: dict[str, Any]) -> dict[str, Any]:
    value = await redis_client.get(change["key"])
    if value is None:
        if not change["expected_missing"]:
            raise ReplayPreconditionError(f"Redis before-image is missing: {change['key']}")
        return {"key": change["key"], "exists": False, "value_sha256": None}
    if isinstance(value, str):
        value = value.encode("utf-8")
    digest = _sha256(value)
    if change["expected_missing"] or digest != change["expected_value_sha256"]:
        raise ReplayPreconditionError(f"Redis before-image changed: {change['key']}")
    return {"key": change["key"], "exists": True, "value_sha256": digest}


async def apply_source_reconciliation(
    manifest: dict[str, Any],
    source_identity: str,
    *,
    account_identity: str | None,
    artifact_root: Path,
    mode: str,
    redis_client: Any | None = None,
    broker_publisher: Any | None = None,
) -> dict[str, Any]:
    """Apply exactly one owner-approved task-12 broker/Redis source scope.

    ``broker_publisher`` is an async callable receiving ``queue``,
    ``routing_key``, ``body`` and ``event_identity``. It is injected so the
    same gate is testable without connecting to a broker.
    """
    if mode not in {"dry-run", "apply", "readback"}:
        raise ReconciliationManifestError("reconciliation mode must be dry-run, apply, or readback")
    scope = _approved_reconciliation_scope(manifest, source_identity, account_identity)
    artifacts = {artifact["artifact_id"]: artifact for artifact in manifest["artifacts"]}
    broker = scope["broker_replay"]
    archive = _read_approved_artifact(artifact_root, artifacts[broker["archive_artifact_id"]])
    payload = _read_approved_artifact(artifact_root, artifacts[broker["payload_artifact_id"]])
    records = _load_approved_payload(payload, broker, source_identity)
    if len(records) > broker["maximum_work"]:
        raise ReconciliationManifestError("approved payload exceeds the source work cap")

    redis_before: list[dict[str, Any]] = []
    if redis_client is not None and mode != "readback":
        for change in scope["redis_changes"]:
            redis_before.append(await _redis_state(redis_client, change))
    elif redis_client is None and mode in {"apply", "readback"} and scope["redis_changes"]:
        raise ReplayPreconditionError("Redis client is required for listed Redis changes")

    published = 0
    if mode == "apply":
        if broker_publisher is None:
            raise ReplayPreconditionError("broker publisher is required for approved replay payload")
        for record in records:
            await broker_publisher(
                broker["queue"], broker["routing_key"],
                base64.b64decode(record["body_base64"]), record["event_identity"],
            )
            published += 1
        for change in scope["redis_changes"]:
            if change["operation"] == "delete":
                await redis_client.delete(change["key"])
            else:
                await redis_client.set(change["key"], base64.b64decode(change["value_base64"]))

    redis_readback: list[dict[str, Any]] = []
    if mode == "readback" and redis_client is not None:
        for change in scope["redis_changes"]:
            value = await redis_client.get(change["key"])
            if isinstance(value, str):
                value = value.encode("utf-8")
            redis_readback.append({
                "key": change["key"], "exists": value is not None,
                "value_sha256": _sha256(value) if value is not None else None,
            })
    return {
        "source_identity": source_identity,
        "account_identity": account_identity,
        "mode": mode,
        "broker_archive_sha256": _sha256(archive),
        "payload_sha256": _sha256(payload),
        "events_planned": len(records),
        "events_published": published,
        "redis_before": redis_before,
        "redis_readback": redis_readback,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a manifest-scoped cursor reset preflight")
    parser.add_argument("--manifest", type=Path, help="approved task-51 cursor replay manifest")
    parser.add_argument("--reconciliation-manifest", type=Path, help="approved task-12 broker/Redis manifest")
    parser.add_argument("--artifact-root", type=Path, help="root containing task-12 approved artifacts")
    parser.add_argument("--source", required=True)
    parser.add_argument("--account-identity", default=None)
    parser.add_argument("--before-image", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--readback", action="store_true")
    reconciliation_mode = parser.add_mutually_exclusive_group()
    reconciliation_mode.add_argument("--reconciliation-dry-run", action="store_true")
    reconciliation_mode.add_argument("--reconciliation-apply", action="store_true")
    reconciliation_mode.add_argument("--reconciliation-readback", action="store_true")
    parser.add_argument("--workers-quiesced", action="store_true")
    parser.add_argument("--queue-drained", action="store_true")
    parser.add_argument("--leases-invalidated", action="store_true")
    args = parser.parse_args()
    try:
        cursor_mode = "dry-run" if args.dry_run else "apply" if args.apply else "readback" if args.readback else None
        reconciliation_run_mode = (
            "dry-run" if args.reconciliation_dry_run else "apply"
            if args.reconciliation_apply else "readback"
            if args.reconciliation_readback else None
        )
        valid_cursor_invocation = bool(args.manifest and cursor_mode and not args.reconciliation_manifest and not reconciliation_run_mode)
        valid_reconciliation_invocation = bool(
            args.reconciliation_manifest and reconciliation_run_mode and not args.manifest and not cursor_mode
        )
        if not (valid_cursor_invocation or valid_reconciliation_invocation):
            raise ReplayManifestError("select exactly one cursor or reconciliation manifest and its matching mode")

        async def _run_cursor() -> dict[str, Any]:
            assert args.manifest is not None and cursor_mode is not None
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            validate_replay_manifest(manifest)
            from src.db.connection import close_pool, get_pool

            pool = await get_pool()
            try:
                async with pool.acquire() as conn:
                    if args.readback:
                        return await readback_source_cursors(
                            conn, manifest, args.source,
                            account_identity=args.account_identity,
                        )
                    if args.before_image is None:
                        raise ReplayManifestError("--before-image is required for dry-run/apply")
                    return await reset_source_cursors(
                        conn, manifest, args.source,
                        account_identity=args.account_identity,
                        workers_quiesced=args.workers_quiesced,
                        queue_drained=args.queue_drained,
                        leases_invalidated=args.leases_invalidated,
                        before_image_path=args.before_image,
                        apply=cursor_mode == "apply",
                    )
            finally:
                await close_pool()

        async def _run_reconciliation() -> dict[str, Any]:
            assert args.reconciliation_manifest is not None and reconciliation_run_mode is not None
            if args.artifact_root is None:
                raise ReconciliationManifestError("--artifact-root is required for reconciliation")
            manifest = json.loads(args.reconciliation_manifest.read_text(encoding="utf-8"))
            validate_reconciliation_manifest(manifest)
            if reconciliation_run_mode == "dry-run":
                return await apply_source_reconciliation(
                    manifest, args.source, account_identity=args.account_identity,
                    artifact_root=args.artifact_root, mode="dry-run",
                )
            redis_url = os.getenv("REDIS_URL")
            rabbitmq_url = os.getenv("RABBITMQ_URL")
            if not redis_url:
                raise ReplayPreconditionError("REDIS_URL is required for reconciliation readback/apply")
            if reconciliation_run_mode == "apply" and not rabbitmq_url:
                raise ReplayPreconditionError("RABBITMQ_URL is required for reconciliation apply")
            import redis.asyncio as aioredis

            redis_client = aioredis.from_url(redis_url, decode_responses=False)
            rabbit_connection = None
            try:
                publisher = None
                if reconciliation_run_mode == "apply":
                    import aio_pika

                    rabbit_connection = await aio_pika.connect_robust(rabbitmq_url)
                    channel = await rabbit_connection.channel()

                    async def publisher(queue: str, _routing_key: str, body: bytes, _event_identity: str) -> None:
                        await channel.default_exchange.publish(
                            aio_pika.Message(body=body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                            routing_key=queue,
                        )
                return await apply_source_reconciliation(
                    manifest, args.source, account_identity=args.account_identity,
                    artifact_root=args.artifact_root, mode=reconciliation_run_mode,
                    redis_client=redis_client, broker_publisher=publisher,
                )
            finally:
                await redis_client.aclose()
                if rabbit_connection is not None:
                    await rabbit_connection.close()

        result = asyncio.run(_run_cursor() if cursor_mode is not None else _run_reconciliation())
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    except (OSError, json.JSONDecodeError, ReplayManifestError, ReconciliationManifestError, ReplayPreconditionError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
