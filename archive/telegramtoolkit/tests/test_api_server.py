#!/usr/bin/env python3
"""
Tests for API server database error handling.
"""

import io
import json
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from src.server.api_server import APIHandler


class TestAPIHandlerDatabaseErrors(unittest.TestCase):
    """Verify API endpoints surface database lock conditions correctly."""

    def _make_handler(self) -> APIHandler:
        handler = APIHandler.__new__(APIHandler)
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        return handler

    def test_serve_users_returns_503_when_database_locked(self):
        """Database lock contention should surface as a temporary-unavailable response."""
        handler = self._make_handler()

        with patch.object(handler, "_open_connection", side_effect=sqlite3.OperationalError("database is locked")):
            handler.serve_users()

        handler.send_response.assert_called_once_with(503)
        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertIn("Database is locked", payload["error"])

    def test_serve_users_returns_500_for_non_lock_database_error(self):
        """Non-lock SQLite failures should remain generic server errors."""
        handler = self._make_handler()

        with patch.object(handler, "_open_connection", side_effect=sqlite3.OperationalError("no such table: users")):
            handler.serve_users()

        handler.send_response.assert_called_once_with(500)
        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertIn("no such table", payload["error"])


if __name__ == "__main__":
    unittest.main()
