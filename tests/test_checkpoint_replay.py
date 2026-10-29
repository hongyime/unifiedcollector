from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from src.core.checkpoint import CheckpointConflict, CheckpointManager
from src.core.checkpoint_replay import (
    ReconciliationManifestError,
    ReplayManifestError,
    ReplayPreconditionError,
    apply_source_reconciliation,
    readback_source_cursors,
    replay_ownership,
    reset_source_cursors,
    validate_replay_manifest,
    validate_reconciliation_manifest,
)


def _approved_manifest() -> dict:
    return {
        "schema_version": 1,
        "manifest_id": "test-replay-manifest",
        "approval": {
            "status": "approved",
            "owner_identity": "legal-data-use-owner",
            "approver_identity": "legal-data-use-owner",
            "approved_at_utc": "2026-10-23T00:00:00Z",
            "approval_reference": "test-owner-approval",
        },
        "owner_accepted_gaps": [],
        "sources": [{
            "source_identity": "telegram",
            "account_identity": "account:tg-01",
            "target_keys": ["telegram:chat:1001"],
            "historical_bounds": {
                "start_utc": "2026-10-01T00:00:00Z",
                "end_utc": "2026-10-02T00:00:00Z",
            },
            "maximum_work": 1,
            "excluded_state": ["telegram:chat:2002", "global service cursor"],
            "worker_service_keys": ["telegram"],
            "cursor_changes": [{
                "service": "telegram:chat:1001",
                "expected_last_processed_id": "100",
                "reset_to": None,
            }],
            "residual_inaccessible_history": "unmeasured",
        }],
    }


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _CursorConn:
    def __init__(self, cursors: dict[str, dict]):
        self.cursors = cursors
        self.calls = []

    def transaction(self):
        return _Transaction()

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        assert "pg_advisory_xact_lock" in sql
        return "SELECT 1"

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if "FOR UPDATE" in sql:
            return self.cursors.get(args[0])
        if sql.lstrip().startswith("UPDATE service_cursors"):
            service, expected, reset_to = args
            row = self.cursors[service]
            if row["last_processed_id"] != expected or row["status"] == "running":
                return None
            row["last_processed_id"] = reset_to
            row["status"] = "idle"
            return {"service": service}
        raise AssertionError(f"unexpected query: {sql}")


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class _CheckpointConn:
    def __init__(self, cursor: str | None):
        self.cursor = cursor

    async def fetchrow(self, sql, *args):
        if sql.lstrip().startswith("INSERT INTO service_cursors") and "IS NOT DISTINCT FROM $4" in sql:
            _service, next_cursor, _stamp, expected = args
            if self.cursor == expected:
                self.cursor = next_cursor
                return {"last_processed_id": self.cursor}
            return None
        if sql.lstrip().startswith("INSERT INTO service_cursors"):
            if "WHERE service_cursors.last_processed_id IS NOT DISTINCT FROM $5" in sql:
                service, next_cursor, _stamp, _status, expected = args
                if self.cursor is None and expected is None:
                    self.cursor = next_cursor
                    return {"last_processed_id": self.cursor}
                if self.cursor == expected:
                    self.cursor = next_cursor
                    return {"last_processed_id": self.cursor}
                return None
            self.cursor = self.cursor
            return {"last_processed_id": self.cursor, "last_processed_at": None, "status": "idle"}
        if sql.lstrip().startswith("SELECT last_processed_id"):
            return {"last_processed_id": self.cursor} if self.cursor is not None else None
        raise AssertionError(f"unexpected query: {sql}")


