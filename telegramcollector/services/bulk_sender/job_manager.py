"""
JobManager — synchronous Postgres persistence layer for the Bulk Sender Service.

All SQL I/O for bulk_sender.send_jobs and bulk_sender.sent_items lives here.
No business logic; pure persistence.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import psycopg2
import psycopg2.extras
import psycopg2.pool

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class JobManager:
    """Synchronous Postgres persistence layer for bulk sender jobs.

    Uses a :class:`psycopg2.pool.ThreadedConnectionPool` so the instance can
    safely be shared between the job-runner thread and the Streamlit dashboard
    process (each call borrows a connection, executes, then returns it).
    """

    def __init__(self, dsn: str) -> None:
        """Open a synchronous psycopg2 connection pool to Postgres.

        Args:
            dsn: A libpq connection string, e.g.
                 ``"postgresql://user:pass@host:5432/dbname"``

        The pool is created with ``minconn=1`` and ``maxconn=10``.
        """
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=dsn,
        )
        logger.debug("JobManager: connection pool opened (minconn=1, maxconn=10)")

    # ------------------------------------------------------------------
    # Job CRUD
    # ------------------------------------------------------------------

    def create_job(
        self,
        account_id: int,
        target_chat_id: int,
        source_type: str,
        source_path: str | None,
        collector_query: dict | None,
        total_files: int,
    ) -> int:
        """Insert a new Send_Job with ``status='pending'`` and return its id.

        Args:
            account_id: ID of the Telegram account that will send the files.
            target_chat_id: Numeric Telegram chat ID of the destination chat.
            source_type: ``'folder'`` or ``'collector_query'``.
            source_path: HDD folder path (required when source_type='folder').
            collector_query: JSONB filter dict (required when
                source_type='collector_query').
            total_files: Pre-computed count of files to send.

        Returns:
            The ``id`` of the newly created ``bulk_sender.send_jobs`` row.
        """
        sql = """
            INSERT INTO bulk_sender.send_jobs (
                account_id, target_chat_id, source_type,
                source_path, collector_query, status, total_files
            ) VALUES (
                %(account_id)s, %(target_chat_id)s, %(source_type)s,
                %(source_path)s, %(collector_query)s, 'pending', %(total_files)s
            )
            RETURNING id;
        """
        params = {
            "account_id": account_id,
            "target_chat_id": target_chat_id,
            "source_type": source_type,
            "source_path": source_path,
            "collector_query": psycopg2.extras.Json(collector_query),
            "total_files": total_files,
        }
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
            conn.commit()
            new_id: int = row[0]
            logger.debug("JobManager.create_job: created job id=%d", new_id)
            return new_id
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def get_pending_jobs(self) -> list[dict]:
        """Return all jobs with ``status='pending'`` ordered by ``created_at ASC``.

        Returns:
            A list of dicts, each containing all columns of
            ``bulk_sender.send_jobs``.
        """
        sql = """
            SELECT *
            FROM bulk_sender.send_jobs
            WHERE status = 'pending'
            ORDER BY created_at ASC;
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                rows = cur.fetchall()
            conn.commit()
            return [dict(r) for r in rows]
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def get_job(self, job_id: int) -> dict | None:
        """Return a single ``send_jobs`` row as a dict, or ``None`` if not found.

        Args:
            job_id: Primary key of the job to fetch.
        """
        sql = "SELECT * FROM bulk_sender.send_jobs WHERE id = %(job_id)s;"
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, {"job_id": job_id})
                row = cur.fetchone()
            conn.commit()
            return dict(row) if row is not None else None
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    # ------------------------------------------------------------------
    # Status / progress updates
    # ------------------------------------------------------------------

    def set_status(self, job_id: int, status: str) -> None:
        """Update ``status`` and ``updated_at`` for a job.

        Valid statuses: ``pending``, ``running``, ``paused``, ``complete``,
        ``failed``, ``cancelled``.

        Args:
            job_id: Primary key of the job to update.
            status: New status string.
        """
        sql = """
            UPDATE bulk_sender.send_jobs
            SET    status = %(status)s,
                   updated_at = NOW()
            WHERE  id = %(job_id)s;
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, {"job_id": job_id, "status": status})
            conn.commit()
            logger.debug("JobManager.set_status: job_id=%d status=%s", job_id, status)
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def increment_sent(self, job_id: int) -> None:
        """Atomically increment ``sent_count`` by 1 and refresh ``updated_at``.

        Args:
            job_id: Primary key of the job to update.
        """
        sql = """
            UPDATE bulk_sender.send_jobs
            SET    sent_count = sent_count + 1,
                   updated_at = NOW()
            WHERE  id = %(job_id)s;
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, {"job_id": job_id})
            conn.commit()
            logger.debug("JobManager.increment_sent: job_id=%d", job_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def record_sent_item(
        self,
        job_id: int,
        file_path: str,
        file_hash: str,
        telegram_message_id: int,
    ) -> None:
        """Insert a row into ``bulk_sender.sent_items`` (idempotent).

        Uses ``ON CONFLICT (job_id, file_hash) DO NOTHING`` so calling this
        method twice for the same ``(job_id, file_hash)`` pair is safe.

        Args:
            job_id: FK to ``bulk_sender.send_jobs.id``.
            file_path: Absolute path of the file that was sent.
            file_hash: SHA-256 hex digest of the file's raw bytes.
            telegram_message_id: Message ID returned by the Telegram API.
        """
        sql = """
            INSERT INTO bulk_sender.sent_items (
                job_id, file_path, file_hash, sent_at, telegram_message_id
            ) VALUES (
                %(job_id)s, %(file_path)s, %(file_hash)s, NOW(), %(telegram_message_id)s
            )
            ON CONFLICT (job_id, file_hash) DO NOTHING;
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, {
                    "job_id": job_id,
                    "file_path": file_path,
                    "file_hash": file_hash,
                    "telegram_message_id": telegram_message_id,
                })
            conn.commit()
            logger.debug(
                "JobManager.record_sent_item: job_id=%d file_hash=%s", job_id, file_hash
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def is_already_sent(self, job_id: int, file_hash: str) -> bool:
        """Return ``True`` if ``(job_id, file_hash)`` exists in ``sent_items``.

        Args:
            job_id: FK to ``bulk_sender.send_jobs.id``.
            file_hash: SHA-256 hex digest to look up.
        """
        sql = """
            SELECT 1
            FROM   bulk_sender.sent_items
            WHERE  job_id    = %(job_id)s
            AND    file_hash = %(file_hash)s
            LIMIT  1;
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, {"job_id": job_id, "file_hash": file_hash})
                row = cur.fetchone()
            conn.commit()
            return row is not None
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    # ------------------------------------------------------------------
    # Startup recovery
    # ------------------------------------------------------------------

    def recover_orphaned_jobs(self) -> int:
        """Set all ``status='running'`` jobs to ``status='paused'``.

        Called once at service startup before the job runner loop begins, to
        handle jobs that were interrupted by an unclean container shutdown.

        Returns:
            The number of rows updated (0 if no orphaned jobs were found).
        """
        sql = """
            UPDATE bulk_sender.send_jobs
            SET    status = 'paused',
                   updated_at = NOW()
            WHERE  status = 'running';
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                count = cur.rowcount
            conn.commit()
            logger.debug("JobManager.recover_orphaned_jobs: recovered %d job(s)", count)
            return count
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    # ------------------------------------------------------------------
    # File-list helpers
    # ------------------------------------------------------------------

    def resolve_file_list(self, job: dict) -> list[str]:
        """Return the ordered list of file paths for a job.

        Dispatch logic:
        - ``source_type='folder'``: delegates to
          :meth:`~services.bulk_sender.sender.Sender._get_file_list`.
        - ``source_type='collector_query'``: runs
          :meth:`~services.bulk_sender.sender.Sender._build_collector_query`
          and returns the ``media_path`` values from
          ``collector.raw_messages``.

        Used at job creation time to compute ``total_files``.

        Args:
            job: A ``send_jobs`` row dict (as returned by :meth:`get_job`).

        Returns:
            Ordered list of absolute file paths.
        """
        if job["source_type"] == "folder":
            # Lazy import to avoid circular imports (Sender imports JobManager)
            from services.bulk_sender.sender import Sender  # noqa: PLC0415
            sender_instance = Sender.__new__(Sender)
            return sender_instance._get_file_list(job["source_path"])

        # source_type == 'collector_query'
        cq = job.get("collector_query") or {}
        sql = """
            SELECT media_path
            FROM   collector.raw_messages
            WHERE  media_path IS NOT NULL
              AND  (%(chat_id)s    IS NULL OR chat_id      = %(chat_id)s)
              AND  (%(date_from)s  IS NULL OR collected_at >= %(date_from)s)
              AND  (%(date_to)s    IS NULL OR collected_at <= %(date_to)s)
              AND  message_type = COALESCE(%(message_type)s, 'photo')
              AND  (%(sender_id)s  IS NULL OR sender_id    = %(sender_id)s)
            ORDER BY media_path ASC;
        """
        params = {
            "chat_id": cq.get("chat_id"),
            "date_from": cq.get("date_from"),
            "date_to": cq.get("date_to"),
            "message_type": cq.get("message_type"),
            "sender_id": cq.get("sender_id"),
        }
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
            conn.commit()
            return [row[0] for row in rows]
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def count_collector_query(self, collector_query: dict) -> int:
        """Execute a ``COUNT(*)`` against ``collector.raw_messages``.

        Used by the dashboard to show a preview count before the operator
        confirms job creation.

        Args:
            collector_query: Filter dict with optional keys ``chat_id``,
                ``date_from``, ``date_to``, ``message_type``, ``sender_id``.

        Returns:
            Integer row count matching the supplied filters.
        """
        sql = """
            SELECT COUNT(*)
            FROM   collector.raw_messages
            WHERE  media_path IS NOT NULL
              AND  (%(chat_id)s    IS NULL OR chat_id      = %(chat_id)s)
              AND  (%(date_from)s  IS NULL OR collected_at >= %(date_from)s)
              AND  (%(date_to)s    IS NULL OR collected_at <= %(date_to)s)
              AND  message_type = COALESCE(%(message_type)s, 'photo')
              AND  (%(sender_id)s  IS NULL OR sender_id    = %(sender_id)s);
        """
        params = {
            "chat_id": collector_query.get("chat_id"),
            "date_from": collector_query.get("date_from"),
            "date_to": collector_query.get("date_to"),
            "message_type": collector_query.get("message_type"),
            "sender_id": collector_query.get("sender_id"),
        }
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
            conn.commit()
            return int(row[0])
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)
