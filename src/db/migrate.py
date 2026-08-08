"""Database schema + migration runner.

Single source of truth for applying DDL. Replaces the four duplicated
``init_db()`` loops (worker, scheduler, dashboard, main) that each globbed
``db/schemas/*.sql`` only — which silently omitted every table defined under
``db/migrations/`` and produced a half-built database on a clean volume.

Two phases, applied in order inside :func:`apply_all`:

1. **Base schemas** — ``db/schemas/*.sql`` applied every run. These are written
   idempotently (``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS``)
   so re-running is a no-op. They define the core tables.

2. **Incremental migrations** — ``db/migrations/*.sql`` applied **once each**,
   tracked in the ``schema_migrations`` ledger by filename + sha256. A migration
   already in the ledger is skipped. A migration whose on-disk checksum no longer
   matches the ledger raises loudly (someone edited an applied migration — drift).

Some migration files are intentionally NOT auto-applied (see ``SKIP``):
  * ``v2_schema.sql`` / ``v2_schema_final.sql`` — superseded full-schema dumps
    from before ``schemas/`` was finalized; not code-referenced.
  * ``drop_wa_face_tables.sql`` — a destructive DROP, run manually if ever needed.

The ledger makes apply-once safe even for non-idempotent migrations (e.g.
``add_telegram_phase1.sql`` does ``DROP TABLE ... CASCADE; CREATE TABLE ...``
which would destroy live data if re-run every boot).
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Postgres SQLSTATE for a statement cancelled by lock_timeout.
_LOCK_TIMEOUT_SQLSTATE = "55P03"


def _is_lock_timeout(exc: Exception) -> bool:
    return getattr(exc, "sqlstate", None) == _LOCK_TIMEOUT_SQLSTATE

_DB_DIR = Path(__file__).resolve().parent
SCHEMAS_DIR = _DB_DIR / "schemas"
MIGRATIONS_DIR = _DB_DIR / "migrations"

# Migration filenames that must NOT be auto-applied by the runner.
SKIP: frozenset[str] = frozenset({
    "v2_schema.sql",          # superseded full-schema dump (pre-2026-05-26)
    "v2_schema_final.sql",    # superseded adjustment dump
    "drop_wa_face_tables.sql",  # destructive DROP — apply by hand if needed
})

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def apply_all(pool) -> dict:
    """Apply base schemas then pending migrations. Returns a summary dict.

    Idempotent and safe to call on every service startup.
    """
    summary = {"schemas": 0, "migrations_applied": [], "migrations_skipped": 0, "deferred": False}

    async with pool.acquire() as conn:
        try:
            locked = await conn.fetchval("SELECT pg_try_advisory_lock(hashtext('unifiedcollector_migrate'))")
            if not locked:
                logger.info("Migration runner deferred: another instance is currently migrating.")
                summary["deferred"] = True
                return summary
        except Exception:
            pass

        # DDL vs. the daily pg_dump: pg_dump holds AccessShareLock on EVERY table
        # for the whole dump (minutes). A migration's ACCESS EXCLUSIVE request would
        # queue behind it AND block all other traffic on that table behind the
        # migration — a self-inflicted multi-table stall. A short lock_timeout makes
        # DDL fail fast instead of piling up; a deferred migration is retried on the
        # next boot (idempotent + ledger-tracked). Reset on the way out so the
        # setting can't leak to the pooled connection's later users.
        lock_ms = int(os.getenv("MIGRATE_LOCK_TIMEOUT_MS", "10000"))
        try:
            # Use parameter binding for CodeQL, but keep the setting session-local.
            # asyncpg runs each statement in its own implicit transaction; using
            # set_config(..., true) would reset lock_timeout immediately after this
            # statement and let later DDL block behind pg_dump again.
            await conn.execute("SELECT set_config('lock_timeout', $1, false)", f"{lock_ms}ms")
        except Exception:  # pragma: no cover - defensive
            logger.debug("could not SET lock_timeout", exc_info=True)

        try:
            # Phase 0: required extensions. The live DB had these enabled by hand;
            # a clean boot must create them or DDL using vector()/gen_random_uuid()
            # fails ("type vector does not exist"). This is the P0 drift class.
            for ext in ("pgcrypto", "vector"):
                try:
                    await conn.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("Could not create extension %s: %s", ext, exc)

            # Ledger first so we can record migrations.
            await conn.execute(_LEDGER_DDL)

            # Phase 1: base schemas (idempotent, applied every run).
            if SCHEMAS_DIR.is_dir():
                for sql_file in sorted(SCHEMAS_DIR.glob("*.sql")):
                    try:
                        await conn.execute(sql_file.read_text(encoding="utf-8"))
                        summary["schemas"] += 1
                    except Exception as exc:
                        if _is_lock_timeout(exc):
                            logger.warning(
                                "Base schema %s deferred: lock_timeout (a long reader "
                                "like pg_dump holds table locks). Retrying next boot.",
                                sql_file.name,
                            )
                            summary["deferred"] = True
                            return summary  # defer the rest; boot continues normally
                        raise
                logger.info("Applied %d base schema file(s)", summary["schemas"])

            # Phase 2: incremental migrations (apply-once via ledger).
            applied = {
                r["filename"]: r["checksum"]
                for r in await conn.fetch("SELECT filename, checksum FROM schema_migrations")
            }

            if MIGRATIONS_DIR.is_dir():
                for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
                    name = sql_file.name
                    if name in SKIP:
                        summary["migrations_skipped"] += 1
                        continue

                    body = sql_file.read_text(encoding="utf-8")
                    checksum = _sha256(body)

                    if name in applied:
                        if applied[name] != checksum:
                            # An already-applied migration was edited on disk.
                            # Fail loudly — silently re-running could DROP data.
                            raise RuntimeError(
                                f"Migration drift: {name} was already applied with a "
                                f"different checksum (ledger={applied[name][:12]}, "
                                f"disk={checksum[:12]}). Edited an applied migration? "
                                f"Create a NEW migration instead of editing this one."
                            )
                        continue  # already applied, unchanged — skip

                    # Apply the migration in its own transaction so a failure
                    # rolls back cleanly and is not recorded as applied.
                    try:
                        async with conn.transaction():
                            await conn.execute(body)
                            await conn.execute(
                                "INSERT INTO schema_migrations (filename, checksum) "
                                "VALUES ($1, $2)",
                                name, checksum,
                            )
                    except Exception as exc:
                        if _is_lock_timeout(exc):
                            logger.warning(
                                "Migration %s deferred: lock_timeout (a long reader "
                                "like pg_dump holds table locks). Retrying next boot.",
                                name,
                            )
                            summary["deferred"] = True
                            break  # defer remaining migrations; boot continues
                        raise
                    summary["migrations_applied"].append(name)
                    logger.info("Applied migration: %s", name)
        finally:
            # Never let the migration lock_timeout leak to this pooled connection's
            # subsequent users.
            try:
                await conn.execute("SET lock_timeout = DEFAULT")
            except Exception:  # pragma: no cover - defensive
                logger.debug("could not reset lock_timeout", exc_info=True)
            try:
                await conn.execute("SELECT pg_advisory_unlock(hashtext('unifiedcollector_migrate'))")
            except Exception:
                pass

    if summary["migrations_applied"]:
        logger.info(
            "Migration runner: %d base schema(s), %d migration(s) applied, %d skipped",
            summary["schemas"], len(summary["migrations_applied"]),
            summary["migrations_skipped"],
        )
    return summary


async def backfill_ledger_for_existing_db(pool) -> int:
    """One-time helper for an ALREADY-POPULATED database.

    The live production DB already has every migration's tables (they were
    applied by hand before this runner existed). Re-applying non-idempotent
    migrations like add_telegram_phase1.sql would DROP live data. This marks
    every non-SKIP migration as already-applied (recording current on-disk
    checksums) WITHOUT executing them, so the runner treats the existing DB
    as fully migrated.

    Returns the number of ledger rows inserted. Safe to run repeatedly.
    """
    inserted = 0
    async with pool.acquire() as conn:
        await conn.execute(_LEDGER_DDL)
        existing = {
            r["filename"]
            for r in await conn.fetch("SELECT filename FROM schema_migrations")
        }
        if MIGRATIONS_DIR.is_dir():
            for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
                name = sql_file.name
                if name in SKIP or name in existing:
                    continue
                checksum = _sha256(sql_file.read_text(encoding="utf-8"))
                await conn.execute(
                    "INSERT INTO schema_migrations (filename, checksum) "
                    "VALUES ($1, $2) ON CONFLICT (filename) DO NOTHING",
                    name, checksum,
                )
                inserted += 1
    logger.info("Ledger backfill: marked %d existing migration(s) as applied", inserted)
    return inserted