def test_manifest_requires_exact_targets_bounds_work_and_owner_approval():
    manifest = _approved_manifest()
    validate_replay_manifest(manifest)

    count_only = _approved_manifest()
    count_only["sources"][0]["target_keys"] = []
    with pytest.raises(ReplayManifestError, match="target_keys"):
        validate_replay_manifest(count_only)

    unbounded = _approved_manifest()
    unbounded["sources"][0]["target_keys"] = ["*"]
    with pytest.raises(ReplayManifestError, match="exact list"):
        validate_replay_manifest(unbounded)

    unapproved = _approved_manifest()
    unapproved["approval"]["status"] = "pending"
    with pytest.raises(ReplayManifestError, match="not preapproved"):
        validate_replay_manifest(unapproved)


def test_reset_writes_private_before_image_and_is_idempotent(tmp_path):
    tmp_path.chmod(0o700)
    before = tmp_path / "before.json"
    conn = _CursorConn({
        "telegram:chat:1001": {
            "service": "telegram:chat:1001",
            "last_processed_id": "100",
            "last_processed_at": None,
            "status": "idle",
        },
        "telegram:chat:2002": {
            "service": "telegram:chat:2002",
            "last_processed_id": "900",
            "last_processed_at": None,
            "status": "idle",
        },
        "telegram": {
            "service": "telegram", "last_processed_id": None,
            "last_processed_at": None, "status": "idle",
        },
    })
    manifest = _approved_manifest()

    dry = asyncio.run(reset_source_cursors(
        conn, manifest, "telegram", account_identity="account:tg-01",
        workers_quiesced=True, queue_drained=True, leases_invalidated=True,
        before_image_path=before,
    ))
    assert dry["mode"] == "dry-run"
    assert conn.cursors["telegram:chat:1001"]["last_processed_id"] == "100"
    assert stat_mode(before) == 0o600
    image = json.loads(before.read_text())
    assert image["cursor_rows"][0]["last_processed_id"] == "100"

    applied = asyncio.run(reset_source_cursors(
        conn, manifest, "telegram", account_identity="account:tg-01",
        workers_quiesced=True, queue_drained=True, leases_invalidated=True,
        before_image_path=before, apply=True,
    ))
    assert applied["cursor_rows_reset"] == 1
    assert conn.cursors["telegram:chat:1001"]["last_processed_id"] is None
    assert conn.cursors["telegram:chat:2002"]["last_processed_id"] == "900"

    repeated = asyncio.run(reset_source_cursors(
        conn, manifest, "telegram", account_identity="account:tg-01",
        workers_quiesced=True, queue_drained=True, leases_invalidated=True,
        before_image_path=before, apply=True,
    ))
    assert repeated["cursor_rows_reset"] == 0


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def test_reset_rejects_unlisted_source_live_worker_and_missing_before_image(tmp_path):
    tmp_path.chmod(0o700)
    manifest = _approved_manifest()
    conn = _CursorConn({
        "telegram:chat:1001": {
            "service": "telegram:chat:1001", "last_processed_id": "100",
            "last_processed_at": None, "status": "idle",
        },
        "telegram": {
            "service": "telegram", "last_processed_id": None,
            "last_processed_at": None, "status": "idle",
        },
    })
    kwargs = dict(
        account_identity="account:tg-01", workers_quiesced=True,
        queue_drained=True, leases_invalidated=True,
        before_image_path=tmp_path / "before.json", apply=True,
    )
    with pytest.raises(ReplayManifestError, match="not listed"):
        asyncio.run(reset_source_cursors(conn, manifest, "whatsapp.messages", **kwargs))
    with pytest.raises(ReplayPreconditionError, match="before-image created by dry-run"):
        asyncio.run(reset_source_cursors(conn, manifest, "telegram", **kwargs))

    conn.cursors["telegram"]["status"] = "running"
    with pytest.raises(ReplayPreconditionError, match="source worker is still running"):
        asyncio.run(reset_source_cursors(
            conn, manifest, "telegram", **{**kwargs, "apply": False},
        ))


