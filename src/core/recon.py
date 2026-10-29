from __future__ import annotations

import json
import re
from typing import Any

ALLOWED_TARGET_TYPES = {"domain", "ip", "ipv4", "email", "username", "user", "channel", "url", "phone"}
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_recon_target(target_type: str, target_value: str) -> tuple[str, str]:
    ttype = (target_type or "").strip().lower()
    value = (target_value or "").strip()
    aliases = {
        "ipv4": "ip",
        "user": "username",
        "channel": "username",
    }
    ttype = aliases.get(ttype, ttype)
    if ttype not in ALLOWED_TARGET_TYPES:
        raise ValueError(f"unsupported recon target type: {target_type}")
    if ttype in {"domain", "email"}:
        value = value.lower()
    if ttype == "domain" and not _DOMAIN_RE.match(value):
        raise ValueError("invalid domain target")
    if ttype == "email" and not _EMAIL_RE.match(value):
        raise ValueError("invalid email target")
    if not value:
        raise ValueError("empty recon target")
    return ttype, value


async def queue_recon_target(
    conn,
    *,
    target_type: str,
    target_value: str,
    source: str = "manual",
    priority: int = 5,
    scope: dict[str, Any] | None = None,
    reset_existing: bool = True,
) -> dict[str, Any]:
    ttype, value = normalize_recon_target(target_type, target_value)
    row = await conn.fetchrow(
        """
        INSERT INTO recon_targets (target_type, target_value, source, priority, scope_json, updated_at)
        VALUES ($1, $2, $3, $4, $5::jsonb, NOW())
        ON CONFLICT (target_type, target_value) DO UPDATE SET
            priority = LEAST(recon_targets.priority, EXCLUDED.priority),
            source = EXCLUDED.source,
            scope_json = recon_targets.scope_json || EXCLUDED.scope_json,
            status = CASE
                WHEN NOT $6::boolean THEN recon_targets.status
                WHEN recon_targets.status = 'in_progress' THEN recon_targets.status
                ELSE 'pending'
            END,
            error = CASE
                WHEN NOT $6::boolean OR recon_targets.status = 'in_progress' THEN recon_targets.error
                ELSE NULL
            END,
            updated_at = CASE WHEN $6::boolean THEN NOW() ELSE recon_targets.updated_at END
        RETURNING id::text, target_type, target_value, source, priority, status,
                  (xmax = 0) AS inserted
        """,
        ttype,
        value,
        source,
        priority,
        json.dumps(scope or {}),
        reset_existing,
    )
    return dict(row)
