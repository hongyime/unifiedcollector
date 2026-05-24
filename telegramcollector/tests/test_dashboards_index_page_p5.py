# Feature: dashboards-index-page, Property 5: status is always 'down' when ping fails
# Validates: Requirements 10.4
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st
from unittest import mock
import socket
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from index.app import ping_service


@given(
    port=st.integers(min_value=8501, max_value=8505),
    error=st.sampled_from([socket.timeout, ConnectionRefusedError, OSError])
)
@h_settings(max_examples=100)
def test_ping_timeout_yields_down(port, error):
    # Feature: dashboards-index-page, Property 5: status is always 'down' when ping fails
    with mock.patch("socket.create_connection", side_effect=error()):
        result = ping_service("127.0.0.1", port, timeout=3)
    assert result is False