def test_reset_before_images_and_cas_resets_page_and_queue_state(tmp_path):
    tmp_path.chmod(0o700)
    manifest = _approved_manifest()
    scope = manifest["sources"][0]
    scope["source_identity"] = "search"
    scope["account_identity"] = None
    scope["target_keys"] = ["term:alice", "queue:item-1"]
    scope["worker_service_keys"] = ["search"]
    scope["cursor_changes"] = [{
        "service": "search", "expected_last_processed_id": "query-40", "reset_to": "query-10",
    }]
    scope["page_state_changes"] = [{
        "term": "term:alice", "expected_template_cursor": 4, "reset_to": 0,
    }]
    scope["queue_state_changes"] = [{
        "id": 17, "source": "search", "entity_id": "term:alice", "content_id": "queue:item-1",
        "expected_status": "failed", "expected_retry_count": 2,
        "reset_status": "pending", "reset_retry_count": 0,
    }]
    conn = _StateConn()
    before = tmp_path / "before-all-state.json"
    common = dict(
        account_identity=None, workers_quiesced=True, queue_drained=True,
        leases_invalidated=True, before_image_path=before,
    )

    asyncio.run(reset_source_cursors(conn, manifest, "search", **common))
    image = json.loads(before.read_text())
    assert image["page_rows"][0]["template_cursor"] == 4
    assert image["queue_rows"][0]["retry_count"] == 2
    asyncio.run(reset_source_cursors(conn, manifest, "search", **common, apply=True))
    assert conn.cursors["search"]["last_processed_id"] == "query-10"
    assert conn.page["template_cursor"] == 0
    assert conn.queue["status"] == "pending"
    assert conn.queue["retry_count"] == 0
    repeated = asyncio.run(reset_source_cursors(conn, manifest, "search", **common, apply=True))
    assert repeated["state_rows_reset"] == 0


class _StateConn(_CursorConn):
    def __init__(self):
        super().__init__({
            "search": {
                "service": "search", "last_processed_id": "query-40",
                "last_processed_at": None, "status": "idle",
            },
        })
        self.page = {"term": "term:alice", "template_cursor": 4}
        self.queue = {
            "id": 17, "source": "search", "entity_id": "term:alice",
            "content_id": "queue:item-1", "status": "failed", "retry_count": 2,
            "next_retry_at": None,
        }

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if "FROM search_dynamic_terms" in sql and sql.lstrip().startswith("SELECT"):
            return dict(self.page) if self.page["term"] == args[0] else None
        if "FROM dead_letter_queue" in sql and sql.lstrip().startswith("SELECT"):
            row = self.queue
            if (row["id"], row["source"], row["entity_id"], row["content_id"]) == args:
                return dict(row)
            return None
        if "UPDATE search_dynamic_terms" in sql:
            term, reset_to, expected = args
            if self.page["term"] == term and self.page["template_cursor"] == expected:
                self.page["template_cursor"] = reset_to
                return {"term": term}
            return None
        if "UPDATE dead_letter_queue" in sql:
            row_id, source, entity_id, content_id, status, retry, expected_status, expected_retry = args
            if (self.queue["id"], self.queue["source"], self.queue["entity_id"], self.queue["content_id"], self.queue["status"], self.queue["retry_count"]) == (row_id, source, entity_id, content_id, expected_status, expected_retry):
                self.queue["status"] = status
                self.queue["retry_count"] = retry
                return {"id": row_id}
            return None
        return await super().fetchrow(sql, *args)


def test_delayed_service_worker_cannot_commit_cursor_after_reset():
    conn = _CheckpointConn("cursor-100")
    manager = CheckpointManager("search")
    manager.set_pool(_Pool(conn))
    asyncio.run(manager.load_progress())

    # The replay reset wins while this worker is delayed.
    conn.cursor = "cursor-20"
    with pytest.raises(CheckpointConflict, match="service cursor changed"):
        asyncio.run(manager.save_progress("cursor-110"))
    assert conn.cursor == "cursor-20"


