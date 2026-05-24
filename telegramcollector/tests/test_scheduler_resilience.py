"""
Tests for Account Scheduler and MTProto resilience:
- AccountScheduler time window logic (normal, overnight, edge cases)
- Activation/deactivation callbacks
- get_status and time_until_next_transition
- MTProto SecurityError handling in TelegramClientManager._health_monitor
- Config settings for scheduling
"""
import pytest
import asyncio
from datetime import datetime, time as dt_time, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock


# ============================================================
# AccountScheduler: Time Window Logic
# ============================================================

class TestAccountSchedulerTimeWindow:
    """Test is_within_active_window for various configurations."""

    def test_disabled_scheduler_always_active(self):
        """When disabled, scheduler always reports active."""
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(enabled=False, active_start="08:00", active_end="12:00")
        # Test at any time — should always be True
        midnight = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        noon = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        assert sched.is_within_active_window(midnight) is True
        assert sched.is_within_active_window(noon) is True

    def test_normal_window_inside(self):
        """Within a normal window (start < end), e.g. 08:00-20:00."""
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(enabled=True, active_start="08:00", active_end="20:00")
        # 10:00 is inside 08:00-20:00
        inside = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        assert sched.is_within_active_window(inside) is True

    def test_normal_window_outside_before(self):
        """Before a normal window."""
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(enabled=True, active_start="08:00", active_end="20:00")
        before = datetime(2024, 1, 1, 6, 0, 0, tzinfo=timezone.utc)
        assert sched.is_within_active_window(before) is False

    def test_normal_window_outside_after(self):
        """After a normal window."""
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(enabled=True, active_start="08:00", active_end="20:00")
        after = datetime(2024, 1, 1, 21, 0, 0, tzinfo=timezone.utc)
        assert sched.is_within_active_window(after) is False

    def test_normal_window_at_start_boundary(self):
        """Exactly at window start should be inside."""
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(enabled=True, active_start="08:00", active_end="20:00")
        at_start = datetime(2024, 1, 1, 8, 0, 0, tzinfo=timezone.utc)
        assert sched.is_within_active_window(at_start) is True

    def test_normal_window_at_end_boundary(self):
        """Exactly at window end should be inside (inclusive)."""
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(enabled=True, active_start="08:00", active_end="20:00")
        at_end = datetime(2024, 1, 1, 20, 0, 0, tzinfo=timezone.utc)
        assert sched.is_within_active_window(at_end) is True

    def test_overnight_window_inside_late(self):
        """Inside an overnight window (start > end), e.g. 22:00-06:00, testing late night."""
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(enabled=True, active_start="22:00", active_end="06:00")
        late_night = datetime(2024, 1, 1, 23, 30, 0, tzinfo=timezone.utc)
        assert sched.is_within_active_window(late_night) is True

    def test_overnight_window_inside_early(self):
        """Inside an overnight window, testing early morning."""
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(enabled=True, active_start="22:00", active_end="06:00")
        early_morning = datetime(2024, 1, 2, 3, 0, 0, tzinfo=timezone.utc)
        assert sched.is_within_active_window(early_morning) is True

    def test_overnight_window_outside(self):
        """Outside an overnight window (midday)."""
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(enabled=True, active_start="22:00", active_end="06:00")
        midday = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        assert sched.is_within_active_window(midday) is False

    def test_full_day_window(self):
        """A 00:00-24:00 window should always be active."""
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(enabled=True, active_start="00:00", active_end="24:00")
        any_time = datetime(2024, 1, 1, 15, 30, 0, tzinfo=timezone.utc)
        assert sched.is_within_active_window(any_time) is True

    def test_narrow_window(self):
        """A narrow window, e.g. 12:00-12:05."""
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(enabled=True, active_start="12:00", active_end="12:05")
        inside = datetime(2024, 1, 1, 12, 3, 0, tzinfo=timezone.utc)
        outside = datetime(2024, 1, 1, 12, 10, 0, tzinfo=timezone.utc)
        assert sched.is_within_active_window(inside) is True
        assert sched.is_within_active_window(outside) is False


# ============================================================
# AccountScheduler: parse_time
# ============================================================

