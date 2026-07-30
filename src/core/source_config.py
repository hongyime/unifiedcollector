"""File-based per-source configuration loader (Option A: files authoritative).

On worker startup this module reads, for each known source:

    config/sources/<source>.targets   one target per line
    config/sources/<source>.env        KEY=VALUE tunable args (optional)

and SYNCS them into the database / process environment so an operator can edit a
plain text file, `docker restart`, and have the new config take effect -- without
touching the dashboard, the DB, or the compose env blocks.

Authoritative semantics (Option A, chosen by Bryan):
  * If `<source>.targets` EXISTS, it is the single source of truth for that
    source's collection_targets: targets in the file are upserted, and any DB
    target for that source NOT in the file is removed. Edit the file -> restart ->
    DB matches the file exactly.
  * If the file does NOT exist for a source, the DB is left untouched (so sources
    you haven't migrated to files keep their dashboard/DB-managed targets).
  * `<source>.env` lines are injected into os.environ (not overwriting values that
    are already set by the real container environment, so compose/.env still wins
    for genuine secrets -- the per-source file is for *tunables* an operator wants
    to hand-edit).

.targets line format (whitespace-flexible, pipe-delimited optional fields):

    target_id
    target_id | Display Name
    target_id | Display Name | priority

Blank lines and lines starting with `#` are ignored. Inline `#` comments are
stripped. A leading `!` disables (skips) a line without deleting it.

This module has NO collector imports -> safe to import early in worker startup.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# All sources that participate in file-based config. Keep in sync with the
# collector registry; unknown files are ignored, missing files are fine.
KNOWN_SOURCES = (
    "github", "instagram", "lemon8", "telegram", "tiktok",
    "youtube", "strava", "website", "search", "beeper", "whatsapp",
)


def _config_root() -> Path:
    """Resolve the config/sources directory.

    Honours COLLECTOR_CONFIG_DIR (absolute path to the dir holding <source>.targets
    files). Defaults to <repo>/config/sources. In the container the repo lives at
    /app so the default resolves to /app/config/sources.
    """
    override = os.getenv("COLLECTOR_CONFIG_DIR", "").strip()
    if override:
        return Path(override)
    # this file: <repo>/src/core/source_config.py  -> repo root is parents[2]
    return Path(__file__).resolve().parents[2] / "config" / "sources"


def _parse_targets_file(path: Path) -> list[dict]:
    """Parse a <source>.targets file into a list of {target_id, name, priority}."""
    out: list[dict] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):  # explicitly disabled line
            continue
        # strip trailing inline comment (but not '#' inside a value before a pipe;
        # target ids never contain '#', so this is safe)
        if "#" in line:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
        parts = [p.strip() for p in line.split("|")]
        target_id = parts[0]
        if not target_id or target_id in seen:
            continue
        seen.add(target_id)
        name = parts[1] if len(parts) > 1 and parts[1] else None
        priority = 5
        if len(parts) > 2 and parts[2]:
            try:
                priority = int(parts[2])
            except ValueError:
                logger.warning("source_config: bad priority %r in %s, using 5", parts[2], path.name)
        out.append({"target_id": target_id, "name": name, "priority": priority})
    return out


def _load_env_file(path: Path) -> int:
    """Inject KEY=VALUE pairs from <source>.env into os.environ.

    Does NOT overwrite a key already present in the real environment (so genuine
    container secrets/compose values win). Returns count of keys applied.
    """
    applied = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not key:
            continue
        if key in os.environ and os.environ[key] != "":
            # real env wins; file is only a fallback default for tunables
            continue
        os.environ[key] = val
        applied += 1
    return applied


async def sync_source_configs(pool) -> dict:
    """Read every config/sources/<source>.{targets,env} and sync to DB + env.

    Returns a summary dict {source: {"targets": n, "removed": m, "env": k}} for
    logging. Safe to call once at startup. Never raises -- a malformed file logs
    a warning and is skipped so one bad file can't stop the whole worker.
    """
    root = _config_root()
    summary: dict = {}
    if not root.is_dir():
        logger.info("source_config: no config dir at %s (file-based config inactive)", root)
        return summary

    for source in KNOWN_SOURCES:
        src_summary = {"targets": 0, "removed": 0, "env": 0}

        # 1. env file (apply BEFORE targets so any tunables are live)
        env_path = root / f"{source}.env"
        if env_path.is_file():
            try:
                src_summary["env"] = _load_env_file(env_path)
            except Exception as exc:
                logger.warning("source_config: failed to load %s: %s", env_path.name, exc)

        # 2. targets file (authoritative sync)
        tgt_path = root / f"{source}.targets"
        if tgt_path.is_file():
            try:
                targets = _parse_targets_file(tgt_path)
                src_summary["targets"] = len(targets)
                removed = await _sync_targets_to_db(pool, source, targets)
                src_summary["removed"] = removed
            except Exception as exc:
                logger.warning("source_config: failed to sync %s targets: %s", source, exc, exc_info=True)

        if src_summary["targets"] or src_summary["env"]:
            summary[source] = src_summary
            logger.info(
                "source_config: %s -> %d targets (%d removed as no longer in file), %d env keys",
                source, src_summary["targets"], src_summary["removed"], src_summary["env"],
            )

    return summary


async def _sync_targets_to_db(pool, source: str, targets: list[dict]) -> int:
    """Make collection_targets for `source` exactly match `targets` (Option A).

    Upserts every file target (resetting status to 'pending' so re-added targets
    get re-collected) and deletes any DB target for this source whose id is not in
    the file. Returns count removed. Runs in one transaction.
    """
    file_ids = [t["target_id"] for t in targets]
    async with pool.acquire() as conn:
        async with conn.transaction():
            for t in targets:
                await conn.execute(
                    "INSERT INTO collection_targets (source, target_id, target_name, priority, status) "
                    "VALUES ($1, $2, $3, $4, 'pending') "
                    "ON CONFLICT (source, target_id) DO UPDATE "
                    "SET target_name = COALESCE($3, collection_targets.target_name), "
                    "    priority = $4",
                    source, t["target_id"], t["name"], t["priority"],
                )
            # remove DB targets no longer present in the file
            if file_ids:
                rows = await conn.fetch(
                    "DELETE FROM collection_targets "
                    "WHERE source = $1 AND target_id <> ALL($2::text[]) "
                    "  AND COALESCE(metadata->>'preserve_on_source_config_sync', 'false') <> 'true' "
                    "RETURNING target_id",
                    source, file_ids,
                )
            else:
                rows = await conn.fetch(
                    "DELETE FROM collection_targets "
                    "WHERE source = $1 "
                    "  AND COALESCE(metadata->>'preserve_on_source_config_sync', 'false') <> 'true' "
                    "RETURNING target_id",
                    source,
                )
    return len(rows)