def test_delayed_chat_worker_cannot_commit_cursor_after_reset():
    conn = _CheckpointConn("message-100")
    manager = CheckpointManager("telegram")
    manager.set_pool(_Pool(conn))
    asyncio.run(manager.load_chat_progress("1001"))

    conn.cursor = "message-20"
    with pytest.raises(CheckpointConflict, match="chat cursor changed"):
        asyncio.run(manager.save_chat_progress("1001", 110))
    assert conn.cursor == "message-20"


def test_replay_ownership_releases_global_lock_on_exit():
    class LockConn:
        def __init__(self):
            self.calls = []

        async def fetchval(self, sql, *_args):
            self.calls.append(sql)
            return True

    conn = LockConn()

    async def _exercise():
        async with replay_ownership(conn):
            assert len(conn.calls) == 1

    asyncio.run(_exercise())
    assert "pg_advisory_lock" in conn.calls[0]
    assert "pg_advisory_unlock" in conn.calls[1]


def test_readback_is_limited_to_manifest_cursor_keys():
    class ReadConn:
        async def fetch(self, sql, *args):
            assert "service = ANY($1::text[])" in sql
            assert args[0] == ["telegram:chat:1001"]
            return [{
                "service": "telegram:chat:1001", "last_processed_id": "75",
                "last_processed_at": None, "status": "idle",
            }]

    result = asyncio.run(readback_source_cursors(
        ReadConn(), _approved_manifest(), "telegram",
        account_identity="account:tg-01",
    ))
    assert result["missing_cursor_keys"] == []
    assert result["cursor_rows"][0]["last_processed_id"] == "75"


def _reconciliation_manifest(root: Path, archive: bytes, payload: bytes) -> dict:
    import hashlib

    def digest(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    return {
        "schema_version": 1,
        "task": 12,
        "approval": {
            "status": "approved",
            "owner_identity": "legal-data-use-owner",
            "approver_identity": "legal-data-use-owner",
            "approved_at_utc": "2026-10-23T00:00:00Z",
            "approval_reference": "task-12-test-approval",
        },
        "owner_accepted_gaps": [],
        "unaccepted_gaps": [],
        "artifacts": [
            {"artifact_id": "rabbit-archive", "kind": "rabbitmq_mnesia", "path": "rabbit/archive.bin", "sha256": digest(archive)},
            {"artifact_id": "wa-payload", "kind": "rabbitmq_payload_ndjson", "path": "payload/whatsapp.ndjson", "sha256": digest(payload)},
            {"artifact_id": "redis-snapshot", "kind": "redis_snapshot", "path": "redis/dump.rdb", "sha256": digest(b"redis snapshot")},
        ],
        "sources": [{
            "source_identity": "whatsapp.messages",
            "account_identity": "account:wa-01",
            "broker_replay": {
                "archive_artifact_id": "rabbit-archive",
                "payload_artifact_id": "wa-payload",
                "queue": "unifiedcollector.messages",
                "routing_key": "whatsapp.message",
                "event_identities": ["event:message:001"],
                "maximum_work": 1,
            },
            "redis_changes": [{
                "source_identity": "whatsapp.messages",
                "key": "whatsapp:dedupe:event:001",
                "operation": "delete",
                "expected_missing": False,
                "expected_value_sha256": digest(b"seen"),
            }],
        }],
    }


class _Redis:
    def __init__(self, values: dict[str, bytes]):
        self.values = values

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, key):
        self.values.pop(key, None)

    async def set(self, key, value):
        self.values[key] = value


