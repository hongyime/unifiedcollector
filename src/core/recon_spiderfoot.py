from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import tempfile
import shlex
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_MODULES = ("sfp_dnsresolve", "sfp_whois", "sfp_names")

# maigret: HTTP-only OSS account-enumeration engine (MIT). Replaces the DEAD
# sfp_accounts module for username targets. Routed here when a target's scope
# has `modules=['maigret']` (per-target opt-in, used by the pilot) OR when the
# RECON_USERNAME_ENGINE env is set to "maigret" globally.
MAIGRET_MODULE = "maigret"
MAIGRET_SOURCE_HEALTH_NAME = "maigret"
DEFAULT_MAIGRET_TOP_SITES = "300"
DEFAULT_MAIGRET_NUM_REQUESTS = "20"
DEFAULT_MAIGRET_HTTP_TIMEOUT_SECONDS = "10"
DEFAULT_MAIGRET_TARGET_TIMEOUT_SECONDS = "420"
SOURCE_HEALTH_NAME = "spiderfoot"
SUPPORTED_TYPES = {"domain", "ip", "ipv4", "email", "username", "user", "url", "phone"}
DEFAULT_INTRUSIVE_MODULES = {
    "sfp_portscan_tcp",
    "sfp_portscan_udp",
    "sfp_dnsbrute",
    "sfp_shodan",
    "sfp_censys",
}
DEFAULT_COLLECTOR_SOURCES = {
    "beeper",
    "facebook",
    "github",
    "instagram",
    "lemon8",
    "search",
    "strava",
    "telegram",
    "threads",
    "tiktok",
    "website",
    "whatsapp",
    "x",
    "youtube",
}
DEFAULT_COLLECTOR_TARGET_TYPES = {"domain", "ip", "ipv4", "email", "phone", "url", "user", "username"}
DEFAULT_COLLECTOR_SOURCE_TABLES = {
    "collection_targets",
    "collector_seen_targets",
    "discovered_links",
    "follow_edges",
    "github_spider_queue",
    "instagram_spider_queue",
    "social_users",
    "website_targets",
    "youtube_profile_queue",
    "youtube_spider_queue",
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


def _csv_set(name: str, default: set[str]) -> set[str]:
    raw = os.getenv(name, "")
    values = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return values or set(default)


def _target_domain(target_value: str) -> str:
    value = (target_value or "").strip().lower()
    parsed = urlparse(value if "://" in value else f"//{value}")
    if parsed.hostname:
        return parsed.hostname.strip(".")
    if "@" in value:
        return value.split("@", 1)[1].strip(".")
    return value.strip(".")


def _suffix_allowed(target_value: str, suffixes: str | None = None) -> bool:
    raw = suffixes if suffixes is not None else os.getenv("RECON_ALLOWED_DOMAIN_SUFFIXES", "")
    allowed = [item.strip().lower().strip(".") for item in raw.split(",") if item.strip()]
    if not allowed:
        return True
    host = _target_domain(target_value)
    return any(host == suffix or host.endswith("." + suffix) for suffix in allowed)


def target_allowed_by_policy(target: dict[str, Any], scope: dict[str, Any] | None = None) -> tuple[bool, str]:
    scope = scope or _json_dict(target.get("scope_json"))
    target_type = str(target.get("target_type") or "").lower()
    target_value = str(target.get("target_value") or "")
    scope_allowlist = scope.get("allowlist")
    if isinstance(scope_allowlist, list):
        scope_allowlist = ",".join(str(item) for item in scope_allowlist)
    if target_in_scope(target_value, scope_allowlist):
        return True, "target matched per-target allowlist"
    if target_in_scope(target_value):
        return True, "target matched RECON_ALLOWLIST"

    collector_derived = bool(scope.get("collector_derived")) or str(target.get("source") or "").startswith("collector:")
    if not collector_derived:
        return False, "manual target outside RECON_ALLOWLIST"

    allowed_types = _csv_set("RECON_COLLECTOR_TARGET_TYPES", DEFAULT_COLLECTOR_TARGET_TYPES)
    if target_type not in allowed_types:
        return False, f"collector-derived target type not allowed: {target_type}"

    collector_source = str(scope.get("collector_source") or scope.get("platform") or "").lower()
    allowed_sources = _csv_set("RECON_ALLOWED_SOURCES", DEFAULT_COLLECTOR_SOURCES)
    if collector_source and collector_source not in allowed_sources:
        return False, f"collector source not allowed: {collector_source}"

    source_table = str(scope.get("source_table") or "").lower()
    if not source_table and str(target.get("source") or "").startswith("collector:"):
        source_table = str(target.get("source") or "").split(":", 1)[1].lower()
    allowed_tables = _csv_set("RECON_ALLOWED_SOURCE_TABLES", DEFAULT_COLLECTOR_SOURCE_TABLES)
    if source_table and source_table not in allowed_tables:
        return False, f"collector source table not allowed: {source_table}"

    if target_type in {"domain", "url", "email"} and not _suffix_allowed(target_value):
        return False, "collector-derived target outside RECON_ALLOWED_DOMAIN_SUFFIXES"

    return True, "collector-derived target matched source/type policy"


def stale_target_minutes() -> int:
    try:
        return max(5, int(os.getenv("SPIDERFOOT_STALE_TARGET_MINUTES", "120")))
    except ValueError:
        return 120


def spiderfoot_max_threads() -> int:
    try:
        return max(1, min(int(os.getenv("SPIDERFOOT_MAX_THREADS", "4")), 20))
    except ValueError:
        return 4


def _safe_worker_label(worker_label: int | str | None) -> str:
    if worker_label is None:
        return "default"
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(worker_label))
    return (safe or "default")[:32]


