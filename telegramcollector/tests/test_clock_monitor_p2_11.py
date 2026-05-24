"""
Tests for P2.11: ClockDriftMonitor._sync_query_ntp() drift calculation documentation.

Validates: bugfix.md F-020 (Option B - Document approximation)
Bug condition: drift calculated using only transmit_timestamp without documentation.
Fix: Code documents the approximation and its limitations.
"""
import inspect
import struct
import time
import socket
import asyncio
from unittest.mock import patch, MagicMock

import pytest

from services.collector.clock_monitor import ClockDriftMonitor, get_clock_monitor


# ---------------------------------------------------------------------------
# Fix Checking Tests (F-020)
# ---------------------------------------------------------------------------

class TestApproximationDocumented:
    """Validates: Requirements 2.20 - drift estimate is documented as approximation."""

    def test_sync_query_ntp_has_approximation_comment(self):
        """The source code must document that only the transmit timestamp is used."""
        source = inspect.getsource(ClockDriftMonitor._sync_query_ntp)
        assert "approximation" in source.lower(), (
            "_sync_query_ntp must document that the calculation is an approximation"
        )

    def test_sync_query_ntp_documents_4_timestamp_requirement(self):
        """Source must note that full NTP requires 4 timestamps."""
        source = inspect.getsource(ClockDriftMonitor._sync_query_ntp)
        assert "4" in source or "four" in source.lower(), (
            "_sync_query_ntp must mention that full NTP offset needs 4 timestamps"
        )

    def test_sync_query_ntp_documents_overestimation(self):
        """Source must note that drift is overestimated by network one-way delay."""
        source = inspect.getsource(ClockDriftMonitor._sync_query_ntp)
        lower = source.lower()
        assert "overestimate" in lower or "one-way" in lower or "one way" in lower, (
            "_sync_query_ntp must document overestimation by network one-way delay"
        )

    def test_sync_query_ntp_documents_monitoring_use_case(self):
        """Source must state this is acceptable for monitoring/warn-only purposes."""
        source = inspect.getsource(ClockDriftMonitor._sync_query_ntp)
        lower = source.lower()
        assert "monitor" in lower or "warn" in lower, (
            "_sync_query_ntp must document that approximation is acceptable for monitoring"
        )

    def test_transmit_timestamp_bytes_extracted(self):
        """Verify the implementation extracts bytes 40-44 (transmit timestamp)."""
        source = inspect.getsource(ClockDriftMonitor._sync_query_ntp)
        # bytes 40:44 is the transmit timestamp in NTP response
        assert "40:44" in source or "40" in source, (
            "_sync_query_ntp must extract transmit timestamp from bytes 40-44"
        )


# ---------------------------------------------------------------------------
# Functional Tests - drift calculation correctness
# ---------------------------------------------------------------------------

class TestDriftCalculation:
    """Tests that the drift calculation returns sensible values."""

    def _make_ntp_response(self, ntp_time: int) -> bytes:
        """Build a minimal 48-byte NTP response with the given transmit timestamp."""
        data = bytearray(48)
        # Pack the NTP transmit timestamp into bytes 40-44
        struct.pack_into('!I', data, 40, ntp_time)
        return bytes(data)

    def test_drift_returns_float(self):
        """_sync_query_ntp must return a float (seconds)."""
        ntp_epoch_offset = 2208988800
        now_ntp = int(time.time()) + ntp_epoch_offset
        fake_response = self._make_ntp_response(now_ntp)

        with patch('socket.socket') as mock_socket_cls:
            mock_sock = MagicMock()
            mock_socket_cls.return_value = mock_sock
            mock_sock.recvfrom.return_value = (fake_response, ('127.0.0.1', 123))

            result = ClockDriftMonitor._sync_query_ntp('pool.ntp.org')

        assert isinstance(result, float)

    def test_drift_near_zero_for_synced_clock(self):
        """When NTP transmit time equals local time, drift should be near zero."""
        ntp_epoch_offset = 2208988800
        now_unix = time.time()
        now_ntp = int(now_unix) + ntp_epoch_offset
        fake_response = self._make_ntp_response(now_ntp)

        with patch('socket.socket') as mock_socket_cls:
            mock_sock = MagicMock()
            mock_socket_cls.return_value = mock_sock
            mock_sock.recvfrom.return_value = (fake_response, ('127.0.0.1', 123))

            result = ClockDriftMonitor._sync_query_ntp('pool.ntp.org')

        # Allow ±2 seconds for test execution time
        assert abs(result) < 2.0, f"Expected near-zero drift, got {result}"

    def test_drift_positive_when_system_clock_ahead(self):
        """When NTP says time is in the past, system clock is ahead → positive drift."""
        ntp_epoch_offset = 2208988800
        # NTP transmit time is 60 seconds behind local time
        past_ntp = int(time.time()) - 60 + ntp_epoch_offset
        fake_response = self._make_ntp_response(past_ntp)

        with patch('socket.socket') as mock_socket_cls:
            mock_sock = MagicMock()
            mock_socket_cls.return_value = mock_sock
            mock_sock.recvfrom.return_value = (fake_response, ('127.0.0.1', 123))

            result = ClockDriftMonitor._sync_query_ntp('pool.ntp.org')

        assert result > 0, f"Expected positive drift when system clock is ahead, got {result}"

    def test_drift_negative_when_system_clock_behind(self):
        """When NTP says time is in the future, system clock is behind → negative drift."""
        ntp_epoch_offset = 2208988800
        # NTP transmit time is 60 seconds ahead of local time
        future_ntp = int(time.time()) + 60 + ntp_epoch_offset
        fake_response = self._make_ntp_response(future_ntp)

        with patch('socket.socket') as mock_socket_cls:
            mock_sock = MagicMock()
            mock_socket_cls.return_value = mock_sock
            mock_sock.recvfrom.return_value = (fake_response, ('127.0.0.1', 123))

            result = ClockDriftMonitor._sync_query_ntp('pool.ntp.org')

        assert result < 0, f"Expected negative drift when system clock is behind, got {result}"

    def test_returns_zero_for_short_response(self):
        """If response is shorter than 44 bytes, should return 0.0 gracefully."""
        short_response = b'\x00' * 10  # too short

        with patch('socket.socket') as mock_socket_cls:
            mock_sock = MagicMock()
            mock_socket_cls.return_value = mock_sock
            mock_sock.recvfrom.return_value = (short_response, ('127.0.0.1', 123))

            result = ClockDriftMonitor._sync_query_ntp('pool.ntp.org')

        assert result == 0.0

    def test_socket_closed_after_query(self):
        """Socket must be closed even on success (resource cleanup)."""
        ntp_epoch_offset = 2208988800
        now_ntp = int(time.time()) + ntp_epoch_offset
        fake_response = self._make_ntp_response(now_ntp)

        with patch('socket.socket') as mock_socket_cls:
            mock_sock = MagicMock()
            mock_socket_cls.return_value = mock_sock
            mock_sock.recvfrom.return_value = (fake_response, ('127.0.0.1', 123))

            ClockDriftMonitor._sync_query_ntp('pool.ntp.org')

        mock_sock.close.assert_called_once()

    def test_socket_closed_on_exception(self):
        """Socket must be closed even when an exception is raised."""
        with patch('socket.socket') as mock_socket_cls:
            mock_sock = MagicMock()
            mock_socket_cls.return_value = mock_sock
            mock_sock.recvfrom.side_effect = socket.error("connection refused")

            with pytest.raises(socket.error):
                ClockDriftMonitor._sync_query_ntp('pool.ntp.org')

        mock_sock.close.assert_called_once()