def test_reconciliation_applier_is_source_scoped_and_requires_verified_payload(tmp_path):
    archive = b"archived-mnesia"
    body = b'{"id":"message-001"}'
    payload = (json.dumps({
        "event_identity": "event:message:001",
        "source_identity": "whatsapp.messages",
        "queue": "unifiedcollector.messages",
        "routing_key": "whatsapp.message",
        "body_base64": base64_encode(body),
    }) + "\n").encode()
    (tmp_path / "rabbit").mkdir()
    (tmp_path / "payload").mkdir()
    (tmp_path / "redis").mkdir()
    (tmp_path / "rabbit" / "archive.bin").write_bytes(archive)
    (tmp_path / "payload" / "whatsapp.ndjson").write_bytes(payload)
    (tmp_path / "redis" / "dump.rdb").write_bytes(b"redis snapshot")
    manifest = _reconciliation_manifest(tmp_path, archive, payload)
    validate_reconciliation_manifest(manifest)
    redis = _Redis({"whatsapp:dedupe:event:001": b"seen"})
    published = []

    async def publish(queue, routing_key, event_body, event_identity):
        published.append((queue, routing_key, event_body, event_identity))

    dry = asyncio.run(apply_source_reconciliation(
        manifest, "whatsapp.messages", account_identity="account:wa-01",
        artifact_root=tmp_path, mode="dry-run", redis_client=redis,
    ))
    assert dry["events_planned"] == 1
    assert dry["events_published"] == 0
    assert redis.values["whatsapp:dedupe:event:001"] == b"seen"

    applied = asyncio.run(apply_source_reconciliation(
        manifest, "whatsapp.messages", account_identity="account:wa-01",
        artifact_root=tmp_path, mode="apply", redis_client=redis, broker_publisher=publish,
    ))
    assert applied["events_published"] == 1
    assert published == [("unifiedcollector.messages", "whatsapp.message", body, "event:message:001")]
    assert "whatsapp:dedupe:event:001" not in redis.values

    readback = asyncio.run(apply_source_reconciliation(
        manifest, "whatsapp.messages", account_identity="account:wa-01",
        artifact_root=tmp_path, mode="readback", redis_client=redis,
    ))
    assert readback["redis_readback"] == [{
        "key": "whatsapp:dedupe:event:001", "exists": False, "value_sha256": None,
    }]


def base64_encode(value: bytes) -> str:
    import base64

    return base64.b64encode(value).decode("ascii")


def test_reconciliation_applier_rejects_unlisted_source_missing_payload_and_unaccepted_gap(tmp_path):
    archive = b"archived-mnesia"
    payload = (json.dumps({
        "event_identity": "event:message:001", "source_identity": "whatsapp.messages",
        "queue": "unifiedcollector.messages", "routing_key": "whatsapp.message",
        "body_base64": base64_encode(b"payload"),
    }) + "\n").encode()
    (tmp_path / "rabbit").mkdir()
    (tmp_path / "payload").mkdir()
    (tmp_path / "redis").mkdir()
    (tmp_path / "rabbit" / "archive.bin").write_bytes(archive)
    (tmp_path / "payload" / "whatsapp.ndjson").write_bytes(payload)
    (tmp_path / "redis" / "dump.rdb").write_bytes(b"redis snapshot")
    manifest = _reconciliation_manifest(tmp_path, archive, payload)
    with pytest.raises(ReconciliationManifestError, match="not listed"):
        asyncio.run(apply_source_reconciliation(
            manifest, "whatsapp.contacts", account_identity="account:wa-01",
            artifact_root=tmp_path, mode="dry-run",
        ))

    missing = _reconciliation_manifest(tmp_path, archive, payload)
    missing["artifacts"][1]["path"] = "payload/missing.ndjson"
    with pytest.raises(ReconciliationManifestError, match="unavailable"):
        asyncio.run(apply_source_reconciliation(
            missing, "whatsapp.messages", account_identity="account:wa-01",
            artifact_root=tmp_path, mode="dry-run",
        ))

    unaccepted = _reconciliation_manifest(tmp_path, archive, payload)
    unaccepted["unaccepted_gaps"] = ["unknown broker payload"]
    with pytest.raises(ReconciliationManifestError, match="unaccepted"):
        validate_reconciliation_manifest(unaccepted)
