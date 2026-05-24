"""
Tests for P2.1: Remove explicit conn.commit() calls from corrections.py and story_scanner.py.

Validates: Requirements 2.10 (no explicit commit on autocommit connection)
           Requirements 3.7 (backward compatibility preserved)
"""
import ast
import inspect
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Static analysis: verify no conn.commit() calls exist in source files
# ---------------------------------------------------------------------------

def _find_commit_calls(filepath: str) -> list[int]:
    """Return line numbers of any conn.commit() calls in the given file."""
    with open(filepath) as f:
        source = f.read()
    tree = ast.parse(source)
    lines = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "commit"
        ):
            lines.append(node.lineno)
    return lines


class TestNoCommitCallsInSource:
    """Static check: conn.commit() must not appear in corrections.py or story_scanner.py."""

    def test_corrections_has_no_commit_calls(self):
        lines = _find_commit_calls("collector/corrections.py")
        assert lines == [], (
            f"collector/corrections.py still has conn.commit() calls at lines: {lines}"
        )

    def test_story_scanner_has_no_commit_calls(self):
        lines = _find_commit_calls("story_scanner.py")
        assert lines == [], (
            f"story_scanner.py still has conn.commit() calls at lines: {lines}"
        )


# ---------------------------------------------------------------------------
# Functional tests: operations work without explicit commit (no ProgrammingError)
# ---------------------------------------------------------------------------

def _make_mock_conn():
    """Build a minimal async context-manager mock for get_db_connection()."""
    mock_cursor = AsyncMock()
    mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
    mock_cursor.__aexit__ = AsyncMock(return_value=False)
    mock_cursor.execute = AsyncMock()
    mock_cursor.fetchone = AsyncMock(return_value=(99,))

    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    # Simulate autocommit=True: calling commit() raises ProgrammingError
    mock_conn.commit = AsyncMock(
        side_effect=Exception("can't call commit() in autocommit mode")
    )
    return mock_conn, mock_cursor


class TestMergeIdentitiesNoCommit:
    """merge_identities() must complete without calling commit."""

    @pytest.mark.asyncio
    async def test_merge_does_not_raise_programming_error(self):
        from services.collector.corrections import CorrectionHandler

        mock_conn, _ = _make_mock_conn()
        handler = CorrectionHandler()

        with patch("services.collector.corrections.get_db_connection", return_value=mock_conn):
            # Should not raise even though mock_conn.commit() would raise
            result = await handler.merge_identities(
                source_topic_id=1, target_topic_id=2
            )

        assert result is True

    @pytest.mark.asyncio
    async def test_merge_never_calls_commit(self):
        from services.collector.corrections import CorrectionHandler

        mock_conn, _ = _make_mock_conn()
        handler = CorrectionHandler()

        with patch("services.collector.corrections.get_db_connection", return_value=mock_conn):
            await handler.merge_identities(source_topic_id=3, target_topic_id=4)

        mock_conn.commit.assert_not_awaited()


class TestRenameIdentityNoCommit:
    """rename_identity() must complete without calling commit."""

    @pytest.mark.asyncio
    async def test_rename_does_not_raise_programming_error(self):
        from services.collector.corrections import CorrectionHandler

        mock_conn, _ = _make_mock_conn()
        handler = CorrectionHandler()

        with patch("services.collector.corrections.get_db_connection", return_value=mock_conn):
            result = await handler.rename_identity(topic_id=5, new_label="Alice")

        assert result is True

    @pytest.mark.asyncio
    async def test_rename_never_calls_commit(self):
        from services.collector.corrections import CorrectionHandler

        mock_conn, _ = _make_mock_conn()
        handler = CorrectionHandler()

        with patch("services.collector.corrections.get_db_connection", return_value=mock_conn):
            await handler.rename_identity(topic_id=6, new_label="Bob")

        mock_conn.commit.assert_not_awaited()