# ---------------------------------------------------------------------------
# Preservation Tests (F-020) - existing monitoring behaviour unchanged
# ---------------------------------------------------------------------------

class TestPreservationChecking:
    """Validates: Requirements 3.17 - drift monitoring continues to work."""

    def test_monitor_has_ntp_servers(self):
        """NTP_SERVERS list must still be present and non-empty."""
        assert hasattr(ClockDriftMonitor, 'NTP_SERVERS')
        assert len(ClockDriftMonitor.NTP_SERVERS) > 0

    def test_monitor_has_drift_thresholds(self):
        """Warning and critical thresholds must still be defined."""
        assert hasattr(ClockDriftMonitor, 'DRIFT_WARN_THRESHOLD')
        assert hasattr(ClockDriftMonitor, 'DRIFT_CRITICAL_THRESHOLD')
        assert ClockDriftMonitor.DRIFT_WARN_THRESHOLD > 0
        assert ClockDriftMonitor.DRIFT_CRITICAL_THRESHOLD > ClockDriftMonitor.DRIFT_WARN_THRESHOLD

    def test_get_stats_returns_dict(self):
        """get_stats() must still return a dict with expected keys."""
        monitor = ClockDriftMonitor()
        stats = monitor.get_stats()
        assert isinstance(stats, dict)
        assert "checks" in stats

    def test_get_stats_with_history(self):
        """get_stats() must aggregate drift history correctly."""
        monitor = ClockDriftMonitor()
        monitor.drift_history = [(time.time(), 1.0), (time.time(), 3.0)]
        stats = monitor.get_stats()
        assert stats["checks"] == 2
        assert stats["avg_drift_sec"] == 2.0
        assert stats["max_drift_sec"] == 3.0
        assert stats["min_drift_sec"] == 1.0

    @pytest.mark.asyncio
    async def test_check_ntp_drift_returns_none_on_all_failures(self):
        """_check_ntp_drift must return None when all NTP servers fail."""
        monitor = ClockDriftMonitor()

        with patch.object(monitor, '_query_ntp', side_effect=Exception("timeout")):
            result = await monitor._check_ntp_drift()

        assert result is None

    @pytest.mark.asyncio
    async def test_query_ntp_runs_in_executor(self):
        """_query_ntp must delegate to run_in_executor (non-blocking)."""
        monitor = ClockDriftMonitor()
        called = []

        async def fake_run_in_executor(executor, func, *args):
            called.append(func)
            return 0.5

        loop = asyncio.get_event_loop()
        with patch.object(loop, 'run_in_executor', side_effect=fake_run_in_executor):
            result = await monitor._query_ntp('pool.ntp.org')

        assert result == 0.5
        assert len(called) == 1

    def test_global_monitor_singleton(self):
        """get_clock_monitor() must return the same instance on repeated calls."""
        m1 = get_clock_monitor()
        m2 = get_clock_monitor()
        assert m1 is m2
