from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Sequence

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)


def mask_value(value: str | None) -> str | None:
    """Deterministically masks a value for UI/audit display."""
    if value is None:
        return None
    text = str(value)
    if text == "":
        return ""
    if len(text) <= 2:
        return "*" * len(text)
    if len(text) <= 6:
        return text[0] + ("*" * (len(text) - 2)) + text[-1]
    return text[:2] + ("*" * (len(text) - 4)) + text[-2:]


def hash_value(value: str | None) -> str | None:
    """Returns a stable SHA-256 hash for audit diffing."""
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PersistResult:
    persisted: bool
    revision_id: int | None = None
    error: str | None = None


class ConfigStore:
    """Database-backed configuration persistence with audit trail."""

    def __init__(self, retry_cooldown_seconds: int = 30) -> None:
        self.retry_cooldown_seconds = retry_cooldown_seconds
        self._next_retry_ts = 0.0

    def _can_query_db(self) -> bool:
        return time.time() >= self._next_retry_ts

    def _mark_db_failure(self, exc: Exception) -> None:
        self._next_retry_ts = time.time() + self.retry_cooldown_seconds
        logger.warning(
            "ConfigStore DB unavailable (%s). Retrying after %ss.",
            type(exc).__name__,
            self.retry_cooldown_seconds,
        )

    def _encryption_key(self) -> str:
        """Returns the symmetric key used for pgcrypto secret encryption."""
        key = os.getenv("CONFIG_STORE_ENCRYPTION_KEY")
        if key and key.strip():
            return key
        db_password = os.getenv("DB_PASSWORD")
        if db_password and db_password.strip():
            return db_password
        return "telegramcollector-config-fallback-key"

    def _connect(self) -> psycopg.Connection:
        """Creates a short-timeout sync DB connection for dashboard writes."""
        from shared.config import settings

        return psycopg.connect(
            host=settings.DB_HOST,
            port=settings.DB_PORT,
            dbname=settings.DB_NAME,
            user=settings.DB_USER,
            password=settings.DB_PASSWORD,
            connect_timeout=1,
            autocommit=True,
            row_factory=dict_row,
        )

    def _fetch_existing_value(self, cur: psycopg.Cursor, key: str) -> tuple[bool, str | None]:
        cur.execute(
            """
            SELECT
                is_sensitive,
                CASE
                    WHEN is_sensitive THEN pgp_sym_decrypt(value_encrypted, %s)::text
                    ELSE value_plain
                END AS current_value
            FROM collector.config_settings
            WHERE config_key = %s
            """,
            (self._encryption_key(), key),
        )
        row = cur.fetchone()
        if not row:
            return False, None
        return bool(row["is_sensitive"]), row["current_value"]

    def get_setting(self, key: str) -> str | None:
        """Returns the persisted effective setting value, if available."""
        if not self._can_query_db():
            return None
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            CASE
                                WHEN is_sensitive THEN pgp_sym_decrypt(value_encrypted, %s)::text
                                ELSE value_plain
                            END AS effective_value
                        FROM collector.config_settings
                        WHERE config_key = %s
                        """,
                        (self._encryption_key(), key),
                    )
                    row = cur.fetchone()
                    return None if row is None else row["effective_value"]
        except Exception as exc:
            self._mark_db_failure(exc)
            return None

    def get_settings_snapshot(self, keys: Sequence[str]) -> dict[str, str]:
        """Returns persisted values for the provided keys."""
        if not keys or not self._can_query_db():
            return {}
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            config_key,
                            CASE
                                WHEN is_sensitive THEN pgp_sym_decrypt(value_encrypted, %s)::text
                                ELSE value_plain
                            END AS effective_value
                        FROM collector.config_settings
                        WHERE config_key = ANY(%s)
                        """,
                        (self._encryption_key(), list(keys)),
                    )
                    rows = cur.fetchall()
                    return {
                        str(r["config_key"]): str(r["effective_value"])
                        for r in rows
                        if r.get("effective_value") is not None
                    }
        except Exception as exc:
            self._mark_db_failure(exc)
            return {}

    def get_revision_count(self) -> int | None:
        """Returns config revision count, or None when DB is unavailable."""
        if not self._can_query_db():
            return None
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('collector.config_revisions') AS rel")
                    table_row = cur.fetchone()
                    if not table_row or table_row.get("rel") is None:
                        return 0

                    cur.execute("SELECT COUNT(*) AS revision_count FROM collector.config_revisions")
                    row = cur.fetchone()
                    return int(row["revision_count"]) if row else 0
        except Exception as exc:
            self._mark_db_failure(exc)
            return None

    def is_first_run(self) -> bool | None:
        """Returns True when no config revisions exist, None when unknown."""
        revision_count = self.get_revision_count()
        if revision_count is None:
            return None
        return revision_count == 0

    def persist_setting(
        self,
        *,
        key: str,
        value: str,
        group: str,
        sensitive: bool,
        changed_by: str,
        source: str,
        live_applied: bool,
        restart_required: bool,
        owners: tuple[str, ...] | None,
    ) -> PersistResult:
        """Persists one setting change with revision + audit entries."""
        if not self._can_query_db():
            return PersistResult(False, error="config store cooldown active")

        value_text = str(value)
        actor = (changed_by or "dashboard").strip() or "dashboard"
        cfg_source = (source or "dashboard").strip() or "dashboard"

        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    _old_sensitive, old_value = self._fetch_existing_value(cur, key)

                    cur.execute(
                        """
                        INSERT INTO collector.config_revisions (changed_by, source, notes)
                        VALUES (%s, %s, %s)
                        RETURNING id
                        """,
                        (actor, cfg_source, f"{group}.{key}"),
                    )
                    revision_row = cur.fetchone()
                    revision_id = int(revision_row["id"])

                    cur.execute(
                        """
                        INSERT INTO collector.config_settings (
                            config_key,
                            group_name,
                            value_plain,
                            value_encrypted,
                            is_sensitive,
                            source,
                            updated_by,
                            revision_id,
                            updated_at
                        )
                        VALUES (
                            %s,
                            %s,
                            CASE WHEN %s THEN NULL ELSE %s END,
                            CASE WHEN %s THEN pgp_sym_encrypt(%s, %s) ELSE NULL END,
                            %s,
                            %s,
                            %s,
                            %s,
                            NOW()
                        )
                        ON CONFLICT (config_key) DO UPDATE
                        SET
                            group_name = EXCLUDED.group_name,
                            value_plain = EXCLUDED.value_plain,
                            value_encrypted = EXCLUDED.value_encrypted,
                            is_sensitive = EXCLUDED.is_sensitive,
                            source = EXCLUDED.source,
                            updated_by = EXCLUDED.updated_by,
                            revision_id = EXCLUDED.revision_id,
                            updated_at = NOW()
                        """,
                        (
                            key,
                            group,
                            sensitive,
                            value_text,
                            sensitive,
                            value_text,
                            self._encryption_key(),
                            sensitive,
                            cfg_source,
                            actor,
                            revision_id,
                        ),
                    )

                    affected_services = list(owners or ())
                    cur.execute(
                        """
                        INSERT INTO collector.config_audit_log (
                            config_key,
                            group_name,
                            old_value_masked,
                            new_value_masked,
                            old_value_hash,
                            new_value_hash,
                            changed_by,
                            source,
                            live_applied,
                            restart_required,
                            affected_services,
                            revision_id,
                            created_at
                        )
                        VALUES (
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            NOW()
                        )
                        """,
                        (
                            key,
                            group,
                            mask_value(old_value),
                            mask_value(value_text),
                            hash_value(old_value),
                            hash_value(value_text),
                            actor,
                            cfg_source,
                            live_applied,
                            restart_required,
                            affected_services,
                            revision_id,
                        ),
                    )

            return PersistResult(True, revision_id=revision_id)
        except Exception as exc:
            self._mark_db_failure(exc)
            return PersistResult(False, error=str(exc))


config_store = ConfigStore()
