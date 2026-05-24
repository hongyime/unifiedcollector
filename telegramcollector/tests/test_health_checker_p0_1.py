"""
Tests for P0.1 fix: HealthChecker accepts client argument and
worker.py passes connected client to HealthChecker.

Validates Requirements 2.1 (bugfix.md):
  WHEN HealthChecker is initialized THEN the system SHALL accept an optional
  client argument and pass a connected client from worker.py to enable
  accurate Telegram connectivity checking.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ============================================================
# HealthChecker: client parameter acceptance
# ============================================================

class TestHealthCheckerClientParameter:
    """HealthChecker must accept an optional client argument."""

    def test_accepts_none_client(self):
        """HealthChecker can be constructed with client=None (default)."""
        from health_checker import HealthChecker
        checker = HealthChecker(client=None)
        assert checker.client is None

    def test_accepts_client_argument(self):
        """HealthChecker stores the provided client."""
        from health_checker import HealthChecker
        mock_client = MagicMock()
        checker = HealthChecker(client=mock_client)
        assert checker.client is mock_client

    def test_default_client_is_none(self):
        """HealthChecker default client is None when not provided."""
        from health_checker import HealthChecker
        checker = HealthChecker()
        assert checker.client is None

    def test_client_can_be_updated(self):
        """HealthChecker.client can be updated after construction (worker.py pattern)."""
        from health_checker import HealthChecker
        checker = HealthChecker(client=None)
        mock_client = MagicMock()
        checker.client = mock_client
        assert checker.client is mock_client


# ============================================================
# HealthChecker._check_telegram(): fix checking
# ============================================================

class TestCheckTelegramFixChecking:
    """
    Fix Checking (F-001): _check_telegram() must reflect actual connectivity.

    Validates Requirements 2.1 (bugfix.md)
    """

    @pytest.mark.asyncio
    async def test_returns_false_when_client_is_none(self):
        """
        Bug condition: client is None → must return False.

        Validates: Requirements 2.1
        """
        from health_checker import HealthChecker
        checker = HealthChecker(client=None)
        result = await checker._check_telegram()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_client_disconnected(self):
        """
        Disconnected client → must return False.

        Validates: Requirements 2.1
        """
        from health_checker import HealthChecker
        mock_client = MagicMock()
        mock_client.is_connected.return_value = False
        checker = HealthChecker(client=mock_client)
        result = await checker._check_telegram()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_when_client_connected_and_authorized(self):
        """
        Connected and authorized client → must return True.

        Validates: Requirements 2.1
        """
        from health_checker import HealthChecker
        mock_client = MagicMock()
        mock_client.is_connected.return_value = True
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        checker = HealthChecker(client=mock_client)
        result = await checker._check_telegram()
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_connected_but_not_authorized(self):
        """
        Connected but not authorized → must return False.

        Validates: Requirements 2.1
        """
        from health_checker import HealthChecker
        mock_client = MagicMock()
        mock_client.is_connected.return_value = True
        mock_client.is_user_authorized = AsyncMock(return_value=False)
        checker = HealthChecker(client=mock_client)
        result = await checker._check_telegram()
        assert result is False


# ============================================================
# HealthChecker._check_telegram(): preservation checking
# ============================================================

class TestCheckTelegramPreservationChecking:
    """
    Preservation Checking (F-001): non-buggy inputs must behave as before.

    Validates Requirements 3.1 (bugfix.md)
    """

    @pytest.mark.asyncio
    async def test_connected_authorized_client_still_returns_true(self):
        """
        Non-buggy condition: connected + authorized client continues to return True.

        Validates: Requirements 3.1
        """
        from health_checker import HealthChecker
        mock_client = MagicMock()
        mock_client.is_connected.return_value = True
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        checker = HealthChecker(client=mock_client)
        result = await checker._check_telegram()
        assert result is True

    @pytest.mark.asyncio
    async def test_other_checks_unaffected_by_client_fix(self):
        """
        Other health checks (database, face model) are not affected by client fix.
        """
        from health_checker import HealthChecker
        mock_face_processor = MagicMock()
        mock_face_processor.app = MagicMock()  # face model loaded
        checker = HealthChecker(client=None, face_processor=mock_face_processor)
        # Face model check should still work independently
        result = checker._check_face_model()
        assert result is True


# ============================================================
# worker.py: passes connected client to HealthChecker
# ============================================================

class TestWorkerPassesClientToHealthChecker:
    """
    worker.py must pass a connected client to HealthChecker after accounts load.

    Validates Requirements 2.1 (bugfix.md)
    """

    def test_worker_updates_health_checker_client_after_accounts_loaded(self):
        """
        After self.clients is populated, worker.py must set health_checker.client
        to the first connected client.
        """
        import inspect
        import worker
        source = inspect.getsource(worker.MainWorker.initialize)

        # The fix: after clients are loaded, update health_checker.client
        assert 'self.health_checker.client' in source, (
            "worker.py must update self.health_checker.client after accounts are loaded"
        )
        assert 'self.clients' in source, (
            "worker.py must check self.clients before updating health_checker.client"
        )

    def test_worker_uses_first_client_from_clients_dict(self):
        """
        worker.py must use next(iter(self.clients.values())).client pattern
        to get the first connected client.
        """
        import inspect
        import worker
        source = inspect.getsource(worker.MainWorker.initialize)

        assert 'next(iter(self.clients.values())).client' in source, (
            "worker.py must use next(iter(self.clients.values())).client "
            "to get the first connected client for HealthChecker"
        )

    def test_health_checker_initialized_before_clients_loaded(self):
        """
        HealthChecker is initialized early (with client=None) and updated later.
        This is the correct pattern to avoid circular dependency.
        """
        import inspect
        import worker
        source = inspect.getsource(worker.MainWorker.initialize)

        # HealthChecker init must come before account loading loop
        hc_init_pos = source.find('HealthChecker(')
        client_update_pos = source.find('self.health_checker.client = first_client')

        assert hc_init_pos != -1, "HealthChecker must be initialized in worker.initialize()"
        assert client_update_pos != -1, "health_checker.client must be updated after accounts load"
        assert hc_init_pos < client_update_pos, (
            "HealthChecker must be initialized before client is assigned "
            "(accounts are loaded after HealthChecker init)"
        )