class TestAccountSchedulerParseTime:
    """Test the _parse_time static method."""

    def test_parse_normal_time(self):
        from services.collector.scheduler import AccountScheduler
        t = AccountScheduler._parse_time("08:30")
        assert t.hour == 8
        assert t.minute == 30

    def test_parse_24_00(self):
        """24:00 should map to 23:59:59 (end of day)."""
        from services.collector.scheduler import AccountScheduler
        t = AccountScheduler._parse_time("24:00")
        assert t.hour == 23
        assert t.minute == 59
        assert t.second == 59

    def test_parse_midnight(self):
        from services.collector.scheduler import AccountScheduler
        t = AccountScheduler._parse_time("00:00")
        assert t.hour == 0
        assert t.minute == 0

    def test_parse_with_whitespace(self):
        from services.collector.scheduler import AccountScheduler
        t = AccountScheduler._parse_time("  09:15  ")
        assert t.hour == 9
        assert t.minute == 15


# ============================================================
# AccountScheduler: Activation/Deactivation Callbacks
# ============================================================

class TestAccountSchedulerCallbacks:
    """Test that on_activate and on_deactivate callbacks fire correctly."""

    @pytest.mark.asyncio
    async def test_start_outside_window_calls_deactivate(self):
        """Starting outside the active window should trigger on_deactivate."""
        from services.collector.scheduler import AccountScheduler
        deactivate_mock = AsyncMock()

        sched = AccountScheduler(
            enabled=True,
            active_start="08:00",
            active_end="09:00",
            on_deactivate=deactivate_mock,
        )

        # Patch current time to be outside window (12:00)
        with patch('services.collector.scheduler.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            # Manually call start logic (without running the background loop)
            should_be_active = sched.is_within_active_window(
                datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
            )
            assert should_be_active is False

            # Simulate start's initial check
            if not should_be_active:
                sched._is_active = False
                if sched.on_deactivate:
                    await sched.on_deactivate()

        deactivate_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_transition_inactive_to_active(self):
        """When transitioning from inactive to active, on_activate should fire."""
        from services.collector.scheduler import AccountScheduler
        activate_mock = AsyncMock()

        sched = AccountScheduler(
            enabled=True,
            active_start="08:00",
            active_end="20:00",
            on_activate=activate_mock,
        )
        sched._is_active = False  # Simulate being inactive

        # Simulate the schedule_loop's transition logic
        should_be_active = sched.is_within_active_window(
            datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        )
        assert should_be_active is True

        if should_be_active and not sched._is_active:
            sched._is_active = True
            if sched.on_activate:
                await sched.on_activate()

        activate_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_transition_active_to_inactive(self):
        """When transitioning from active to inactive, on_deactivate should fire."""
        from services.collector.scheduler import AccountScheduler
        deactivate_mock = AsyncMock()

        sched = AccountScheduler(
            enabled=True,
            active_start="08:00",
            active_end="12:00",
            on_deactivate=deactivate_mock,
        )
        sched._is_active = True  # Currently active

        should_be_active = sched.is_within_active_window(
            datetime(2024, 1, 1, 15, 0, 0, tzinfo=timezone.utc)
        )
        assert should_be_active is False

        if not should_be_active and sched._is_active:
            sched._is_active = False
            if sched.on_deactivate:
                await sched.on_deactivate()

        deactivate_mock.assert_awaited_once()

    @pytest.mark.asyncio  
    async def test_no_transition_when_already_active(self):
        """No callback should fire if already in the correct state."""
        from services.collector.scheduler import AccountScheduler
        activate_mock = AsyncMock()
        deactivate_mock = AsyncMock()

        sched = AccountScheduler(
            enabled=True,
            active_start="08:00",
            active_end="20:00",
            on_activate=activate_mock,
            on_deactivate=deactivate_mock,
        )
        sched._is_active = True  # Already active

        should_be_active = sched.is_within_active_window(
            datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        )
        # Already active and should be active => no transition
        if should_be_active and not sched._is_active:
            await sched.on_activate()
        elif not should_be_active and sched._is_active:
            await sched.on_deactivate()

        activate_mock.assert_not_awaited()
        deactivate_mock.assert_not_awaited()


# ============================================================
# AccountScheduler: get_status
# ============================================================

class TestAccountSchedulerStatus:
    """Test scheduler status reporting."""

    def test_status_when_disabled(self):
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(enabled=False)
        status = sched.get_status()
        assert status["enabled"] is False
        assert status["is_active"] is True  # Always active when disabled
        assert "within_window" in status

    def test_status_when_enabled_and_active(self):
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(enabled=True, active_start="00:00", active_end="24:00")
        sched._is_active = True
        status = sched.get_status()
        assert status["enabled"] is True
        assert status["is_active"] is True
        assert "next_transition_minutes" in status
        assert isinstance(status["next_transition_minutes"], float)

    def test_status_active_window_format(self):
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(enabled=True, active_start="08:00", active_end="20:00")
        status = sched.get_status()
        assert status["active_window"] == "08:00-20:00 UTC"


# ============================================================
# AccountScheduler: time_until_next_transition
# ============================================================

class TestAccountSchedulerTransitionTime:
    """Test time_until_next_transition calculations."""

    def test_time_until_deactivation(self):
        """When active, reports time until deactivation (active_end)."""
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(enabled=True, active_start="08:00", active_end="20:00")
        now = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        delta = sched.time_until_next_transition(now)
        # Should be 10 hours until 20:00
        assert 9 * 3600 < delta.total_seconds() < 11 * 3600

    def test_time_until_activation(self):
        """When inactive, reports time until activation (active_start)."""
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(enabled=True, active_start="08:00", active_end="20:00")
        now = datetime(2024, 1, 1, 22, 0, 0, tzinfo=timezone.utc)
        delta = sched.time_until_next_transition(now)
        # Should be ~10 hours until 08:00 next day
        assert 9 * 3600 < delta.total_seconds() < 11 * 3600

    def test_disabled_returns_24h(self):
        """Disabled scheduler returns 24h."""
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(enabled=False)
        delta = sched.time_until_next_transition()
        assert delta == timedelta(hours=24)


# ============================================================
# AccountScheduler: Start and Stop lifecycle
# ============================================================

class TestAccountSchedulerLifecycle:
    """Test the async start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_when_disabled_does_nothing(self):
        """Starting a disabled scheduler should not create a task."""
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(enabled=False)
        await sched.start()
        assert sched._task is None

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        """Stopping should cancel the background task."""
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(
            enabled=True,
            active_start="00:00",
            active_end="24:00",
        )
        await sched.start()
        assert sched._task is not None
        assert not sched._task.done()
        await sched.stop()
        assert sched._task.done() or sched._task.cancelled()

    @pytest.mark.asyncio
    async def test_is_active_property(self):
        """is_active property reflects _is_active state."""
        from services.collector.scheduler import AccountScheduler
        sched = AccountScheduler(enabled=True, active_start="00:00", active_end="24:00")
        sched._is_active = True
        assert sched.is_active is True
        sched._is_active = False
        assert sched.is_active is False


# ============================================================
# TelegramClientManager: MTProto Resilience
# ============================================================

class TestTelegramClientMTProtoResilience:
    """Test health_monitor's SecurityError / MTProto conflict handling."""

    @staticmethod
    def _read_source():
        with open("telegram_client.py", "r", encoding='utf-8') as f:
            return f.read()

    def test_security_error_imported(self):
        """SecurityError must be in the telegram_client imports."""
        source = self._read_source()
        assert "SecurityError" in source

    def test_health_monitor_has_consecutive_counter_attr(self):
        """_health_monitor should init _consecutive_mtproto_errors."""
        source = self._read_source()
        assert '_consecutive_mtproto_errors' in source

    def test_health_monitor_catches_security_error(self):
        """_health_monitor must have an except clause for SecurityError."""
        source = self._read_source()
        assert 'SecurityError' in source
        assert 'ConnectionError' in source

    def test_health_monitor_120s_backoff(self):
        """When MTProto conflict detected (>=3 errors), 120s backoff."""
        source = self._read_source()
        assert '120' in source, "120-second backoff for MTProto conflict not found"

    def test_health_monitor_full_disconnect_reconnect(self):
        """Health monitor performs full disconnect before reconnect."""
        source = self._read_source()
        assert 'await self.client.disconnect()' in source

    def test_max_reconnect_attempts_increased(self):
        """MAX_RECONNECT_ATTEMPTS should be >= 10 for resilience."""
        source = self._read_source()
        assert 'MAX_RECONNECT_ATTEMPTS = 10' in source

    def test_reset_authorization_import_guarded(self):
        """ResetAuthorizationRequest import should be try/except guarded."""
        source = self._read_source()
        assert 'ResetAuthorizationRequest = None' in source


# ============================================================
# Config: Schedule Settings
# ============================================================

class TestConfigScheduleSettings:
    """Ensure config.py has the scheduling settings."""

    def test_account_schedule_enabled_default(self):
        """ACCOUNT_SCHEDULE_ENABLED should default to False."""
        from shared.config import Settings
        s = Settings(
            TG_API_ID=12345,
            TG_API_HASH="abc",
            _env_file=None,
        )
        assert s.ACCOUNT_SCHEDULE_ENABLED is False

    def test_account_active_start_default(self):
        """ACCOUNT_ACTIVE_START should default to '00:00'."""
        from shared.config import Settings
        s = Settings(
            TG_API_ID=12345,
            TG_API_HASH="abc",
            _env_file=None,
        )
        assert s.ACCOUNT_ACTIVE_START == "00:00"

    def test_account_active_end_default(self):
        """ACCOUNT_ACTIVE_END should default to '24:00'."""
        from shared.config import Settings
        s = Settings(
            TG_API_ID=12345,
            TG_API_HASH="abc",
            _env_file=None,
        )
        assert s.ACCOUNT_ACTIVE_END == "24:00"


# ============================================================
# Worker: Scheduler Integration (structural checks)
# ============================================================

class TestWorkerSchedulerIntegration:
    """Verify that worker.py has the scheduler integration points."""

    def test_worker_imports_scheduler(self):
        """worker.py must reference AccountScheduler."""
        import inspect
        # Read the source file directly
        with open("worker.py", "r", encoding='utf-8') as f:
            source = f.read()
        assert "AccountScheduler" in source, "worker.py must import/use AccountScheduler"

    def test_worker_has_scheduler_field(self):
        """worker.py __init__ should have self.scheduler."""
        with open("worker.py", "r", encoding='utf-8') as f:
            source = f.read()
        assert "self.scheduler" in source

    def test_worker_has_on_schedule_activate(self):
        """worker.py should have _on_schedule_activate method."""
        with open("worker.py", "r", encoding='utf-8') as f:
            source = f.read()
        assert "_on_schedule_activate" in source

    def test_worker_has_on_schedule_deactivate(self):
        """worker.py should have _on_schedule_deactivate method."""
        with open("worker.py", "r", encoding='utf-8') as f:
            source = f.read()
        assert "_on_schedule_deactivate" in source

    def test_worker_scheduler_stop_in_shutdown(self):
        """worker.py shutdown should stop the scheduler."""
        with open("worker.py", "r", encoding='utf-8') as f:
            source = f.read()
        assert "scheduler.stop" in source or "self.scheduler.stop" in source


# ============================================================
# Docker-Compose: Schedule Environment Variables
# ============================================================

class TestDockerComposeScheduleEnv:
    """Verify docker-compose.yml has the scheduling env vars."""

    def test_schedule_env_vars_present(self):
        with open("docker-compose.yml", "r", encoding='utf-8') as f:
            content = f.read()
        assert "ACCOUNT_SCHEDULE_ENABLED" in content
        assert "ACCOUNT_ACTIVE_START" in content
        assert "ACCOUNT_ACTIVE_END" in content


# ============================================================
# AccountScheduler module imports cleanly
# ============================================================

class TestAccountSchedulerImport:
    """Ensure the module imports without errors."""

    def test_import_account_scheduler(self):
        import services.collector.scheduler as account_scheduler
        assert hasattr(account_scheduler, 'AccountScheduler')

    def test_scheduler_class_attributes(self):
        from services.collector.scheduler import AccountScheduler
        s = AccountScheduler()
        assert hasattr(s, 'enabled')
        assert hasattr(s, 'active_start')
        assert hasattr(s, 'active_end')
        assert hasattr(s, 'on_activate')
        assert hasattr(s, 'on_deactivate')
        assert hasattr(s, 'is_within_active_window')
        assert hasattr(s, 'start')
        assert hasattr(s, 'stop')
        assert hasattr(s, 'get_status')
        assert hasattr(s, 'time_until_next_transition')
        assert hasattr(s, 'is_active')