def _spiderfoot_env(worker_label: int | str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    state_root = Path(os.getenv("SPIDERFOOT_STATE_ROOT", "/tmp/spiderfoot-state"))
    home = state_root / _safe_worker_label(worker_label)
    home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    return env


def _is_target_level_failure(error: str) -> bool:
    return (
        error.startswith("SpiderFoot timed out after ")
        or "Could not determine target type" in error
        or "Invalid target" in error
    )


def _extract_json_objects(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    rows: list[dict[str, Any]] = []
    position = 0
    while position < len(text):
        next_object = text.find("{", position)
        if next_object < 0:
            break
        try:
            value, end = decoder.raw_decode(text[next_object:])
        except json.JSONDecodeError:
            position = next_object + 1
            continue
        if isinstance(value, dict):
            rows.append(value)
        position = next_object + max(end, 1)
    return rows


def parse_spiderfoot_stdout(stdout: bytes) -> list[dict[str, Any]]:
    text = stdout.decode("utf-8", errors="replace")
    try:
        return normalize_spiderfoot_payload(json.loads(text))
    except json.JSONDecodeError as exc:
        rows = _extract_json_objects(text)
        if rows:
            return rows
        raise RuntimeError(f"SpiderFoot returned malformed JSON output at byte {exc.pos}") from exc


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
        SET status = 'in_progress', error = NULL, updated_at = NOW()
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
            target_id, module, observation_type, value, value_hash, confidence, raw_json,
            first_seen_at, last_seen_at
        )
        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::jsonb, NOW(), NOW())
        ON CONFLICT (target_id, module, observation_type, value_hash) DO UPDATE SET
            confidence = GREATEST(recon_observations.confidence, EXCLUDED.confidence),
            value = EXCLUDED.value,
            raw_json = EXCLUDED.raw_json,
            last_seen_at = NOW()
        """,
        [
            (
                row["target_id"],
                row["module"],
                row["observation_type"],
                row["value"],
                hashlib.sha256(str(row["value"]).encode("utf-8")).hexdigest(),
                row["confidence"],
                json.dumps(row["raw_json"]),
            )
            for row in observations
        ],
    )
    return len(observations)


def _maigret_selected(target_type: str, modules: list[str]) -> bool:
    """Return True when this target should run through maigret, not SpiderFoot."""
    if (target_type or "").lower() != "username":
        return False
    if modules == [MAIGRET_MODULE]:
        return True
    if MAIGRET_MODULE in modules and len(modules) == 1:
        return True
    engine = os.getenv("RECON_USERNAME_ENGINE", "").strip().lower()
    return engine == "maigret"


def _maigret_rows_to_observations(
    target_id: str,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize maigret site entries into recon_observations rows.

    Only entries with a Claimed status and a real profile URL are kept. The
    per-row confidence is a HTTP-only heuristic: top-500 sites -> 0.7, else 0.5.
    Both remain <=0.5 after the analyzer bridge caps for cross_platform_link.
    """
    observations: list[dict[str, Any]] = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        status_field = entry.get("status")
        if isinstance(status_field, dict):
            status_str = str(status_field.get("status") or "").strip().lower()
        else:
            status_str = str(status_field or "").strip().lower()
        # Skip everything except an explicit Claimed hit. maigret emits
        # "claimed", "not claimed", "unknown", "illegal", "available" — only
        # "claimed" is evidence of an actual account.
        if "claimed" not in status_str or "not" in status_str:
            continue
        url = (
            entry.get("url_user")
            or entry.get("url")
            or entry.get("profile_url")
            or entry.get("link")
        )
        if not url:
            continue
        rank_val = entry.get("rank") or entry.get("alexa_rank") or 0
        try:
            rank = int(rank_val)
        except (TypeError, ValueError):
            rank = 0
        confidence = 0.7 if 0 < rank <= 500 else 0.5
        observations.append(
            {
                "target_id": target_id,
                "module": MAIGRET_MODULE,
                "observation_type": "ACCOUNT_EXTERNAL_OWNED",
                "value": str(url),
                "confidence": confidence,
                "raw_json": entry,
            }
        )
    return observations


