from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
from typing import Any
from urllib.parse import urlparse

DEFAULT_MODULES = ("sfp_dnsresolve", "sfp_whois", "sfp_names")
SOURCE_HEALTH_NAME = "spiderfoot"
SUPPORTED_TYPES = {"domain", "ip", "email", "username", "url", "phone"}
DEFAULT_INTRUSIVE_MODULES = {
    "sfp_portscan_tcp",
    "sfp_portscan_udp",
    "sfp_dnsbrute",
    "sfp_shodan",
    "sfp_censys",
}


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def allowed_modules(raw: str | None = None) -> list[str]:
    modules = [item.strip() for item in (raw or os.getenv("SPIDERFOOT_MODULES") or "").split(",") if item.strip()]
    modules = modules or list(DEFAULT_MODULES)
    allow_intrusive = os.getenv("SPIDERFOOT_ALLOW_INTRUSIVE", "").strip().lower() in {"1", "true", "yes"}
    blocked = {
        item.strip()
        for item in os.getenv("SPIDERFOOT_BLOCKED_MODULES", ",".join(sorted(DEFAULT_INTRUSIVE_MODULES))).split(",")
        if item.strip()
    }
    if not allow_intrusive:
        modules = [module for module in modules if module not in blocked]
    try:
        max_modules = max(1, int(os.getenv("SPIDERFOOT_MAX_MODULES", "10")))
    except ValueError:
        max_modules = 10
    return modules[:max_modules]


def target_in_scope(target_value: str, allowlist: str | None = None) -> bool:
    raw = allowlist if allowlist is not None else os.getenv("RECON_ALLOWLIST", "")
    allowed = [item.strip().lower() for item in raw.split(",") if item.strip()]
    if not allowed:
        return os.getenv("RECON_ALLOW_UNSCOPED", "").strip().lower() in {"1", "true", "yes"}
    value = (target_value or "").lower()
    parsed = urlparse(value if "://" in value else f"//{value}")
    host = (parsed.hostname or value.split("@")[-1]).strip(".")
    candidates = {value, host}
    if "@" in value:
        candidates.add(value.split("@", 1)[1])
    return any(candidate == item or candidate.endswith("." + item) for candidate in candidates for item in allowed)


def stale_target_minutes() -> int:
    try:
        return max(5, int(os.getenv("SPIDERFOOT_STALE_TARGET_MINUTES", "120")))
    except ValueError:
        return 120


async def _mark_source_health(conn, status: str, error: str | None = None, *, success: bool = False) -> None:
    await conn.execute(
        """
        INSERT INTO source_health (source, status, last_success_at, last_error, updated_at)
        VALUES ($1, $2, CASE WHEN $4 THEN NOW() ELSE NULL END, $3, NOW())
        ON CONFLICT (source) DO UPDATE SET
            status = EXCLUDED.status,
            last_success_at = CASE
                WHEN $4 THEN NOW()
                ELSE source_health.last_success_at
            END,
            last_error = EXCLUDED.last_error,
            updated_at = NOW()
        """,
        SOURCE_HEALTH_NAME,
        status,
        error,
        success,
    )


async def _reclaim_stale_targets(conn) -> None:
    await conn.execute(
        """
        UPDATE recon_targets
        SET status = 'pending',
            error = 'stale SpiderFoot run reclaimed',
            updated_at = NOW()
        WHERE status = 'in_progress'
          AND updated_at < NOW() - ($1::int * INTERVAL '1 minute')
        """,
        stale_target_minutes(),
    )


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
    command = shlex.split(cli)
    if not command:
        raise RuntimeError("SPIDERFOOT_CLI is not configured")
    executable = command[0]
    if shutil.which(executable) is None and not os.path.exists(executable):
        raise RuntimeError(f"SPIDERFOOT_CLI executable not found: {executable}")
    proc = await asyncio.create_subprocess_exec(
        *command,
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
    return normalize_spiderfoot_payload(json.loads(stdout.decode("utf-8")))


def normalize_spiderfoot_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        data = payload.get("data") or payload.get("events") or []
        return [item for item in data if isinstance(item, dict)]
    return []


async def run_spiderfoot_once(conn, *, dry_run: bool = False) -> dict[str, Any]:
    async with conn.transaction():
        await _reclaim_stale_targets(conn)
        target = await _claim_target(conn)
    if not target:
        await _mark_source_health(conn, "running", None, success=True)
        return {"status": "idle", "target": None, "observations": 0, "dry_run": dry_run}
    scope = _json_dict(target.get("scope_json"))
    scope_allowlist = scope.get("allowlist")
    if isinstance(scope_allowlist, list):
        scope_allowlist = ",".join(str(item) for item in scope_allowlist)
    if not target_in_scope(target["target_value"], scope_allowlist):
        await conn.execute(
            "UPDATE recon_targets SET status = 'blocked', error = $2, updated_at = NOW() WHERE id = $1::uuid",
            target["id"],
            "target outside RECON_ALLOWLIST",
        )
        await _mark_source_health(conn, "running", None, success=True)
        return {"status": "blocked", "target": target, "observations": 0, "dry_run": dry_run}

    module_override = scope.get("modules")
    if isinstance(module_override, list):
        module_override = ",".join(str(item) for item in module_override)
    modules = allowed_modules(module_override if isinstance(module_override, str) else None)
    if not modules:
        await conn.execute(
            "UPDATE recon_targets SET status = 'blocked', error = $2, updated_at = NOW() WHERE id = $1::uuid",
            target["id"],
            "no SpiderFoot modules allowed by scope",
        )
        await _mark_source_health(conn, "running", None, success=True)
        return {"status": "blocked", "target": target, "observations": 0, "dry_run": dry_run}
    if dry_run:
        await conn.execute("UPDATE recon_targets SET status = 'pending', updated_at = NOW() WHERE id = $1::uuid", target["id"])
        await _mark_source_health(conn, "running", None, success=True)
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
        await _mark_source_health(conn, "running", None, success=True)
        return {"status": "completed", "target": target, "modules": modules, "observations": written, "dry_run": False}
    except Exception as exc:  # noqa: BLE001
        await conn.execute(
            "UPDATE recon_targets SET status = 'failed', error = $2, updated_at = NOW() WHERE id = $1::uuid",
            target["id"],
            str(exc)[:500],
        )
        await _mark_source_health(conn, "degraded", str(exc)[:500])
        return {"status": "failed", "target": target, "modules": modules, "observations": 0, "error": str(exc)[:500], "dry_run": False}
