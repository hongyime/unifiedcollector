"""Store bounded discovery evidence and optional shared spider queue targets."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from src.core.spider_graph_store import shared_spider_enabled


_PLATFORM_RE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")


def canonical_discovery_node(target_type: str, target_id: str) -> str:
    """Namespace node IDs so unlike Telegram targets cannot collide."""
    kind = str(target_type or "").strip().lower()
    value = str(target_id or "").strip()
    if not kind or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", kind):
        raise ValueError("target_type must be a short lowercase identifier")
    if not value:
        raise ValueError("target_id must not be empty")
    node_id = f"{kind}:{value}"
    if len(node_id) > 500:
        raise ValueError("canonical target node exceeds 500 characters")
    return node_id


async def persist_discovery_target(
    conn,
    platform: str,
    target_id: str,
    *,
    target_type: str,
    discovery_type: str,
    source_table: str,
    source_record_id: str,
    confidence: float,
    priority: int = 5,
    source_metadata: dict[str, Any] | None = None,
    enqueue: bool = True,
    max_pending: int = 500,
) -> bool:
    """Persist one evidence row and, when requested, idempotently seed a node.

    Callers must pass stable source records. The evidence key includes the
    source, target and discovery kind, so distinct observations remain
    auditable even though ``spider_queue`` has one row per canonical node.
    """
    platform = str(platform or "").strip().lower()
    discovery_type = str(discovery_type or "").strip().lower()
    source_table = str(source_table or "").strip()
    source_record_id = str(source_record_id or "").strip()
    target_type = str(target_type or "").strip().lower()
    node_id = canonical_discovery_node(target_type, target_id)
    if not shared_spider_enabled(platform):
        return False
    if not _PLATFORM_RE.fullmatch(platform):
        raise ValueError("platform must be a lowercase identifier")
    if not discovery_type or len(discovery_type) > 64:
        raise ValueError("discovery_type must contain 1 to 64 characters")
    if not source_table or len(source_table) > 128:
        raise ValueError("source_table must contain 1 to 128 characters")
    if not source_record_id or len(source_record_id) > 500:
        raise ValueError("source_record_id must contain 1 to 500 characters")
    if target_type not in {"account", "channel", "chat", "member", "link"}:
        raise ValueError("unsupported discovery target type")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    metadata = dict(source_metadata or {})
    metadata["target_type"] = target_type
    metadata_json = json.dumps(metadata, default=str)
    evidence_key = hashlib.sha256("\0".join((
        platform, node_id, discovery_type, source_table, source_record_id,
    )).encode("utf-8")).hexdigest()

    await conn.execute(
        """
        INSERT INTO spider_discovery_evidence (
            evidence_key, platform, target_type, target_node_id,
            discovery_type, source_table, source_record_id, confidence,
            source_metadata, first_seen_at, last_seen_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, NOW(), NOW())
        ON CONFLICT (evidence_key) DO UPDATE SET
            confidence = GREATEST(spider_discovery_evidence.confidence, EXCLUDED.confidence),
            source_metadata = spider_discovery_evidence.source_metadata || EXCLUDED.source_metadata,
            last_seen_at = NOW()
        """,
        evidence_key,
        platform,
        target_type,
        node_id,
        discovery_type,
        source_table,
        source_record_id,
        confidence,
        metadata_json,
    )
    if enqueue:
        await conn.execute(
            """
            INSERT INTO spider_queue (
                platform, node_id, hop_distance, priority, status,
                discovery_type, source_table, source_record_id,
                source_metadata, idempotency_key, max_attempts, enqueued_at
            )
            SELECT $1::VARCHAR(50), $2::VARCHAR(500), 0, $3::INTEGER,
                   'pending', $4::VARCHAR(64), $5::VARCHAR(128),
                   $6::VARCHAR(500), $7::jsonb, $8::VARCHAR(600), 5, NOW()
             WHERE (
                SELECT COUNT(*) FROM spider_queue
                 WHERE platform = $1::VARCHAR(50)
                   AND status IN ('pending', 'in_progress')
             ) < $9
            ON CONFLICT (platform, idempotency_key) DO UPDATE SET
                priority = LEAST(spider_queue.priority, EXCLUDED.priority),
                source_metadata = spider_queue.source_metadata || EXCLUDED.source_metadata
            """,
            platform,
            node_id,
            max(1, min(100, int(priority))),
            discovery_type,
            source_table,
            source_record_id,
            metadata_json,
            f"{platform}:{node_id}",
            max(1, min(10_000, int(max_pending))),
        )
    return True


__all__ = ["canonical_discovery_node", "persist_discovery_target"]