def _parse_maigret_report_dir(report_dir: Path) -> list[dict[str, Any]]:
    """Read the maigret output folder and return raw site entries."""
    rows: list[dict[str, Any]] = []
    # maigret 0.5.x writes `report_<username>_ndjson.json` when invoked with
    # `-J ndjson -fo <dir>` (one JSON object per line). Older builds used
    # `report_<username>.ndjson` — accept both to stay forward/backward safe.
    ndjson_paths = sorted(report_dir.glob("report_*_ndjson.json")) + sorted(
        report_dir.glob("report_*.ndjson")
    )
    for path in ndjson_paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    if rows:
        return rows
    # Fallback: simple JSON summary (dict keyed by sitename)
    for path in sorted(report_dir.glob("report_*_simple.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            for site_name, entry in payload.items():
                if isinstance(entry, dict):
                    entry.setdefault("sitename", site_name)
                    rows.append(entry)
        elif isinstance(payload, list):
            rows.extend(item for item in payload if isinstance(item, dict))
    return rows


async def _run_maigret_cli(
    target: dict[str, Any],
    timeout_seconds: int,
    *,
    worker_label: int | str | None = None,
) -> list[dict[str, Any]]:
    """Run maigret HTTP-only against a username target. Returns normalized obs."""
    executable = os.getenv("MAIGRET_CLI", "maigret")
    if shutil.which(executable) is None and not os.path.exists(executable):
        raise RuntimeError(f"MAIGRET_CLI executable not found: {executable}")
    top_sites = os.getenv("MAIGRET_TOP_SITES", DEFAULT_MAIGRET_TOP_SITES)
    num_requests = os.getenv("MAIGRET_NUM_REQUESTS", DEFAULT_MAIGRET_NUM_REQUESTS)
    http_timeout = os.getenv(
        "MAIGRET_HTTP_TIMEOUT_SECONDS", DEFAULT_MAIGRET_HTTP_TIMEOUT_SECONDS
    )
    keep_dir = (os.getenv("MAIGRET_REPORT_KEEPDIR", "") or "").strip()
    with tempfile.TemporaryDirectory(prefix="maigret-") as tmpdir:
        cmd = [
            executable,
            str(target["target_value"]),
            "--top-sites", str(top_sites),
            "--timeout", str(http_timeout),
            "-n", str(num_requests),
            "--no-progressbar",
            "-J", "ndjson",
            "-fo", tmpdir,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_spiderfoot_env(worker_label),
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (AttributeError, ProcessLookupError):
                proc.kill()
            await proc.communicate()
            raise RuntimeError(f"maigret timed out after {timeout_seconds}s")
        # maigret exits non-zero when zero sites match; we still parse the report
        # dir. Only a missing report dir is a real failure.
        raw_rows = _parse_maigret_report_dir(Path(tmpdir))
        if keep_dir:
            try:
                dst_root = Path(keep_dir)
                dst_root.mkdir(parents=True, exist_ok=True)
                for path in Path(tmpdir).glob("report_*"):
                    try:
                        shutil.copy2(path, dst_root / path.name)
                    except OSError:
                        pass
            except OSError:
                pass  # never let keepdir failures break the pipeline
        if not raw_rows and proc.returncode not in (0, 1):
            # No parseable output AND non-normal exit -> propagate stderr
            err_text = (stderr or stdout).decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"maigret failed (exit={proc.returncode}): {err_text}")
    return _maigret_rows_to_observations(str(target["id"]), raw_rows)



async def _run_spiderfoot_cli(
    target: dict[str, Any],
    modules: list[str],
    timeout_seconds: int,
    *,
    worker_label: int | str | None = None,
) -> list[dict[str, Any]]:
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
        "-max-threads",
        str(spiderfoot_max_threads()),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_spiderfoot_env(worker_label),
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (AttributeError, ProcessLookupError):
            proc.kill()
        await proc.communicate()
        raise RuntimeError(f"SpiderFoot timed out after {timeout_seconds}s")
    if proc.returncode != 0:
        raise RuntimeError((stderr or stdout).decode("utf-8", errors="replace")[:500])
    return parse_spiderfoot_stdout(stdout)


def normalize_spiderfoot_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        data = payload.get("data") or payload.get("events") or []
        return [item for item in data if isinstance(item, dict)]
    return []


async def run_spiderfoot_once(
    conn,
    *,
    dry_run: bool = False,
    worker_label: int | str | None = None,
) -> dict[str, Any]:
    async with conn.transaction():
        await _reclaim_stale_targets(conn)
        target = await _claim_target(conn)
    if not target:
        await _mark_source_health(conn, "running", None, success=True)
        return {"status": "idle", "target": None, "observations": 0, "dry_run": dry_run}
    scope = _json_dict(target.get("scope_json"))
    allowed, reason = target_allowed_by_policy(target, scope)
    if not allowed:
        await conn.execute(
            "UPDATE recon_targets SET status = 'blocked', error = $2, updated_at = NOW() WHERE id = $1::uuid",
            target["id"],
            reason,
        )
        await _mark_source_health(conn, "running", None, success=True)
        return {"status": "blocked", "target": target, "observations": 0, "error": reason, "dry_run": dry_run}

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

    if _maigret_selected(target["target_type"], modules):
        try:
            observations = await _run_maigret_cli(
                target,
                int(os.getenv(
                    "MAIGRET_TARGET_TIMEOUT_SECONDS",
                    DEFAULT_MAIGRET_TARGET_TIMEOUT_SECONDS,
                )),
                worker_label=worker_label,
            )
            written = await _store_observations(conn, observations)
            await conn.execute(
                "UPDATE recon_targets SET status = 'completed', error = NULL, updated_at = NOW() WHERE id = $1::uuid",
                target["id"],
            )
            await _mark_source_health(conn, "running", None, success=True)
            return {
                "status": "completed",
                "target": target,
                "modules": modules,
                "engine": MAIGRET_MODULE,
                "observations": written,
                "dry_run": False,
            }
        except Exception as exc:  # noqa: BLE001
            error = str(exc)[:500]
            await conn.execute(
                "UPDATE recon_targets SET status = 'failed', error = $2, updated_at = NOW() WHERE id = $1::uuid",
                target["id"],
                error,
            )
            # maigret timeouts are target-level; keep the recon service green.
            if "maigret timed out" in error or _is_target_level_failure(error):
                await _mark_source_health(conn, "running", None, success=True)
            else:
                await _mark_source_health(conn, "degraded", error)
            return {
                "status": "failed",
                "target": target,
                "modules": modules,
                "engine": MAIGRET_MODULE,
                "observations": 0,
                "error": error,
                "dry_run": False,
            }

    try:
        raw_rows = await _run_spiderfoot_cli(
            target,
            modules,
            int(os.getenv("SPIDERFOOT_TARGET_TIMEOUT_SECONDS", "300")),
            worker_label=worker_label,
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
        error = str(exc)[:500]
        await conn.execute(
            "UPDATE recon_targets SET status = 'failed', error = $2, updated_at = NOW() WHERE id = $1::uuid",
            target["id"],
            error,
        )
        if _is_target_level_failure(error):
            await _mark_source_health(conn, "running", None, success=True)
        else:
            await _mark_source_health(conn, "degraded", error)
        return {"status": "failed", "target": target, "modules": modules, "observations": 0, "error": error, "dry_run": False}
