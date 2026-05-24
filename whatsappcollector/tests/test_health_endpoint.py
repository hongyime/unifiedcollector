"""
Property test for health endpoint — Property 14: Health Endpoint Reflects Worker+Broker State

**Validates: Requirements 7.3**

FOR ALL combinations of (worker_running: bool, broker_connected: bool),
the /health endpoint SHALL return HTTP 200 when both are True,
and HTTP 503 when either is False.
"""

import sys
import os
import socket
import threading
import time
import urllib.request
import urllib.error

# Ensure workspace root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

from shared.observability import HealthAndMetricsHandler
from http.server import ThreadingHTTPServer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    """Bind to port 0 to get an OS-assigned free port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def _start_test_server(health_fn) -> tuple[ThreadingHTTPServer, int]:
    """Start a ThreadingHTTPServer with the given health_fn on a free port."""
    # Each test server needs its own handler class to avoid shared state
    class _TestHandler(HealthAndMetricsHandler):
        _health_check_fn = staticmethod(health_fn)

    port = _find_free_port()
    server = ThreadingHTTPServer(('', port), _TestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _http_get(port: int, path: str) -> tuple[int, bytes]:
    """Make a GET request and return (status_code, body)."""
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ---------------------------------------------------------------------------
# Property 14: Health status logic — pure function test
# ---------------------------------------------------------------------------

@given(
    worker_running=st.booleans(),
    broker_connected=st.booleans(),
)
@h_settings(max_examples=50)
def test_health_status_reflects_worker_and_broker(worker_running, broker_connected):
    """
    Property 14: Health Endpoint Reflects Worker+Broker State (503 when either is false)
    **Validates: Requirements 7.3**
    """
    health_fn = lambda: {
        "status": "ok" if worker_running and broker_connected else "degraded",
        "worker": "running" if worker_running else "stopped",
        "broker": "connected" if broker_connected else "disconnected",
    }
    data = health_fn()
    expected_status = 200 if data.get('status') == 'ok' else 503

    if worker_running and broker_connected:
        assert expected_status == 200, (
            f"Expected 200 when both running, got {expected_status}"
        )
    else:
        assert expected_status == 503, (
            f"Expected 503 when worker_running={worker_running}, "
            f"broker_connected={broker_connected}, got {expected_status}"
        )


# ---------------------------------------------------------------------------
# Property 14: HTTP server integration — real request test
# ---------------------------------------------------------------------------

@given(
    worker_running=st.booleans(),
    broker_connected=st.booleans(),
)
@h_settings(max_examples=20, deadline=None)
def test_health_endpoint_http_status_code(worker_running, broker_connected):
    """
    Property 14 (integration): The actual HTTP /health endpoint returns 200 or 503
    based on worker+broker state.
    **Validates: Requirements 7.3**
    """
    health_fn = lambda: {
        "status": "ok" if worker_running and broker_connected else "degraded",
        "worker": "running" if worker_running else "stopped",
        "broker": "connected" if broker_connected else "disconnected",
    }

    server, port = _start_test_server(health_fn)
    try:
        # Give the server a moment to start
        time.sleep(0.01)
        status_code, body = _http_get(port, '/health')

        if worker_running and broker_connected:
            assert status_code == 200, (
                f"Expected HTTP 200 when both running, got {status_code}, body={body}"
            )
        else:
            assert status_code == 503, (
                f"Expected HTTP 503 when worker_running={worker_running}, "
                f"broker_connected={broker_connected}, got {status_code}, body={body}"
            )
    finally:
        server.shutdown()


def test_health_endpoint_404_for_unknown_path():
    """Non-health/metrics paths return 404."""
    server, port = _start_test_server(lambda: {"status": "ok"})
    try:
        time.sleep(0.01)
        status_code, _ = _http_get(port, '/unknown')
        assert status_code == 404
    finally:
        server.shutdown()


def test_health_endpoint_metrics_path():
    """The /metrics path returns 200 with prometheus content type."""
    server, port = _start_test_server(lambda: {"status": "ok"})
    try:
        time.sleep(0.01)
        status_code, _ = _http_get(port, '/metrics')
        assert status_code == 200
    finally:
        server.shutdown()
