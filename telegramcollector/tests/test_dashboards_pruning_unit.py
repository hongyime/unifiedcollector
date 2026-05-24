"""Unit tests for pruning execution logic.

Requirements: 7.5, 7.6
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.collector.dashboard.pruning import (
    PruneCandidate,
    execute_prune,
    fetch_prune_candidates,
)


def _make_candidate(raw_message_id=1, chat_id=100, message_id=200,
                    file_unique_id="abc123", media_path="/mnt/media/by_message/100/200"):
    return PruneCandidate(
        raw_message_id=raw_message_id,
        chat_id=chat_id,
        message_id=message_id,
        file_unique_id=file_unique_id,
        media_path=media_path,
    )


def test_pruning_candidate_query():
    """Mock DB cursor — verify SQL returns only rows with id <= min_cursor."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_cur.fetchall.return_value = [
        (1, 100, 200, "abc123", "/mnt/media/by_message/100/200"),
        (2, 101, 201, "def456", "/mnt/media/by_message/101/201"),
    ]

    candidates = fetch_prune_candidates(mock_conn, min_cursor=5)

    mock_cur.execute.assert_called_once()
    call_args = mock_cur.execute.call_args
    sql = call_args[0][0]
    params = call_args[0][1]
    assert "WHERE id <=" in sql or "WHERE id<=" in sql.replace(" ", "")
    assert params == (5,)
    assert len(candidates) == 2
    assert candidates[0].raw_message_id == 1
    assert candidates[1].raw_message_id == 2


def test_pruning_ref_count_check():
    """Mock DB — verify ref_count query uses correct file_unique_id and excludes candidate row."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_cur.fetchone.return_value = (0,)

    candidate = _make_candidate(raw_message_id=42, file_unique_id="xyz789")

    with patch("os.path.islink", return_value=False), \
         patch("os.path.exists", return_value=False):
        execute_prune(mock_conn, [candidate], "/mnt/media")

    ref_count_calls = [
        c for c in mock_cur.execute.call_args_list
        if "file_unique_id" in str(c) and "COUNT" in str(c)
    ]
    assert len(ref_count_calls) >= 1
    ref_call = ref_count_calls[0]
    sql = ref_call[0][0]
    params = ref_call[0][1]
    assert "file_unique_id" in sql
    assert "xyz789" in params
    assert 42 in params


def test_pruning_skips_by_id_when_referenced():
    """Mock filesystem + DB — verify os.remove not called when ref_count > 0."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_cur.fetchone.return_value = (2,)

    candidate = _make_candidate(raw_message_id=10, file_unique_id="shared_file")

    with patch("os.path.islink", return_value=False), \
         patch("os.path.exists", return_value=True), \
         patch("os.remove") as mock_remove:
        result = execute_prune(mock_conn, [candidate], "/mnt/media")

    mock_remove.assert_not_called()
    assert result.files_skipped == 1
    assert result.files_deleted == 0


def test_pruning_deletes_by_id_when_unreferenced():
    """Mock filesystem + DB — verify os.remove called when ref_count == 0."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_cur.fetchone.return_value = (0,)

    candidate = _make_candidate(raw_message_id=10, file_unique_id="unique_file")

    with patch("os.path.islink", return_value=False), \
         patch("os.path.exists", return_value=True), \
         patch("os.remove") as mock_remove:
        result = execute_prune(mock_conn, [candidate], "/mnt/media")

    mock_remove.assert_called_once_with("/mnt/media/by_id/unique_file")
    assert result.files_deleted == 1
    assert result.files_skipped == 0
