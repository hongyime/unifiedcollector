"""Unit tests for Dashboard Index Page HTML rendering and ping logic.

Requirements: 9.1, 9.2, 10.3, 10.4
"""

import sys
import os
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from index.app import HTML_TEMPLATE, ROW_TEMPLATE, SERVICES, ping_service


def _render_html(statuses: dict) -> str:
    """Helper: render full HTML with given port→bool status map."""
    rows = "\n".join(
        ROW_TEMPLATE.format(
            name=svc["name"],
            port=svc["port"],
            status_badge="🟢 up" if statuses.get(svc["port"]) else "🔴 down",
        )
        for svc in SERVICES
    )
    return HTML_TEMPLATE.format(rows=rows)


class TestIndexPageHTMLRendering(unittest.TestCase):

    def test_index_page_html_contains_title(self):
        """Rendered HTML must contain the 'telegramcollector' title string."""
        all_down = {svc["port"]: False for svc in SERVICES}
        html = _render_html(all_down)
        self.assertIn("telegramcollector", html)

    def test_index_page_html_contains_all_services(self):
        """Rendered HTML must contain all 5 service names and ports 8501-8505."""
        all_up = {svc["port"]: True for svc in SERVICES}
        html = _render_html(all_up)

        expected_names = [
            "Collector",
            "Face Recognition",
            "User Intelligence",
            "Link Discovery",
            "Bulk Sender",
        ]
        for name in expected_names:
            self.assertIn(name, html, f"Service name '{name}' not found in HTML")

        for port in range(8501, 8506):
            self.assertIn(str(port), html, f"Port {port} not found in HTML")

    def test_index_page_html_has_meta_refresh(self):
        """Rendered HTML must contain the 30-second meta refresh tag."""
        all_down = {svc["port"]: False for svc in SERVICES}
        html = _render_html(all_down)
        self.assertIn('<meta http-equiv="refresh" content="30">', html)

    def test_ping_service_returns_false_on_connection_refused(self):
        """ping_service must return False when socket raises ConnectionRefusedError."""
        with patch("socket.create_connection", side_effect=ConnectionRefusedError):
            result = ping_service("127.0.0.1", 8501)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
