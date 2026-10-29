"""Extract only an approved WhatsApp payload cohort from an isolated clone."""

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote

import aio_pika


def load_scope(proposal_path: Path, metadata_path: Path) -> tuple[str, set[str], dict[int, tuple[str, str]]]:
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    scopes = proposal.get("candidate_scopes")
    if proposal.get("task") != 51 or not isinstance(scopes, list) or len(scopes) != 1:
        raise ValueError("expected one task-51 proposed scope")
    scope = scopes[0]
    account = scope.get("account_identity")
    target_keys = scope.get("target_keys")
    if not isinstance(account, str) or not account or not isinstance(target_keys, list):
        raise ValueError("scope account or target keys are invalid")
    target_ids = set(target_keys)
    if not target_ids or len(target_ids) != len(target_keys) or not all(isinstance(value, str) and value for value in target_ids):
        raise ValueError("scope target keys must be unique non-empty strings")

    expected: dict[int, tuple[str, str]] = {}
    with metadata_path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            identifiers = row.get("identifiers") or {}
            message_id = identifiers.get("message_id")
            if (
                row.get("queue") == "unifiedcollector.messages"
                and identifiers.get("session_name") == account
                and message_id in target_ids
                and isinstance(row.get("body_sha256"), str)
            ):
                ordinal = row.get("ordinal")
                if not isinstance(ordinal, int) or ordinal in expected:
                    raise ValueError("identity inventory has an invalid queue ordinal")
                expected[ordinal] = (message_id, row["body_sha256"])
    if {message_id for message_id, _ in expected.values()} != target_ids:
        raise ValueError("identity inventory does not cover every approved message")
    return account, target_ids, expected


async def main() -> None:
    proposal = Path(os.environ["TASK51_SCOPE_PROPOSAL"])
    metadata = Path(os.environ["TASK51_IDENTITY_METADATA"])
    output = Path(os.environ["TASK51_PAYLOAD_OUTPUT"])
    account, target_ids, expected = load_scope(proposal, metadata)
    password = quote(os.environ["UC_RECOVERY_PASSWORD"], safe="")
    connection = await aio_pika.connect(f"amqp://recovery_inventory:{password}@127.0.0.1/%2F", timeout=30)
    channel = await connection.channel(publisher_confirms=False)
    seen: set[int] = set()
    routing_keys: set[str] = set()
    try:
        queue = await channel.declare_queue("unifiedcollector.messages", passive=True)
        ordinal = 0
        with output.open("x", encoding="utf-8") as handle:
            while message := await queue.get(fail=False, no_ack=False):
                ordinal += 1
                body_hash = hashlib.sha256(message.body).hexdigest()
                try:
                    decoded = json.loads(message.body)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    decoded = None
                message_id = decoded.get("message_id") if isinstance(decoded, dict) else None
                if (
                    isinstance(decoded, dict)
                    and decoded.get("session_name") == account
                    and message_id in target_ids
                    and expected.get(ordinal) == (message_id, body_hash)
                ):
                    if ordinal in seen:
                        raise ValueError("isolated clone has a duplicate approved queue ordinal")
                    route_identity = "whatsapp.messages.route." + hashlib.sha256(
                        message.routing_key.encode("utf-8")
                    ).hexdigest()[:16]
                    handle.write(json.dumps({
                        "event_identity": f"{route_identity}:{body_hash}:{ordinal}",
                        "source_identity": route_identity,
                        "queue": "unifiedcollector.messages",
                        "routing_key": message.routing_key,
                        "body_base64": base64.b64encode(message.body).decode("ascii"),
                    }, sort_keys=True, separators=(",", ":")) + "\n")
                    seen.add(ordinal)
                    routing_keys.add(message.routing_key)
                await message.ack()
    finally:
        await connection.close()
    if seen != set(expected) or not routing_keys:
        raise ValueError("isolated clone did not yield exactly the approved queue-event cohort")
    print(json.dumps({
        "event_count": len(seen),
        "payload_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "routing_key_count": len(routing_keys),
    }, sort_keys=True))


asyncio.run(main())
