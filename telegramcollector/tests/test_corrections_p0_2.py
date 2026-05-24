"""
Tests for P0.2: corrections.py rename_identity() Telegram topic rename fix.

Validates: Requirements 2.2 (rename_identity updates both DB and Telegram topic)
           Requirements 3.2 (DB update preserved)
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestCorrectionHandlerInit:
    """CorrectionHandler accepts topic_manager parameter."""

    def test_accepts_topic_manager(self):
        from services.collector.corrections import CorrectionHandler
        mock_tm = MagicMock()
        handler = CorrectionHandler(topic_manager=mock_tm)
        assert handler.topic_manager is mock_tm

    def test_defaults_to_none(self):
        from services.collector.corrections import CorrectionHandler
        handler = CorrectionHandler()
        assert handler.topic_manager is None


class TestRenameIdentity:
    """rename_identity() updates DB and calls topic_manager.rename_topic()."""

    @pytest.mark.asyncio
    async def test_rename_calls_topic_manager(self):
        """Fix checking: rename_identity calls rename_topic when topic_manager is set."""
        from services.collector.corrections import CorrectionHandler

        mock_tm = MagicMock()
        mock_tm.rename_topic = AsyncMock(return_value=True)

        mock_cursor = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=False)
        mock_cursor.execute = AsyncMock()

        handler = CorrectionHandler(topic_manager=mock_tm)

        with patch("services.collector.corrections.get_db_connection", return_value=mock_conn):
            result = await handler.rename_identity(topic_id=42, new_label="Alice")

        assert result is True
        mock_tm.rename_topic.assert_awaited_once_with(42, "Alice")

    @pytest.mark.asyncio
    async def test_rename_updates_db(self):
        """Preservation checking: DB update still happens."""
        from services.collector.corrections import CorrectionHandler

        mock_tm = MagicMock()
        mock_tm.rename_topic = AsyncMock(return_value=True)

        mock_cursor = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=False)
        mock_cursor.execute = AsyncMock()

        handler = CorrectionHandler(topic_manager=mock_tm)

        with patch("services.collector.corrections.get_db_connection", return_value=mock_conn):
            result = await handler.rename_identity(topic_id=7, new_label="Bob")

        assert result is True
        mock_cursor.execute.assert_awaited_once()
        call_args = mock_cursor.execute.call_args[0]
        assert "UPDATE telegram_topics" in call_args[0]
        assert "Bob" in call_args[1]
        assert 7 in call_args[1]

    @pytest.mark.asyncio
    async def test_rename_without_topic_manager_skips_telegram(self):
        """Preservation: no topic_manager means no Telegram call, but DB still updated."""
        from services.collector.corrections import CorrectionHandler

        mock_cursor = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=False)
        mock_cursor.execute = AsyncMock()

        handler = CorrectionHandler()  # no topic_manager

        with patch("services.collector.corrections.get_db_connection", return_value=mock_conn):
            result = await handler.rename_identity(topic_id=5, new_label="Charlie")

        assert result is True
        mock_cursor.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rename_returns_false_on_db_error(self):
        """Error handling: returns False when DB raises."""
        from services.collector.corrections import CorrectionHandler

        mock_tm = MagicMock()
        mock_tm.rename_topic = AsyncMock(return_value=True)

        mock_conn = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(side_effect=Exception("DB error"))
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        handler = CorrectionHandler(topic_manager=mock_tm)

        with patch("services.collector.corrections.get_db_connection", return_value=mock_conn):
            result = await handler.rename_identity(topic_id=1, new_label="Fail")

        assert result is False
        mock_tm.rename_topic.assert_not_awaited()
