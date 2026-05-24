from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import re
from typing import Any

import asyncpg

from shared.db import create_pool

from .config import settings
from .control_plane_secrets import EncryptedSecret, SecretCipher, masked_secret
from .db_retry import with_db_retry
from .observability import get_logger

logger = get_logger(__name__)

_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_BOOTSTRAP_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "uninitialized": {"wizard_in_progress"},
    "wizard_in_progress": {"initialized", "uninitialized"},
    "initialized": set(),
}
_ROLE_LEVELS: dict[str, int] = {
    "viewer": 1,
    "operator": 2,
    "admin": 3,
}


def _to_dt(value: Any) -> datetime:
    """Convert a value to a naive-UTC datetime matching TIMESTAMP columns."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(tzinfo=None)
    return datetime.utcnow()


class Database:
    def __init__(self) -> None:
        self.pool: asyncpg.Pool | None = None
        self._secret_cipher: SecretCipher | None = None

    async def connect(self) -> None:
        if self.pool:
            return
        self.pool = await create_pool(
            settings.DATABASE_URL,
            min_size=settings.DB_POOL_SIZE,
            max_size=settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW,
            command_timeout=float(settings.DB_POOL_TIMEOUT),
            max_inactive_connection_lifetime=float(settings.DB_POOL_RECYCLE),
        )
        await self.ensure_schema_compatibility()
        logger.info("collector_db_connected")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None
            logger.info("collector_db_closed")

    def _require_pool(self) -> asyncpg.Pool:
        if not self.pool:
            raise RuntimeError("Database pool is not initialized")
        return self.pool

    @staticmethod
    def _require_mutation_authorization(
        actor_id: str | None,
        actor_role: str | None,
        *,
        minimum_role: str = "operator",
    ) -> None:
        """Reject unauthenticated or under-privileged mutation requests."""
        normalized_actor = (actor_id or "").strip()
        normalized_role = (actor_role or "").strip().lower()
        required_role = (minimum_role or "operator").strip().lower()

        if not normalized_actor:
            raise PermissionError("Unauthenticated mutation request rejected")

        if required_role not in _ROLE_LEVELS:
            raise PermissionError(f"Unknown minimum role: {required_role!r}")
        if normalized_role not in _ROLE_LEVELS:
            raise PermissionError(f"Unknown actor role: {normalized_role!r}")

        if _ROLE_LEVELS[normalized_role] < _ROLE_LEVELS[required_role]:
            raise PermissionError(
                f"Insufficient role: {normalized_role!r} cannot perform mutation requiring {required_role!r}"
            )

    @staticmethod
    def _assert_bootstrap_transition(current_state: str, new_state: str) -> None:
        allowed = _BOOTSTRAP_ALLOWED_TRANSITIONS.get(current_state)
        if allowed is None:
            raise ValueError(f"Unknown bootstrap state: {current_state!r}")
        if new_state not in allowed:
            raise ValueError(
                f"Invalid bootstrap transition: {current_state!r} -> {new_state!r}"
            )

    def _get_secret_cipher(self) -> SecretCipher:
        if self._secret_cipher is None:
            self._secret_cipher = SecretCipher(
                key_material=settings.CONTROL_PLANE_SECRET_KEY,
                key_id=settings.CONTROL_PLANE_SECRET_KEY_ID,
            )
        return self._secret_cipher

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        value = (identifier or "").strip().lower()
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError(f"Invalid SQL identifier: {identifier!r}")
        return f'"{value}"'

    @staticmethod
    def _normalize_target_schemas(schemas: list[str]) -> list[str]:
        requested = [(schema or "").strip().lower() for schema in schemas if (schema or "").strip()]
        if not requested:
            raise ValueError("At least one schema must be provided")

        allowed = set(settings.wipeable_schemas)
        invalid = sorted(schema for schema in requested if schema not in allowed)
        if invalid:
            raise ValueError(f"Unsupported schema target(s): {', '.join(invalid)}")

        # Preserve order but deduplicate.
        normalized: list[str] = []
        for schema in requested:
            if schema not in normalized:
                normalized.append(schema)
        return normalized

    @with_db_retry()
    async def health_check(self) -> bool:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            return (await conn.fetchval("SELECT 1")) == 1

    async def ensure_schema_compatibility(self) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "ALTER TABLE collector.backfill_jobs ADD COLUMN IF NOT EXISTS correlation_id TEXT"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_backfill_jobs_correlation_id "
                "ON collector.backfill_jobs(correlation_id)"
            )
            await conn.execute(
                "ALTER TABLE collector.user_sightings ADD COLUMN IF NOT EXISTS source_message_id TEXT"
            )
            await conn.execute(
                "ALTER TABLE collector.user_sightings ADD COLUMN IF NOT EXISTS source_chat_jid TEXT"
            )
            await conn.execute(
                "ALTER TABLE collector.user_sightings ADD COLUMN IF NOT EXISTS session_name VARCHAR(100)"
            )
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_sightings_source "
                "ON collector.user_sightings(user_jid, seen_in_chat_jid, source_message_id, source_chat_jid)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_sightings_source_message "
                "ON collector.user_sightings(source_message_id)"
            )

    @with_db_retry()
    async def seed_registry_and_cursors(self) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                for service in settings.known_services:
                    await conn.execute(
                        """
                        INSERT INTO collector.service_registry(service_name, is_active)
                        VALUES ($1, TRUE)
                        ON CONFLICT (service_name)
                        DO UPDATE SET is_active = EXCLUDED.is_active
                        """,
                        service,
                    )
                    await conn.execute(
                        """
                        INSERT INTO collector.service_cursors(service_name, last_message_id)
                        VALUES ($1, 0)
                        ON CONFLICT (service_name) DO NOTHING
                        """,
                        service,
                    )

    @with_db_retry()
    async def upsert_raw_message(self, payload: dict[str, Any], session_name: str) -> None:
        pool = self._require_pool()
        message_id = payload.get("message_id")
        chat_jid = payload.get("chat_jid")
        if not message_id or not chat_jid:
            return

        has_media = bool(payload.get("has_media", False))
        if not has_media and payload.get("media_metadata") is not None:
            has_media = True

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO collector.raw_messages (
                    message_id, chat_jid, chat_type, sender_jid, sender_lid,
                    session_name, message_type, body, has_media, is_forwarded,
                    forwarding_score, quoted_msg_id, is_edit, is_deleted, raw_payload
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9, $10,
                    $11, $12, $13, $14, $15::jsonb
                )
                ON CONFLICT (message_id, chat_jid)
                DO NOTHING
                """,
                str(message_id),
                str(chat_jid),
                payload.get("chat_type"),
                payload.get("sender_jid"),
                payload.get("sender_lid"),
                session_name,
                payload.get("message_type") or "unknown",
                payload.get("body"),
                has_media,
                bool(payload.get("is_forwarded", False)),
                int(payload.get("forwarding_score", 0) or 0),
                payload.get("quoted_msg_id"),
                bool(payload.get("is_edit", False)),
                bool(payload.get("is_deleted", False)),
                json.dumps(payload),
            )

    @with_db_retry()
    async def upsert_user_sighting(
        self,
        *,
        user_jid: str,
        seen_in_chat_jid: str,
        source_message_id: str,
        source_chat_jid: str,
        session_name: str,
        payload: dict[str, Any],
    ) -> None:
        pool = self._require_pool()
        if not user_jid or not seen_in_chat_jid or not source_message_id or not source_chat_jid:
            return

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO collector.user_sightings (
                    user_jid, seen_in_chat_jid, seen_at,
                    source_message_id, source_chat_jid, session_name, payload
                )
                VALUES ($1, $2, NOW(), $3, $4, $5, $6::jsonb)
                ON CONFLICT (user_jid, seen_in_chat_jid, source_message_id, source_chat_jid)
                DO UPDATE SET
                    seen_at = NOW(),
                    session_name = EXCLUDED.session_name,
                    payload = EXCLUDED.payload
                """,
                user_jid,
                seen_in_chat_jid,
                source_message_id,
                source_chat_jid,
                session_name,
                json.dumps(payload),
            )

    @with_db_retry()
    async def upsert_user(self, payload: dict[str, Any]) -> None:
        pool = self._require_pool()
        jid = payload.get("jid")
        if not jid:
            return

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO collector.users (
                    jid, lid, phone_number, display_name, push_name,
                    business_name, is_business, is_verified, payload, first_seen, last_seen
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9::jsonb, NOW(), NOW()
                )
                ON CONFLICT (jid)
                DO UPDATE SET
                    lid = COALESCE(EXCLUDED.lid, collector.users.lid),
                    phone_number = COALESCE(EXCLUDED.phone_number, collector.users.phone_number),
                    display_name = COALESCE(EXCLUDED.display_name, collector.users.display_name),
                    push_name = COALESCE(EXCLUDED.push_name, collector.users.push_name),
                    business_name = COALESCE(EXCLUDED.business_name, collector.users.business_name),
                    is_business = EXCLUDED.is_business,
                    is_verified = EXCLUDED.is_verified,
                    payload = EXCLUDED.payload,
                    last_seen = NOW()
                """,
                jid,
                payload.get("lid"),
                payload.get("phone_number"),
                payload.get("display_name"),
                payload.get("push_name"),
                payload.get("business_name"),
                bool(payload.get("is_business", False)),
                bool(payload.get("is_verified", False)),
                json.dumps(payload),
            )

    @with_db_retry()
    async def upsert_jid_lid_map(self, payload: dict[str, Any], session_name: str) -> None:
        pool = self._require_pool()
        jid = payload.get("jid")
        lid = payload.get("lid")
        if not jid or not lid:
            return

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO collector.jid_lid_map (jid, lid, session_name, mapped_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (jid, session_name)
                DO UPDATE SET lid = EXCLUDED.lid, mapped_at = NOW()
                """,
                jid,
                lid,
                session_name,
            )

    @with_db_retry()
    async def upsert_chat(self, payload: dict[str, Any]) -> None:
        pool = self._require_pool()
        jid = payload.get("jid") or payload.get("chat_jid")
        if not jid:
            return
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO collector.chats (
                    jid, chat_type, name, description, photo_path,
                    member_count, is_community, community_jid, created_at, payload
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9, $10::jsonb
                )
                ON CONFLICT (jid)
                DO UPDATE SET
                    chat_type = EXCLUDED.chat_type,
                    name = COALESCE(EXCLUDED.name, collector.chats.name),
                    description = COALESCE(EXCLUDED.description, collector.chats.description),
                    photo_path = COALESCE(EXCLUDED.photo_path, collector.chats.photo_path),
                    member_count = COALESCE(EXCLUDED.member_count, collector.chats.member_count),
                    is_community = EXCLUDED.is_community,
                    community_jid = COALESCE(EXCLUDED.community_jid, collector.chats.community_jid),
                    payload = EXCLUDED.payload,
                    collected_at = NOW()
                """,
                jid,
                payload.get("chat_type") or "group",
                payload.get("subject") or payload.get("name"),
                payload.get("description"),
                payload.get("photo_path"),
                payload.get("member_count"),
                bool(payload.get("is_community", False)),
                payload.get("community_jid"),
                _to_dt(payload.get("created_at")),
                json.dumps(payload),
            )

    @with_db_retry()
    async def upsert_group_participants(self, chat_jid: str, participants: list[dict[str, Any]]) -> None:
        if not chat_jid or not participants:
            return
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                for participant in participants:
                    user_jid = participant.get("id") or participant.get("user_jid")
                    if not user_jid:
                        continue
                    await conn.execute(
                        """
                        INSERT INTO collector.group_participants (
                            chat_jid, user_jid, role, joined_at, seen_at
                        ) VALUES ($1, $2, $3, NOW(), NOW())
                        ON CONFLICT (chat_jid, user_jid)
                        DO UPDATE SET role = EXCLUDED.role, seen_at = NOW()
                        """,
                        chat_jid,
                        user_jid,
                        participant.get("role") or "member",
                    )

    @with_db_retry()
    async def upsert_wa_session(self, payload: dict[str, Any]) -> None:
        pool = self._require_pool()
        session_name = payload.get("session_name")
        if not session_name:
            return
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO collector.wa_sessions (
                    session_name, phone_jid, display_name, status, last_connected, cooldown_until
                ) VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (session_name)
                DO UPDATE SET
                    phone_jid = COALESCE(EXCLUDED.phone_jid, collector.wa_sessions.phone_jid),
                    display_name = COALESCE(EXCLUDED.display_name, collector.wa_sessions.display_name),
                    status = EXCLUDED.status,
                    last_connected = COALESCE(EXCLUDED.last_connected, collector.wa_sessions.last_connected),
                    cooldown_until = COALESCE(EXCLUDED.cooldown_until, collector.wa_sessions.cooldown_until)
                """,
                session_name,
                payload.get("phone_jid"),
                payload.get("display_name"),
                payload.get("status") or "active",
                _to_dt(payload.get("last_connected")),
                _to_dt(payload.get("cooldown_until")) if payload.get("cooldown_until") else None,
            )

    @with_db_retry()
    async def insert_session_event(self, payload: dict[str, Any]) -> None:
        pool = self._require_pool()
        session_name = payload.get("session_name")
        event_type = payload.get("event_type")
        if not session_name or not event_type:
            return
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO collector.session_events (session_name, event_type, detail, occurred_at)
                VALUES ($1, $2, $3, NOW())
                """,
                session_name,
                event_type,
                payload.get("detail") or json.dumps(payload),
            )

    @with_db_retry()
    async def insert_call(self, payload: dict[str, Any]) -> None:
        pool = self._require_pool()
        call_id = payload.get("call_id")
        if not call_id:
            return
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO collector.calls (
                    call_id, from_jid, chat_jid, call_type, status,
                    duration_seconds, session_name, occurred_at, raw_payload
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                """,
                call_id,
                payload.get("from_jid") or payload.get("from"),
                payload.get("chat_jid"),
                payload.get("call_type"),
                payload.get("status"),
                payload.get("duration_seconds"),
                payload.get("session_name"),
                _to_dt(payload.get("occurred_at")),
                json.dumps(payload),
            )

    @with_db_retry()
    async def get_backfill_jobs_to_resume(self) -> list[asyncpg.Record]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT id, session_name, chat_jid, status,
                       oldest_msg_key, oldest_msg_ts, messages_done,
                       cutoff_date, correlation_id
                FROM collector.backfill_jobs
                WHERE status IN ('pending', 'running')
                ORDER BY updated_at ASC
                """
            )

    @with_db_retry()
    async def mark_backfill_running(self, job_id: int, correlation_id: str) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE collector.backfill_jobs
                SET status = 'running', correlation_id = $2, updated_at = NOW()
                WHERE id = $1
                """,
                job_id,
                correlation_id,
            )

    @with_db_retry()
    async def mark_backfill_complete_by_correlation(self, correlation_id: str) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE collector.backfill_jobs
                SET status = 'complete', updated_at = NOW()
                WHERE correlation_id = $1
                """,
                correlation_id,
            )

    @with_db_retry()
    async def update_backfill_progress_by_correlation(
        self,
        correlation_id: str,
        messages: list[dict[str, Any]],
    ) -> bool:
        if not correlation_id:
            return False

        pool = self._require_pool()
        oldest_ts = None
        oldest_key = None

        for message in messages:
            ts = message.get("timestamp")
            if ts is None:
                continue
            ts_i = int(ts)
            if oldest_ts is None or ts_i < oldest_ts:
                oldest_ts = ts_i
                oldest_key = message.get("key")

        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE collector.backfill_jobs
                SET oldest_msg_key = COALESCE($2::jsonb, oldest_msg_key),
                    oldest_msg_ts = COALESCE($3::bigint, oldest_msg_ts),
                    messages_done = messages_done + $4,
                    status = 'running',
                    updated_at = NOW()
                WHERE correlation_id = $1
                """,
                correlation_id,
                json.dumps(oldest_key) if oldest_key else None,
                oldest_ts,
                len(messages),
            )
            return result.endswith("1") or result.split(" ")[-1].isdigit() and int(result.split(" ")[-1]) > 0

    @with_db_retry()
    async def pause_backfills_for_session(self, session_name: str) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE collector.backfill_jobs
                SET status = 'pending', updated_at = NOW()
                WHERE session_name = $1 AND status = 'running'
                """,
                session_name,
            )

    @with_db_retry()
    async def get_active_sessions(self) -> list[asyncpg.Record]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT id, session_name, status, created_at
                FROM collector.wa_sessions
                WHERE status IN ('active', 'connecting', 'connected')
                """
            )

    @with_db_retry()
    async def get_session_events_recent(self, session_name: str, window_seconds: int) -> list[asyncpg.Record]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT event_type, detail, occurred_at
                FROM collector.session_events
                WHERE session_name = $1
                  AND occurred_at >= NOW() - ($2 * interval '1 second')
                ORDER BY occurred_at DESC
                """,
                session_name,
                window_seconds,
            )

    @with_db_retry()
    async def set_session_cooldown(self, session_name: str, cooldown_seconds: int) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE collector.wa_sessions
                SET status = 'paused',
                    cooldown_until = NOW() + ($2 * interval '1 second')
                WHERE session_name = $1
                """,
                session_name,
                cooldown_seconds,
            )

    @with_db_retry()
    async def get_backfill_jobs(self, limit: int = 200) -> list[asyncpg.Record]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT id, session_name, chat_jid, status, messages_done,
                       oldest_msg_ts, cutoff_date, updated_at, correlation_id
                FROM collector.backfill_jobs
                ORDER BY updated_at DESC
                LIMIT $1
                """,
                limit,
            )

    @with_db_retry()
    async def get_service_cursors(self) -> list[asyncpg.Record]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT service_name, last_message_id, updated_at
                FROM collector.service_cursors
                ORDER BY service_name
                """
            )

    @with_db_retry()
    async def get_schema_table_counts(self, schemas: list[str]) -> list[asyncpg.Record]:
        pool = self._require_pool()
        targets = self._normalize_target_schemas(schemas)
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT schemaname AS schema_name,
                       relname AS table_name,
                       n_live_tup::bigint AS estimated_rows
                FROM pg_stat_user_tables
                WHERE schemaname = ANY($1::text[])
                ORDER BY schemaname, relname
                """,
                targets,
            )

    @with_db_retry()
    async def wipe_schemas(self, schemas: list[str]) -> list[dict[str, int]]:
        """Destructively truncate all tables in the target schema list.

        This action uses RESTART IDENTITY CASCADE and should only be called from
        explicit operator workflows (dashboard danger zone).
        """
        pool = self._require_pool()
        targets = self._normalize_target_schemas(schemas)
        results: list[dict[str, int]] = []

        async with pool.acquire() as conn:
            async with conn.transaction():
                for schema in targets:
                    tables = await conn.fetch(
                        """
                        SELECT tablename
                        FROM pg_tables
                        WHERE schemaname = $1
                        ORDER BY tablename
                        """,
                        schema,
                    )

                    table_names = [row["tablename"] for row in tables]
                    if table_names:
                        qualified = ", ".join(
                            f"{self._quote_identifier(schema)}.{self._quote_identifier(table)}"
                            for table in table_names
                        )
                        await conn.execute(f"TRUNCATE TABLE {qualified} RESTART IDENTITY CASCADE")

                    results.append({"schema": schema, "tables_truncated": len(table_names)})

                if "collector" in targets:
                    for service in settings.known_services:
                        await conn.execute(
                            """
                            INSERT INTO collector.service_registry(service_name, is_active)
                            VALUES ($1, TRUE)
                            ON CONFLICT (service_name)
                            DO UPDATE SET is_active = EXCLUDED.is_active
                            """,
                            service,
                        )
                        await conn.execute(
                            """
                            INSERT INTO collector.service_cursors(service_name, last_message_id)
                            VALUES ($1, 0)
                            ON CONFLICT (service_name)
                            DO UPDATE SET last_message_id = 0, updated_at = NOW()
                            """,
                            service,
                        )

        return results

    @with_db_retry()
    async def upsert_system_config(self, key: str, value: str) -> None:
        """Upsert a key-value pair into collector.system_config."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO collector.system_config(key, value, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value,
                        updated_at = NOW()
                """,
                key,
                value,
            )

    @with_db_retry()
    async def get_system_config(self, key: str) -> str | None:
        """Fetch a value from collector.system_config by key. Returns None if absent."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT value FROM collector.system_config WHERE key = $1",
                key,
            )

    @with_db_retry()
    async def get_group_chats(self) -> list[asyncpg.Record]:
        """Return all group chats from collector.chats."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT jid, name, member_count, collected_at
                FROM collector.chats
                WHERE chat_type = 'group'
                ORDER BY collected_at DESC
                """,
            )

    @with_db_retry()
    async def get_control_bootstrap_state(self) -> dict[str, Any]:
        """Return the current bootstrap wizard lifecycle state."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT state, wizard_version, generated_defaults,
                       initialized_by, initialized_at, updated_at
                FROM collector.control_bootstrap_state
                WHERE singleton_id = 1
                """
            )
            if row is None:
                await conn.execute(
                    """
                    INSERT INTO collector.control_bootstrap_state (singleton_id)
                    VALUES (1)
                    ON CONFLICT (singleton_id) DO NOTHING
                    """
                )
                row = await conn.fetchrow(
                    """
                    SELECT state, wizard_version, generated_defaults,
                           initialized_by, initialized_at, updated_at
                    FROM collector.control_bootstrap_state
                    WHERE singleton_id = 1
                    """
                )

        return {
            "state": row["state"],
            "wizard_version": row["wizard_version"],
            "generated_defaults": row["generated_defaults"] or {},
            "initialized_by": row["initialized_by"],
            "initialized_at": row["initialized_at"],
            "updated_at": row["updated_at"],
        }

    @with_db_retry()
    async def start_bootstrap_wizard(
        self,
        *,
        wizard_version: str,
        actor_id: str | None = None,
        actor_role: str | None = None,
        request_id: str | None = None,
        reason: str | None = None,
        generated_defaults: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Transition bootstrap state to wizard_in_progress.

        Only valid transition is uninitialized -> wizard_in_progress.
        """
        version = (wizard_version or "").strip()
        if not version:
            raise ValueError("wizard_version is required")

        self._require_mutation_authorization(actor_id, actor_role, minimum_role="operator")

        defaults_json = json.dumps(generated_defaults or {})
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT state
                    FROM collector.control_bootstrap_state
                    WHERE singleton_id = 1
                    FOR UPDATE
                    """
                )
                if row is None:
                    await conn.execute(
                        """
                        INSERT INTO collector.control_bootstrap_state (singleton_id)
                        VALUES (1)
                        ON CONFLICT (singleton_id) DO NOTHING
                        """
                    )
                    current_state = "uninitialized"
                else:
                    current_state = str(row["state"])

                self._assert_bootstrap_transition(current_state, "wizard_in_progress")

                await conn.execute(
                    """
                    UPDATE collector.control_bootstrap_state
                    SET state = 'wizard_in_progress',
                        wizard_version = $1,
                        generated_defaults = $2::jsonb,
                        updated_at = NOW()
                    WHERE singleton_id = 1
                    """,
                    version,
                    defaults_json,
                )

                await conn.execute(
                    """
                    INSERT INTO collector.control_change_log (
                        event_type,
                        service_name,
                        config_key,
                        actor_id,
                        actor_role,
                        event_source,
                        request_id,
                        old_value_masked,
                        new_value_masked,
                        reason,
                        metadata,
                        created_at
                    )
                    VALUES (
                        'bootstrap_wizard_started',
                        'collector',
                        'bootstrap_state',
                        $1,
                        $2,
                        'dashboard',
                        $3,
                        $4,
                        $5,
                        $6,
                        $7::jsonb,
                        NOW()
                    )
                    """,
                    actor_id,
                    actor_role,
                    request_id,
                    current_state,
                    "wizard_in_progress",
                    reason,
                    json.dumps({"wizard_version": version}),
                )

                state_row = await conn.fetchrow(
                    """
                    SELECT state, wizard_version, generated_defaults,
                           initialized_by, initialized_at, updated_at
                    FROM collector.control_bootstrap_state
                    WHERE singleton_id = 1
                    """
                )

        return {
            "state": state_row["state"],
            "wizard_version": state_row["wizard_version"],
            "generated_defaults": state_row["generated_defaults"] or {},
            "initialized_by": state_row["initialized_by"],
            "initialized_at": state_row["initialized_at"],
            "updated_at": state_row["updated_at"],
        }

    @with_db_retry()
    async def commit_bootstrap_baseline(
        self,
        *,
        wizard_version: str,
        generated_defaults: dict[str, Any],
        config_values: list[dict[str, Any]],
        actor_id: str | None = None,
        actor_role: str | None = None,
        request_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Commit generated defaults and baseline config transactionally.

        Valid transition: wizard_in_progress -> initialized.
        """
        version = (wizard_version or "").strip()
        if not version:
            raise ValueError("wizard_version is required")
        if not config_values:
            raise ValueError("config_values must contain at least one baseline entry")

        self._require_mutation_authorization(actor_id, actor_role, minimum_role="operator")

        defaults_json = json.dumps(generated_defaults or {})
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                state_row = await conn.fetchrow(
                    """
                    SELECT state
                    FROM collector.control_bootstrap_state
                    WHERE singleton_id = 1
                    FOR UPDATE
                    """
                )
                if state_row is None:
                    await conn.execute(
                        """
                        INSERT INTO collector.control_bootstrap_state (singleton_id)
                        VALUES (1)
                        ON CONFLICT (singleton_id) DO NOTHING
                        """
                    )
                    current_state = "uninitialized"
                else:
                    current_state = str(state_row["state"])

                self._assert_bootstrap_transition(current_state, "initialized")

                for entry in config_values:
                    service_name = str(entry.get("service_name") or "").strip()
                    config_key = str(entry.get("config_key") or "").strip()
                    if not service_name or not config_key:
                        raise ValueError(
                            "Each baseline config entry must include service_name and config_key"
                        )

                    is_secret = bool(entry.get("is_secret", False))
                    if is_secret:
                        raise ValueError(
                            "Baseline commit only accepts non-secret values; "
                            "use upsert_control_secret for secrets"
                        )

                    scope = str(entry.get("scope") or "bootstrap")
                    if scope not in {"runtime", "restart", "bootstrap"}:
                        raise ValueError(
                            f"Invalid scope {scope!r} for {service_name}.{config_key}"
                        )

                    value = entry.get("value")
                    value_json = json.dumps(value)
                    requires_restart = bool(entry.get("requires_restart", False))
                    update_reason = str(
                        entry.get("update_reason")
                        or reason
                        or "bootstrap wizard baseline commit"
                    )

                    previous = await conn.fetchrow(
                        """
                        SELECT value_json
                        FROM collector.control_config_values
                        WHERE service_name = $1 AND config_key = $2
                        """,
                        service_name,
                        config_key,
                    )
                    old_value = previous["value_json"] if previous else None

                    await conn.execute(
                        """
                        INSERT INTO collector.control_config_values (
                            service_name,
                            config_key,
                            value_json,
                            scope,
                            is_secret,
                            requires_restart,
                            version,
                            updated_by,
                            update_reason,
                            updated_at
                        )
                        VALUES ($1, $2, $3::jsonb, $4, FALSE, $5, 1, $6, $7, NOW())
                        ON CONFLICT (service_name, config_key)
                        DO UPDATE SET
                            value_json = EXCLUDED.value_json,
                            scope = EXCLUDED.scope,
                            is_secret = FALSE,
                            requires_restart = EXCLUDED.requires_restart,
                            version = collector.control_config_values.version + 1,
                            updated_by = EXCLUDED.updated_by,
                            update_reason = EXCLUDED.update_reason,
                            updated_at = NOW()
                        """,
                        service_name,
                        config_key,
                        value_json,
                        scope,
                        requires_restart,
                        actor_id,
                        update_reason,
                    )

                    await conn.execute(
                        """
                        INSERT INTO collector.control_config_versions (
                            service_name,
                            config_key,
                            old_value_json,
                            new_value_json,
                            changed_by,
                            change_reason,
                            change_source,
                            request_id,
                            is_secret,
                            requires_restart,
                            changed_at
                        )
                        VALUES (
                            $1,
                            $2,
                            $3::jsonb,
                            $4::jsonb,
                            $5,
                            $6,
                            'bootstrap_wizard',
                            $7,
                            FALSE,
                            $8,
                            NOW()
                        )
                        """,
                        service_name,
                        config_key,
                        json.dumps(old_value) if old_value is not None else None,
                        value_json,
                        actor_id,
                        update_reason,
                        request_id,
                        requires_restart,
                    )

                    old_display = "(unset)" if old_value is None else json.dumps(old_value)
                    new_display = json.dumps(value)
                    await conn.execute(
                        """
                        INSERT INTO collector.control_change_log (
                            event_type,
                            service_name,
                            config_key,
                            actor_id,
                            actor_role,
                            event_source,
                            request_id,
                            old_value_masked,
                            new_value_masked,
                            reason,
                            metadata,
                            created_at
                        )
                        VALUES (
                            'bootstrap_config_applied',
                            $1,
                            $2,
                            $3,
                            $4,
                            'dashboard',
                            $5,
                            $6,
                            $7,
                            $8,
                            $9::jsonb,
                            NOW()
                        )
                        """,
                        service_name,
                        config_key,
                        actor_id,
                        actor_role,
                        request_id,
                        old_display,
                        new_display,
                        update_reason,
                        json.dumps(
                            {
                                "scope": scope,
                                "requires_restart": requires_restart,
                                "source": "bootstrap_wizard",
                            }
                        ),
                    )

                await conn.execute(
                    """
                    UPDATE collector.control_bootstrap_state
                    SET state = 'initialized',
                        wizard_version = $1,
                        generated_defaults = $2::jsonb,
                        initialized_by = $3,
                        initialized_at = NOW(),
                        updated_at = NOW()
                    WHERE singleton_id = 1
                    """,
                    version,
                    defaults_json,
                    actor_id,
                )

                await conn.execute(
                    """
                    INSERT INTO collector.control_change_log (
                        event_type,
                        service_name,
                        config_key,
                        actor_id,
                        actor_role,
                        event_source,
                        request_id,
                        old_value_masked,
                        new_value_masked,
                        reason,
                        metadata,
                        created_at
                    )
                    VALUES (
                        'bootstrap_initialized',
                        'collector',
                        'bootstrap_state',
                        $1,
                        $2,
                        'dashboard',
                        $3,
                        $4,
                        $5,
                        $6,
                        $7::jsonb,
                        NOW()
                    )
                    """,
                    actor_id,
                    actor_role,
                    request_id,
                    current_state,
                    "initialized",
                    reason,
                    json.dumps(
                        {
                            "wizard_version": version,
                            "baseline_config_entries": len(config_values),
                        }
                    ),
                )

                final_row = await conn.fetchrow(
                    """
                    SELECT state, wizard_version, generated_defaults,
                           initialized_by, initialized_at, updated_at
                    FROM collector.control_bootstrap_state
                    WHERE singleton_id = 1
                    """
                )

        return {
            "state": final_row["state"],
            "wizard_version": final_row["wizard_version"],
            "generated_defaults": final_row["generated_defaults"] or {},
            "initialized_by": final_row["initialized_by"],
            "initialized_at": final_row["initialized_at"],
            "updated_at": final_row["updated_at"],
        }

    @with_db_retry()
    async def upsert_control_secret(
        self,
        *,
        service_name: str,
        secret_key: str,
        plaintext_value: str,
        updated_by: str | None = None,
        update_reason: str | None = None,
        actor_role: str | None = None,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Encrypt and persist a control-plane secret value.

        The plaintext is never written to the database. AES-GCM associated data
        binds ciphertext to (service_name, secret_key).
        """
        service = (service_name or "").strip()
        key = (secret_key or "").strip()
        if not service:
            raise ValueError("service_name is required")
        if not key:
            raise ValueError("secret_key is required")

        self._require_mutation_authorization(updated_by, actor_role, minimum_role="operator")

        cipher = self._get_secret_cipher()
        aad = f"{service}:{key}".encode("utf-8")
        encrypted = cipher.encrypt(plaintext_value, associated_data=aad)
        meta_json = json.dumps(metadata or {})

        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO collector.control_secret_values (
                    service_name,
                    secret_key,
                    ciphertext,
                    nonce,
                    auth_tag,
                    encryption_key_id,
                    metadata,
                    updated_by,
                    update_reason,
                    updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, NOW())
                ON CONFLICT (service_name, secret_key)
                DO UPDATE SET
                    ciphertext = EXCLUDED.ciphertext,
                    nonce = EXCLUDED.nonce,
                    auth_tag = EXCLUDED.auth_tag,
                    encryption_key_id = EXCLUDED.encryption_key_id,
                    metadata = EXCLUDED.metadata,
                    updated_by = EXCLUDED.updated_by,
                    update_reason = EXCLUDED.update_reason,
                    updated_at = NOW()
                """,
                service,
                key,
                encrypted.ciphertext,
                encrypted.nonce,
                encrypted.auth_tag,
                cipher.key_id,
                meta_json,
                updated_by,
                update_reason,
            )
            await conn.execute(
                """
                INSERT INTO collector.control_change_log (
                    event_type,
                    service_name,
                    config_key,
                    actor_id,
                    actor_role,
                    event_source,
                    request_id,
                    old_value_masked,
                    new_value_masked,
                    reason,
                    metadata,
                    created_at
                )
                VALUES (
                    'secret_updated',
                    $1,
                    $2,
                    $3,
                    $4,
                    'dashboard',
                    $5,
                    $6,
                    $7,
                    $8,
                    $9::jsonb,
                    NOW()
                )
                """,
                service,
                key,
                updated_by,
                actor_role,
                request_id,
                masked_secret("previous"),
                masked_secret(plaintext_value),
                update_reason,
                meta_json,
            )

    @with_db_retry()
    async def get_control_secret(
        self,
        service_name: str,
        secret_key: str,
    ) -> dict[str, Any] | None:
        """Return secret metadata only (never plaintext)."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    service_name,
                    secret_key,
                    encryption_key_id,
                    metadata,
                    updated_by,
                    update_reason,
                    updated_at
                FROM collector.control_secret_values
                WHERE service_name = $1 AND secret_key = $2
                """,
                service_name,
                secret_key,
            )

        if row is None:
            return None

        return {
            "service_name": row["service_name"],
            "secret_key": row["secret_key"],
            "encryption_key_id": row["encryption_key_id"],
            "metadata": row["metadata"] or {},
            "updated_by": row["updated_by"],
            "update_reason": row["update_reason"],
            "updated_at": row["updated_at"],
            "value_masked": masked_secret("set"),
        }

    @with_db_retry()
    async def get_control_secret_plaintext(
        self,
        service_name: str,
        secret_key: str,
    ) -> str | None:
        """Explicitly decrypt and return plaintext for an existing secret."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT ciphertext, nonce, auth_tag
                FROM collector.control_secret_values
                WHERE service_name = $1 AND secret_key = $2
                """,
                service_name,
                secret_key,
            )

        if row is None:
            return None

        payload = EncryptedSecret(
            ciphertext=bytes(row["ciphertext"]),
            nonce=bytes(row["nonce"]),
            auth_tag=bytes(row["auth_tag"]),
        )
        aad = f"{service_name}:{secret_key}".encode("utf-8")
        return self._get_secret_cipher().decrypt(payload, associated_data=aad)

    @with_db_retry()
    async def list_control_secrets(self, service_name: str) -> list[dict[str, Any]]:
        """List secret records for a service with metadata-only payloads."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    service_name,
                    secret_key,
                    encryption_key_id,
                    metadata,
                    updated_by,
                    update_reason,
                    updated_at
                FROM collector.control_secret_values
                WHERE service_name = $1
                ORDER BY secret_key ASC
                """,
                service_name,
            )

        results: list[dict[str, Any]] = []
        for row in rows:
            results.append(
                {
                    "service_name": row["service_name"],
                    "secret_key": row["secret_key"],
                    "encryption_key_id": row["encryption_key_id"],
                    "metadata": row["metadata"] or {},
                    "updated_by": row["updated_by"],
                    "update_reason": row["update_reason"],
                    "updated_at": row["updated_at"],
                    "value_masked": masked_secret("set"),
                }
            )
        return results

    @with_db_retry()
    async def insert_control_change_log_event(
        self,
        *,
        event_type: str,
        service_name: str | None,
        config_key: str | None,
        actor_id: str | None,
        actor_role: str | None,
        request_id: str | None,
        old_value_masked: str | None,
        new_value_masked: str | None,
        reason: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Insert an immutable control-plane audit event."""
        self._require_mutation_authorization(actor_id, actor_role, minimum_role="operator")

        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO collector.control_change_log (
                    event_type,
                    service_name,
                    config_key,
                    actor_id,
                    actor_role,
                    event_source,
                    request_id,
                    old_value_masked,
                    new_value_masked,
                    reason,
                    metadata,
                    created_at
                )
                VALUES (
                    $1,
                    $2,
                    $3,
                    $4,
                    $5,
                    'dashboard',
                    $6,
                    $7,
                    $8,
                    $9,
                    $10::jsonb,
                    NOW()
                )
                """,
                event_type,
                service_name,
                config_key,
                actor_id,
                actor_role,
                request_id,
                old_value_masked,
                new_value_masked,
                reason,
                json.dumps(metadata or {}),
            )


database = Database()
