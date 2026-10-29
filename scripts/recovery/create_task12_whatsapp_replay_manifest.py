"""Create the approved Task 12 manifest from a private payload extraction."""

import argparse
import base64
import collections
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.core.checkpoint_replay import validate_reconciliation_manifest


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope-proposal", type=Path, required=True)
    parser.add_argument("--payload", type=Path, action="append", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    proposal = json.loads(args.scope_proposal.read_text(encoding="utf-8"))
    scopes = proposal.get("candidate_scopes")
    if proposal.get("task") != 51 or not isinstance(scopes, list) or len(scopes) != 1:
        raise ValueError("expected exactly one approved Task 51 scope proposal")
    scope = scopes[0]
    account = scope.get("account_identity")
    target_keys = scope.get("target_keys")
    if not isinstance(account, str) or not isinstance(target_keys, list) or not target_keys:
        raise ValueError("scope proposal is missing its exact account or target keys")
    expected_ids = set(target_keys)
    if len(expected_ids) != len(target_keys) or not all(isinstance(value, str) and value for value in expected_ids):
        raise ValueError("scope target keys must be exact and unique")

    events_by_source: dict[str, list[str]] = collections.defaultdict(list)
    message_ids_by_source: dict[str, set[str]] = collections.defaultdict(set)
    routing_by_source: dict[str, str] = {}
    payload_by_source: dict[str, Path] = {}
    seen_events: set[str] = set()
    for payload_path in args.payload:
        for line in payload_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            encoded = row.get("body_base64")
            source = row.get("source_identity")
            routing_key = row.get("routing_key")
            if (
            not isinstance(source, str)
            or not source.startswith("whatsapp.messages.route.")
            or row.get("queue") != "unifiedcollector.messages"
            or not isinstance(routing_key, str)
            or not routing_key
            or not isinstance(encoded, str)
            ):
                raise ValueError("payload record is outside the approved WhatsApp broker scope")
            body = base64.b64decode(encoded, validate=True)
            body_hash = hashlib.sha256(body).hexdigest()
            event_identity = row.get("event_identity")
            if not isinstance(event_identity, str) or not event_identity.startswith(f"{source}:{body_hash}:"):
                raise ValueError("payload event identity is not bound to its body")
            decoded = json.loads(body)
            message_id = decoded.get("message_id") if isinstance(decoded, dict) else None
            if not isinstance(decoded, dict) or decoded.get("session_name") != account or message_id not in expected_ids:
                raise ValueError("payload event is outside the approved account/message cohort")
            if source in routing_by_source and routing_by_source[source] != routing_key:
                raise ValueError("a route-scoped payload source has mixed routing keys")
            if event_identity in seen_events:
                raise ValueError("payload includes a duplicate queue event identity")
            routing_by_source[source] = routing_key
            if source in payload_by_source and payload_by_source[source] != payload_path:
                raise ValueError("each route scope must have one private payload artifact")
            payload_by_source[source] = payload_path
            events_by_source[source].append(event_identity)
            message_ids_by_source[source].add(message_id)
            seen_events.add(event_identity)
    if set().union(*message_ids_by_source.values()) != expected_ids:
        raise ValueError("payload does not exactly cover the approved message IDs")

    archive = args.artifact_root / "rabbitmq" / "SHA256SUMS.rabbitmq"
    if not archive.is_file() or any(not path.is_file() for path in args.payload):
        raise ValueError("approved artifact root is incomplete or does not contain the extracted payload")

    approved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    manifest = {
        "schema_version": 1,
        "task": 12,
        "manifest_status": "activation_approved_scoped_whatsapp_broker_replay",
        "activation_eligible": True,
        "approval": {
            "status": "approved",
            "owner_identity": "legal-data-use-owner",
            "approver_identity": "legal-data-use-owner",
            "approved_at_utc": approved_at,
            "approval_reference": "owner explicit approval in recovery session 2026-10-29",
            "scope": "Only the approved WhatsApp account and its exact 1,369 message IDs (1,433 preserved queue events, including timestamp-distinct revisions); no other broker queue/account and no Redis mutation.",
        },
        "owner_accepted_gaps": [
            {
                "gap_id": "whatsapp-approved-broker-replay",
                "source_identity": "whatsapp.messages",
                "status": "accepted",
                "owner_identity": "legal-data-use-owner",
                "disposition": "republish only the extracted exact WhatsApp message cohort",
            },
            {
                "gap_id": "redis-preserved-without-mutation",
                "source_identity": "redis",
                "status": "accepted",
                "owner_identity": "legal-data-use-owner",
                "disposition": "preserve without replay or mutation; excluded from this approval",
            },
        ],
        "unaccepted_gaps": [],
        "artifacts": [
            {"artifact_id": "rabbitmq-preservation-manifest", "kind": "rabbitmq_mnesia", "path": "rabbitmq/SHA256SUMS.rabbitmq", "sha256": digest(archive)},
            *[{"artifact_id": f"approved-whatsapp-payload-{source.rsplit('.', 1)[-1]}", "kind": "rabbitmq_payload_ndjson", "path": str(payload_by_source[source].resolve().relative_to(args.artifact_root.resolve())), "sha256": digest(payload_by_source[source])} for source in sorted(events_by_source)],
        ],
        "sources": [{
            "source_identity": source,
            "account_identity": account,
            "broker_replay": {
                "archive_artifact_id": "rabbitmq-preservation-manifest",
                "payload_artifact_id": f"approved-whatsapp-payload-{source.rsplit('.', 1)[-1]}",
                "queue": "unifiedcollector.messages",
                "routing_key": routing_by_source[source],
                "event_identities": events_by_source[source],
                "maximum_work": len(events_by_source[source]),
            },
            "replay_scope": {
                "target_keys": sorted(message_ids_by_source[source]),
                "approved_queue_event_count": len(events_by_source[source]),
                "historical_bounds_observed": scope.get("historical_bounds_observed"),
                "excluded_state": [value for value in scope.get("excluded_state", []) if value != "message payload bodies"],
            },
            "redis_changes": [],
        } for source in sorted(events_by_source)],
    }
    validate_reconciliation_manifest(manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, args.output)
    print(json.dumps({"event_count": len(seen_events), "route_scope_count": len(events_by_source), "status": "validated"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
