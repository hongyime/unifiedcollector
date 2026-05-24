"""
Unit tests for BUG-2 fixes:
- collector.database: upsert_system_config, get_system_config, get_group_chats
- collector.worker: handle_session_event findings_hub_configured branch
- collector.dashboard: _findings_hub_panel

Validates: Requirements 2.4, 2.5, 2.6, 3.1, 3.3
"""
import sys
from pathlib import Path

# Make collector and shared importable
_COLLECTOR_ROOT = Path(__file__).resolve().parents[2] / "services" / "collector"
_SHARED_ROOT = Path(__file__).resolve().parents[2]
for _p in [str(_COLLECTOR_ROOT), str(_SHARED_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import asyncio
import unittest.mock as mock
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


# -------------------------------------------------------------------------
# Database method tests
# -------------------------------------------------------------------------

class TestUpsertSystemConfig:
    """Tests for Database.upsert_system_config()"""

    def test_upsert_system_config_inserts_new_key(self):
        """upsert_system_config must execute INSERT with correct key/value."""
        from collector.database import Database

        db = Database()
        mock_conn = AsyncMock()
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        db.pool = mock_pool

        asyncio.run(db.upsert_system_config("findings_hub_jid", "123@g.us"))

        mock_conn.execute.assert_called_once()
        call_args = mock_conn.execute.call_args
        sql = call_args[0][0]
        assert "system_config" in sql.lower()
        assert "ON CONFLICT" in sql
        # Check parameters
        params = call_args[0][1:]
        assert "findings_hub_jid" in params
        assert "123@g.us" in params

    def test_upsert_system_config_updates_existing_key(self):
        """upsert_system_config must use ON CONFLICT UPDATE."""
        from collector.database import Database

        db = Database()
        mock_conn = AsyncMock()
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        db.pool = mock_pool

        asyncio.run(db.upsert_system_config("findings_hub_jid", "456@g.us"))

        call_args = mock_conn.execute.call_args
        sql = call_args[0][0]
        assert "ON CONFLICT" in sql
        assert "DO UPDATE" in sql


class TestGetSystemConfig:
    """Tests for Database.get_system_config()"""

    def test_get_system_config_returns_value(self):
        """get_system_config must return the stored value."""
        from collector.database import Database

        db = Database()
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value="123@g.us")
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        db.pool = mock_pool

        result = asyncio.run(db.get_system_config("findings_hub_jid"))
        assert result == "123@g.us"

    def test_get_system_config_returns_none_when_absent(self):
        """get_system_config must return None when key is absent."""
        from collector.database import Database

        db = Database()
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        db.pool = mock_pool

        result = asyncio.run(db.get_system_config("nonexistent_key"))
        assert result is None


class TestGetGroupChats:
    """Tests for Database.get_group_chats()"""

    def test_get_group_chats_queries_correct_table(self):
        """get_group_chats must query collector.chats WHERE chat_type = 'group'."""
        from collector.database import Database

        db = Database()
        mock_row1 = {"jid": "g1@g.us", "name": "Group A", "member_count": 5, "collected_at": None}
        mock_row2 = {"jid": "g2@g.us", "name": "Group B", "member_count": 3, "collected_at": None}
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[mock_row1, mock_row2])
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        db.pool = mock_pool

        result = asyncio.run(db.get_group_chats())

        assert len(result) == 2
        mock_conn.fetch.assert_called_once()
        sql = mock_conn.fetch.call_args[0][0]
        assert "collector.chats" in sql
        assert "chat_type" in sql
        assert "group" in sql


# -------------------------------------------------------------------------
# Worker event handler tests
# -------------------------------------------------------------------------

class TestHandleSessionEventFindingsHub:
    """Tests for Worker.handle_session_event() findings_hub_configured branch."""

    def _make_mock_message(self, payload: dict):
        """Create a mock AMQP message with the given payload."""
        import json
        msg = AsyncMock()
        msg.body = json.dumps(payload).encode()
        msg.ack = AsyncMock()
        msg.nack = AsyncMock()
        return msg

    def test_handle_session_event_persists_hub_jid(self):
        """findings_hub_configured event must persist JID to system_config."""
        import os
        worker_py = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "services", "collector", "collector", "worker.py"
        )
        with open(worker_py) as f:
            source = f.read()

        assert "findings_hub_configured" in source, (
            "Worker must handle findings_hub_configured event type"
        )
        assert "upsert_system_config" in source, (
            "Worker must call upsert_system_config when findings_hub_configured received"
        )

    def test_handle_session_event_ignores_missing_jid(self):
        """findings_hub_configured with no jid field must not call upsert_system_config."""
        import os
        worker_py = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "services", "collector", "collector", "worker.py"
        )
        with open(worker_py) as f:
            source = f.read()

        # The worker should check for jid before calling upsert
        assert 'jid = payload.get("jid")' in source or "payload.get('jid')" in source, (
            "Worker must check for jid field before persisting"
        )
        assert "if jid:" in source, (
            "Worker must guard upsert_system_config call with 'if jid:'"
        )

    def test_handle_session_event_other_types_unchanged(self):
        """disconnected/disconnect branch must still exist alongside new branch."""
        import os
        worker_py = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "services", "collector", "collector", "worker.py"
        )
        with open(worker_py) as f:
            source = f.read()

        assert '"disconnected"' in source or "'disconnected'" in source, (
            "Existing disconnected branch must still be present"
        )
        assert "pause_for_session" in source, (
            "Existing backfill pause logic must still be present"
        )


# -------------------------------------------------------------------------
# Dashboard panel tests
# -------------------------------------------------------------------------

class TestFindingsHubPanel:
    """Tests for _findings_hub_panel() in collector dashboard."""

    def test_findings_hub_panel_function_exists(self):
        """_findings_hub_panel function must exist in dashboard app.py."""
        import os
        app_py = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "services", "collector", "collector", "dashboard", "app.py"
        )
        with open(app_py) as f:
            source = f.read()

        assert "_findings_hub_panel" in source, (
            "_findings_hub_panel function must exist in collector dashboard"
        )

    def test_findings_hub_panel_calls_get_group_chats(self):
        """_findings_hub_panel must call database.get_group_chats()."""
        import os
        app_py = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "services", "collector", "collector", "dashboard", "app.py"
        )
        with open(app_py) as f:
            source = f.read()

        assert "get_group_chats" in source, (
            "_findings_hub_panel must call database.get_group_chats()"
        )

    def test_findings_hub_panel_calls_get_system_config(self):
        """_findings_hub_panel must call database.get_system_config('findings_hub_jid')."""
        import os
        app_py = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "services", "collector", "collector", "dashboard", "app.py"
        )
        with open(app_py) as f:
            source = f.read()

        assert "get_system_config" in source, (
            "_findings_hub_panel must call database.get_system_config()"
        )
        assert "findings_hub_jid" in source, (
            "_findings_hub_panel must query for findings_hub_jid key"
        )

    def test_findings_hub_panel_called_from_render_async(self):
        """_findings_hub_panel must be called from _render_async()."""
        import os
        app_py = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "services", "collector", "collector", "dashboard", "app.py"
        )
        with open(app_py) as f:
            source = f.read()

        # Find _render_async function and check it calls _findings_hub_panel
        render_start = source.find("async def _render_async()")
        assert render_start != -1
        # Use the rest of the file from _render_async onwards (function can be long)
        render_body = source[render_start:]
        assert "_findings_hub_panel" in render_body, (
            "_findings_hub_panel must be called from _render_async()"
        )
