from __future__ import annotations

import asyncio
import json
import os
from typing import Any

DEFAULT_MODULES = ("sfp_dnsresolve", "sfp_whois", "sfp_names")
SUPPORTED_TYPES = {"domain", "ip", "email", "username", "url", "phone"}


def allowed_modules(raw: str | None = None) -> list[str]:
    modules = [item.strip() for item in (raw or os.getenv("SPIDERFOOT_MODULES") or "").split(",") if item.strip()]
    return modules or list(DEFAULT_MODULES)


def target_in_scope(target_value: str, allowlist: str | None = None) -> bool:
    raw = allowlist if allowlist is not None else os.getenv("RECON_ALLOWLIST", "")
    allowed = [item.strip().lower() for item in raw.split(",") if item.strip()]
    if not allowed:
        return True
    value = (target_value or "").lower()
    return any(value == item or value.endswith("." + item) or item in value for item in allowed)


def normalize_observation(target_id: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    module = raw.get("module") or raw.get("moduleName") or raw.get("source")
    observation_type = raw.get("type") or raw.get("eventType") or raw.get("dataType")
    value = raw.get("value") or raw.get("data") or raw.get("content")
    if not module or not observation_type or value in {None, ""}:
        return None
    confidence = raw.get("confidence", 0.25)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.25
    return {
        "target_id": target_id,
        "module": str(module),
        "observation_type": str(observation_type),
        "value": str(value),
        "confidence": max(0.0, min(confidence, 1.0)),
        "raw_json": raw,
    }


async def _claim_target(conn) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        """
        UPDATE recon_targets
        SET status = 'in_progress', updated_at = NOW()
        WHERE id = (
            SELECT id
            FROM recon_targets
            WHERE status = 'pending'
              AND target_type = ANY($1::text[])
            ORDER BY priority, created_at
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id::text, target_type, target_value, source, priority, scope_json
        """,
        sorted(SUPPORTED_TYPES),
    )
    return dict(row) if row else None


async def _store_observations(conn, observations: list[dict[str, Any]]) -> int:
    if not observations:
        return 0
    await conn.executemany(
        """
        INSERT INTO recon_observations (
            target_id, module, observation_type, value, confidence, raw_json,
            first_seen_at, last_seen_at
        )
        VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb, NOW(), NOW())
        ON CONFLICT (target_id, module, observation_type, value) DO UPDATE SET
            confidence = GREATEST(recon_observations.confidence, EXCLUDED.confidence),
            raw_json = EXCLUDED.raw_json,
            last_seen_at = NOW()
        """,
        [
            (
                row["target_id"],
                row["module"],
                row["observation_type"],
                row["value"],
                row["confidence"],
                json.dumps(row["raw_json"]),
            )
            for row in observations
        ],
    )
    return len(observations)


async def _run_spiderfoot_cli(target: dict[str, Any], modules: list[str], timeout_seconds: int) -> list[dict[str, Any]]:
    cli = os.getenv("SPIDERFOOT_CLI")
    if not cli:
        raise RuntimeError("SPIDERFOOT_CLI is not configured")
    proc = await asyncio.create_subprocess_exec(
        cli,
        "-s",
        target["target_value"],
        "-m",
        ",".join(modules),
        "-o",
        "json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(f"SpiderFoot timed out after {timeout_seconds}s")
    if proc.returncode != 0:
        raise RuntimeError((stderr or stdout).decode("utf-8", errors="replace")[:500])
    payload = json.loads(stdout.decode("utf-8"))
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        data = payload.get("data") or payload.get("events") or []
        return [item for item in data if isinstance(item, dict)]
    return []


async def run_spiderfoot_once(conn, *, dry_run: bool = False) -> dict[str, Any]:
    async with conn.transaction():
        target = await _claim_target(conn)
    if not target:
        return {"status": "idle", "target": None, "observations": 0, "dry_run": dry_run}
    if not target_in_scope(target["target_value"]):
        await conn.execute(
            "UPDATE recon_targets SET status = 'blocked', error = $2, updated_at = NOW() WHERE id = $1::uuid",
            target["id"],
            "target outside RECON_ALLOWLIST",
        )
        return {"status": "blocked", "target": target, "observations": 0, "dry_run": dry_run}

    modules = allowed_modules()
    if dry_run:
        await conn.execute("UPDATE recon_targets SET status = 'pending', updated_at = NOW() WHERE id = $1::uuid", target["id"])
        return {"status": "dry_run", "target": target, "modules": modules, "observations": 0, "dry_run": True}

    try:
        raw_rows = await _run_spiderfoot_cli(
            target,
            modules,
            int(os.getenv("SPIDERFOOT_TARGET_TIMEOUT_SECONDS", "300")),
        )
        observations = [
            obs for raw in raw_rows
            if (obs := normalize_observation(target["id"], raw)) is not None
        ]
        written = await _store_observations(conn, observations)
        await conn.execute(
            "UPDATE recon_targets SET status = 'completed', error = NULL, updated_at = NOW() WHERE id = $1::uuid",
            target["id"],
        )
        return {"status": "completed", "target": target, "modules": modules, "observations": written, "dry_run": False}
    except Exception as exc:  # noqa: BLE001
        await conn.execute(
            "UPDATE recon_targets SET status = 'failed', error = $2, updated_at = NOW() WHERE id = $1::uuid",
            target["id"],
            str(exc)[:500],
        )
        return {"status": "failed", "target": target, "modules": modules, "observations": 0, "error": str(exc)[:500], "dry_run": False}
