"""Pruning candidate computation for the collector dashboard."""
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PruneCandidate:
    raw_message_id: int          # collector.raw_messages.id (BIGSERIAL)
    chat_id: int                 # collector.raw_messages.chat_id
    message_id: int              # collector.raw_messages.message_id
    file_unique_id: str | None   # collector.raw_messages.file_unique_id
    media_path: str | None       # collector.raw_messages.media_path (symlink path)


@dataclass
class PruneResult:
    total_candidates: int
    symlinks_deleted: int
    files_deleted: int           # by_id files actually removed
    files_skipped: int           # by_id files kept (still referenced)
    db_rows_deleted: int
    errors: list[str] = field(default_factory=list)


def compute_min_cursor(conn) -> int | None:
    """Return the minimum last_message_id across all active services.

    Returns None if the result is NULL (no active services) or zero
    (no service has consumed any messages yet).
    """
    sql = """
        SELECT MIN(sc.last_message_id) AS min_cursor
        FROM collector.service_cursors sc
        JOIN collector.service_registry sr ON sr.service_name = sc.service_name
        WHERE sr.is_active = TRUE
    """
    cur = conn.cursor()
    try:
        cur.execute(sql)
        row = cur.fetchone()
    finally:
        cur.close()

    if row is None or row[0] is None or row[0] == 0:
        return None
    return int(row[0])


def compute_prunable_set(message_ids: list[int], min_cursor: int) -> set[int]:
    """Return the set of message IDs that are safe to prune.

    Pure function — no DB access.  A message is prunable when its id is
    less than or equal to *min_cursor*.
    """
    return {mid for mid in message_ids if mid <= min_cursor}


def fetch_prune_candidates(conn, min_cursor: int) -> list[PruneCandidate]:
    """Fetch all raw_messages rows whose id <= min_cursor, ordered by id."""
    sql = """
        SELECT id, chat_id, message_id, file_unique_id, media_path
        FROM collector.raw_messages
        WHERE id <= %s
        ORDER BY id
    """
    cur = conn.cursor()
    try:
        cur.execute(sql, (min_cursor,))
        rows = cur.fetchall()
    finally:
        cur.close()

    return [
        PruneCandidate(
            raw_message_id=row[0],
            chat_id=row[1],
            message_id=row[2],
            file_unique_id=row[3],
            media_path=row[4],
        )
        for row in rows
    ]


def estimate_disk_recovery(candidates: list[PruneCandidate], media_store_path: str) -> int:
    """Estimate bytes that would be freed by pruning *candidates*.

    Follows each symlink to the real ``by_id`` file and sums sizes.
    Each ``file_unique_id`` is counted only once (deduplication).
    Files that no longer exist are silently skipped.
    """
    seen_file_unique_ids: set[str] = set()
    total_bytes = 0

    for candidate in candidates:
        if candidate.media_path is None:
            continue
        fuid = candidate.file_unique_id
        if fuid is not None:
            if fuid in seen_file_unique_ids:
                continue
            seen_file_unique_ids.add(fuid)
        try:
            real_path = os.path.realpath(candidate.media_path)
            total_bytes += os.path.getsize(real_path)
        except OSError:
            pass

    return total_bytes


def execute_prune(conn, candidates: list[PruneCandidate], media_store_path: str) -> PruneResult:
    """Execute the pruning operation for the given candidates.

    Steps per candidate (in ascending id order):
    4a. Delete the by_message symlink
    4b. Check file_unique_id reference count (before deleting DB row)
    4c. Conditionally delete the by_id file (only if ref_count == 0)
    4d. DELETE the DB row
    """
    result = PruneResult(
        total_candidates=len(candidates),
        symlinks_deleted=0,
        files_deleted=0,
        files_skipped=0,
        db_rows_deleted=0,
    )

    for candidate in sorted(candidates, key=lambda c: c.raw_message_id):
        # 4a: Delete the by_message symlink
        symlink_path = f"{media_store_path}/by_message/{candidate.chat_id}/{candidate.message_id}"
        if os.path.islink(symlink_path):
            try:
                os.unlink(symlink_path)
                result.symlinks_deleted += 1
            except OSError as e:
                logger.warning("Failed to unlink %s: %s", symlink_path, e)
                result.errors.append(f"unlink {symlink_path}: {e}")

        # 4b: Check file_unique_id reference count (BEFORE deleting DB row)
        ref_count = 0
        if candidate.file_unique_id is not None:
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM collector.raw_messages "
                    "WHERE file_unique_id = %s AND id != %s",
                    (candidate.file_unique_id, candidate.raw_message_id),
                )
                ref_count = cur.fetchone()[0]
                cur.close()
            except Exception as e:
                logger.warning("ref_count query failed for %s: %s", candidate.file_unique_id, e)
                result.errors.append(f"ref_count {candidate.file_unique_id}: {e}")
                ref_count = 1  # Assume referenced on error — safe default

        # 4c: Conditionally delete the by_id file
        if ref_count == 0 and candidate.file_unique_id is not None:
            by_id_path = f"{media_store_path}/by_id/{candidate.file_unique_id}"
            if os.path.exists(by_id_path):
                try:
                    os.remove(by_id_path)
                    result.files_deleted += 1
                except OSError as e:
                    logger.warning("Failed to remove %s: %s", by_id_path, e)
                    result.errors.append(f"remove {by_id_path}: {e}")
        elif candidate.file_unique_id is not None:
            result.files_skipped += 1

        # 4d: DELETE the DB row
        try:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM collector.raw_messages WHERE id = %s",
                (candidate.raw_message_id,),
            )
            conn.commit()
            cur.close()
            result.db_rows_deleted += 1
        except Exception as e:
            logger.warning("Failed to delete row %s: %s", candidate.raw_message_id, e)
            result.errors.append(f"delete row {candidate.raw_message_id}: {e}")

    return result
