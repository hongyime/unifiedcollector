"""
Tests for P2.4: HubNotifier._send_message() caches rate-limited messages.

Validates: Requirements 2.13 (rate-limited messages cached for later replay)
           Requirements 3.10 (non-rate-limited messages delivered immediately)
"""
import pytest
import asyncio
import sqlite3
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch, call


@pytest.fixture
def notifier():
    """Creates a HubNotifier with a low rate limit for easy testing."""
    from shared.hub_notifier import HubNotifier
    HubNotifier._instance = None
    n = HubNotifier(
        batch_interval=60,
        rate_limit_per_minute=2,
        enable_notifications=True,
    )
    n._supervisor_loop = AsyncMock()
    return n


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Redirect cache DB to a temp file."""
    db_file = str(tmp_path / "hub_cache.db")
    return db_file


# ---------------------------------------------------------------------------
# Fix-checking: rate-limited messages are cached (not dropped)
# ---------------------------------------------------------------------------

class TestRateLimitedMessagesCached:
    """Validates: Requirements 2.13"""

    @pytest.mark.asyncio
    async def test_rate_limited_message_is_cached(self, notifier, tmp_path):
        """When rate limit is hit, message must be written to cache."""
        db_path = str(tmp_path / "hub_cache.db")

        # Exhaust the rate limit (2/min)
        notifier._messages_sent_this_minute = notifier._rate_limit

        with patch.object(notifier, '_cache_notification', new_callable=AsyncMock) as mock_cache:
            result = await notifier._send_message("rate-limited message")

        mock_cache.assert_called_once_with("rate-limited message")
        assert result is False  # returns False when rate-limited

    @pytest.mark.asyncio
    async def test_rate_limited_message_written_to_sqlite(self, notifier, tmp_path):
        """Cached message actually lands in the SQLite DB."""
        db_path = str(tmp_path / "hub_cache.db")

        notifier._messages_sent_this_minute = notifier._rate_limit

        # Patch _write_to_cache to use our temp db
        original_write = notifier._write_to_cache

        def write_to_tmp(path, message, max_size=500):
            return original_write(db_path, message, max_size)

        with patch.object(notifier, '_write_to_cache', side_effect=write_to_tmp):
            await notifier._send_message("cached msg")

        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT message FROM pending_notifications").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "cached msg"

    @pytest.mark.asyncio
    async def test_multiple_rate_limited_messages_all_cached(self, notifier, tmp_path):
        """All messages sent while rate-limited must be cached."""
        db_path = str(tmp_path / "hub_cache.db")
        notifier._messages_sent_this_minute = notifier._rate_limit

        original_write = notifier._write_to_cache

        def write_to_tmp(path, message, max_size=500):
            return original_write(db_path, message, max_size)

        with patch.object(notifier, '_write_to_cache', side_effect=write_to_tmp):
            for i in range(5):
                await notifier._send_message(f"msg {i}")

        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT message FROM pending_notifications ORDER BY id").fetchall()
        assert len(rows) == 5
        assert [r[0] for r in rows] == [f"msg {i}" for i in range(5)]


# ---------------------------------------------------------------------------
# Fix-checking: cached messages are replayed
# ---------------------------------------------------------------------------

class TestCachedMessagesReplayed:
    """Validates: Requirements 2.13 (replay)"""

    @pytest.mark.asyncio
    async def test_replay_sends_cached_messages(self, notifier, tmp_path):
        """_replay_cached_notifications() sends cached messages."""
        db_path = str(tmp_path / "hub_cache.db")

        # Pre-populate cache
        notifier._write_to_cache(db_path, "pending msg 1")
        notifier._write_to_cache(db_path, "pending msg 2")

        sent = []

        async def fake_send(message):
            sent.append(message)
            return True

        with patch.object(notifier, '_send_message', side_effect=fake_send):
            with patch('os.path.exists', return_value=True):
                original_read = notifier._read_cache
                original_delete = notifier._delete_from_cache

                def read_from_tmp(path):
                    return original_read(db_path)

                def delete_from_tmp(path, msg_id):
                    return original_delete(db_path, msg_id)

                with patch.object(notifier, '_read_cache', side_effect=read_from_tmp):
                    with patch.object(notifier, '_delete_from_cache', side_effect=delete_from_tmp):
                        await notifier._replay_cached_notifications()

        assert len(sent) == 2

    @pytest.mark.asyncio
    async def test_replay_deletes_successfully_sent_messages(self, notifier, tmp_path):
        """After successful replay, messages are removed from cache."""
        db_path = str(tmp_path / "hub_cache.db")
        notifier._write_to_cache(db_path, "to replay")

        async def fake_send(message):
            return True

        with patch.object(notifier, '_send_message', side_effect=fake_send):
            with patch('os.path.exists', return_value=True):
                original_read = notifier._read_cache
                original_delete = notifier._delete_from_cache

                with patch.object(notifier, '_read_cache', side_effect=lambda p: original_read(db_path)):
                    with patch.object(notifier, '_delete_from_cache', side_effect=lambda p, i: original_delete(db_path, i)):
                        await notifier._replay_cached_notifications()

        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT * FROM pending_notifications").fetchall()
        assert len(rows) == 0


# ---------------------------------------------------------------------------
# Fix-checking: cache has size limit
# ---------------------------------------------------------------------------

class TestCacheSizeLimit:
    """Validates: Requirements 2.13 (cache size limit)"""

    def test_cache_size_limit_enforced(self, notifier, tmp_path):
        """Cache must not grow beyond max_size."""
        db_path = str(tmp_path / "hub_cache.db")
        max_size = 5

        for i in range(10):
            notifier._write_to_cache(db_path, f"msg {i}", max_size=max_size)

        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM pending_notifications").fetchone()[0]
        assert count <= max_size

    def test_cache_size_limit_drops_oldest(self, notifier, tmp_path):
        """When cache is full, oldest messages are dropped."""
        db_path = str(tmp_path / "hub_cache.db")
        max_size = 3

        for i in range(5):
            notifier._write_to_cache(db_path, f"msg {i}", max_size=max_size)

        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT message FROM pending_notifications ORDER BY id"
            ).fetchall()

        messages = [r[0] for r in rows]
        # Newest messages should be retained
        assert "msg 4" in messages
        assert len(messages) <= max_size

    def test_cache_within_limit_keeps_all(self, notifier, tmp_path):
        """Messages within the limit are all retained."""
        db_path = str(tmp_path / "hub_cache.db")
        max_size = 10

        for i in range(5):
            notifier._write_to_cache(db_path, f"msg {i}", max_size=max_size)

        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM pending_notifications").fetchone()[0]
        assert count == 5


# ---------------------------------------------------------------------------
# Preservation-checking: non-rate-limited messages delivered immediately
# ---------------------------------------------------------------------------

class TestNonRateLimitedDelivery:
    """Validates: Requirements 3.10 (non-rate-limited messages not cached)"""

    @pytest.mark.asyncio
    async def test_non_rate_limited_message_sent_directly(self, notifier):
        """Messages sent when not rate-limited go directly to Hub."""
        notifier._messages_sent_this_minute = 0  # well under limit

        with patch('services.collector.account_manager.bot_client_manager') as mock_bot:
            mock_client = AsyncMock()
            mock_bot.client = mock_client
            with patch('shared.config.get_hub_group_id', return_value=123456):
                with patch.object(notifier, '_cache_notification', new_callable=AsyncMock) as mock_cache:
                    await notifier._send_message("immediate message")

        mock_cache.assert_not_called()
        mock_client.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_rate_limit_counter_increments_on_send(self, notifier):
        """Successful sends increment the rate limit counter."""
        notifier._messages_sent_this_minute = 0

        with patch('services.collector.account_manager.bot_client_manager') as mock_bot:
            mock_client = AsyncMock()
            mock_bot.client = mock_client
            with patch('shared.config.get_hub_group_id', return_value=123456):
                await notifier._send_message("msg")

        assert notifier._messages_sent_this_minute == 1

    @pytest.mark.asyncio
    async def test_rate_limit_resets_after_minute(self, notifier):
        """Rate limit counter resets after 60 seconds."""
        from datetime import datetime, timezone, timedelta

        # Simulate being in a previous minute
        notifier._minute_start = datetime.now(timezone.utc) - timedelta(seconds=61)
        notifier._messages_sent_this_minute = notifier._rate_limit  # was at limit

        with patch('services.collector.account_manager.bot_client_manager') as mock_bot:
            mock_client = AsyncMock()
            mock_bot.client = mock_client
            with patch('shared.config.get_hub_group_id', return_value=123456):
                with patch.object(notifier, '_cache_notification', new_callable=AsyncMock) as mock_cache:
                    await notifier._send_message("after reset")

        # Should NOT have been cached — rate limit reset
        mock_cache.assert_not_called()
        mock_client.send_message.assert_called_once()
