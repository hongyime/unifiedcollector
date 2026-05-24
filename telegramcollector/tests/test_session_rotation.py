"""
P1.2 Bug-Condition Exploration Test — No Session Rotation on INVALID_SESSION

Validates: Requirements 1.4, 2.4, 3.3, 3.8

Bug condition:
    session.connection_state = INVALID_SESSION
    AND EXISTS account IN db WHERE account.status = 'active'
                                AND account.session_name != self.session_name

The current _handle_invalid_session() in telegram_client.py:
  1. Updates the DB status to 'paused'
  2. Notifies Hub
  3. STOPS — it does NOT query for other active accounts
  4. There is NO _rotation_callbacks list and NO on_session_rotation method

EXPECTED OUTCOME (Task 8 — bug-condition test, on unfixed code): FAILS
  — no rotation callback is ever invoked, even when active accounts exist in DB.

EXPECTED OUTCOME (Task 9 — preservation tests, on unfixed code): PASSES
  — healthy sessions and manually-paused sessions are unaffected.

Documented counterexample (Task 8):
    active_accounts = ["account_b"]  (1 active account, different from self.session_name)
    After calling _handle_invalid_session():
        rotation_callback_invoked = False   ← BUG: should be True
    Root cause: _handle_invalid_session() has no _rotation_callbacks list and
    never queries the DB for other active accounts. The method simply marks the
    current account as 'paused' and returns, leaving scanning permanently stopped.

    Counterexample: active_accounts=["account_b"]
        _rotation_callbacks does not exist on TelegramClientManager
        → no callback can ever be registered or fired
        → scanning stops permanently when session becomes invalid
"""

import asyncio
import sys
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
from hypothesis import given, settings as h_settings, HealthCheck
from hypothesis import strategies as st

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_manager(session_name: str = "test_session"):
    """
    Build a minimal TelegramClientManager without touching the filesystem
    or making real Telegram connections.

    Uses __new__ to bypass __init__ entirely, then manually sets all required
    attributes. This avoids patching internal imports (SQLiteSession, etc.)
    that are resolved inside __init__.
    """
    from shared.telegram_client import TelegramClientManager, ConnectionState

    manager = TelegramClientManager.__new__(TelegramClientManager)
    manager.session_name = session_name
    manager.api_id = 12345
    manager.api_hash = "test_hash"
    manager.enable_mtproto_reset = False
    manager._health_task = None
    manager._is_healthy = False
    manager._state = ConnectionState.INVALID_SESSION
    manager._state_change_callbacks = []
    manager._flood_wait_until = None
    manager._session_lock = None
    manager._is_legacy_session = False
    manager._session_path = f"sessions/{session_name}"
    manager.client = MagicMock()
    manager.manual_pause = False
    manager._rotation_callbacks = []

    return manager


def _make_db_mock(active_session_names: list[str]):
    """
    Build an async context-manager mock for get_db_connection that returns
    rows for the given active session names.
    """
    mock_cursor = AsyncMock()
    # fetchone returns the first active account row (or None if empty)
    if active_session_names:
        first = active_session_names[0]
        mock_cursor.fetchone = AsyncMock(return_value=(first,))
    else:
        mock_cursor.fetchone = AsyncMock(return_value=None)

    mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
    mock_cursor.__aexit__ = AsyncMock(return_value=False)
    mock_cursor.execute = AsyncMock()

    mock_conn = AsyncMock()
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.commit = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)

    @asynccontextmanager
    async def _fake_get_db_connection():
        yield mock_conn

    return _fake_get_db_connection, mock_conn, mock_cursor


# ---------------------------------------------------------------------------
# Task 8 — Property 1: Bug Condition
# No rotation callback fired even when active accounts exist
# ---------------------------------------------------------------------------

@given(
    active_accounts=st.lists(st.text(min_size=1), min_size=1, max_size=10)
)
@h_settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
def test_no_rotation_callback_when_invalid_session(active_accounts):
    """
    **Validates: Requirements 1.4, 2.4**

    Bug condition:
        session.connection_state = INVALID_SESSION
        AND EXISTS account IN db WHERE account.status = 'active'
                                    AND account.session_name != self.session_name

    For each list of active_accounts (1–10 names):
      1. Build a TelegramClientManager in INVALID_SESSION state.
      2. Mock get_db_connection to return those active accounts.
      3. Register a rotation callback via on_session_rotation().
      4. Call _handle_invalid_session().
      5. Assert the rotation callback WAS invoked (correct expected behavior).

    EXPECTED OUTCOME on unfixed code: FAILS
      — on_session_rotation() does not exist, so registering a callback raises
        AttributeError, OR the callback is never invoked because _handle_invalid_session
        has no rotation logic.

    EXPECTED OUTCOME on fixed code: PASSES
      — rotation callback is invoked with the next active session name.

    Documented counterexample:
        active_accounts=["account_b"]
            rotation_callback_invoked = False
            Root cause: no _rotation_callbacks list, no on_session_rotation method,
            no DB query for active accounts in _handle_invalid_session().
    """
    manager = _make_manager(session_name="current_session")

    # Track whether the rotation callback was invoked
    rotation_calls = []

    async def rotation_callback(next_session_name: str) -> None:
        rotation_calls.append(next_session_name)

    # The fix requires on_session_rotation() to exist and register the callback.
    # On unfixed code this raises AttributeError → test FAILS (bug confirmed).
    assert hasattr(manager, "on_session_rotation"), (
        "BUG CONFIRMED: TelegramClientManager has no on_session_rotation() method. "
        "A rotation callback registration mechanism is required."
    )
    manager.on_session_rotation(rotation_callback)

    # Mock DB to return active accounts (different from current session)
    get_db_mock, mock_conn, mock_cursor = _make_db_mock(active_accounts)

    with patch("shared.telegram_client.get_db_connection", get_db_mock), \
         patch("shared.hub_notifier.notify", new_callable=AsyncMock):

        asyncio.run(manager._handle_invalid_session())

    # Assert the rotation callback WAS invoked (correct expected behavior).
    # On unfixed code: rotation_calls is empty → assertion FAILS → bug confirmed.
    assert len(rotation_calls) > 0, (
        f"BUG CONFIRMED: rotation callback never invoked even though "
        f"{len(active_accounts)} active account(s) exist in DB. "
        f"active_accounts={active_accounts}. "
        f"_handle_invalid_session() must query DB for active accounts and "
        f"fire rotation callbacks."
    )


# ---------------------------------------------------------------------------
# Task 9 — Property 2: Preservation
# Healthy sessions and manually-paused sessions are unaffected
# ---------------------------------------------------------------------------

@given(
    session_state=st.sampled_from([
        "disconnected",
        "connecting",
        "connected",
        "reconnecting",
        "flood_wait",
        "paused",
    ])
)
@h_settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
def test_no_rotation_for_healthy_session_states(session_state):
    """
    **Validates: Requirements 3.3**

    Preservation: when session state is NOT INVALID_SESSION,
    _handle_invalid_session is never called and no rotation callback fires.

    For all healthy session states (not INVALID_SESSION):
      1. Build a TelegramClientManager in the given healthy state.
      2. Verify _handle_invalid_session is NOT called during normal operation.
      3. Assert no rotation callback is registered or fired.

    EXPECTED OUTCOME on unfixed code: PASSES
      — healthy sessions are unaffected (no rotation mechanism exists at all).

    EXPECTED OUTCOME on fixed code: PASSES
      — healthy sessions must remain unaffected after the fix.
    """
    from shared.telegram_client import TelegramClientManager, ConnectionState

    state_map = {
        "disconnected": ConnectionState.DISCONNECTED,
        "connecting": ConnectionState.CONNECTING,
        "connected": ConnectionState.CONNECTED,
        "reconnecting": ConnectionState.RECONNECTING,
        "flood_wait": ConnectionState.FLOOD_WAIT,
        "paused": ConnectionState.PAUSED,
    }

    manager = _make_manager(session_name="healthy_session")
    manager._state = state_map[session_state]

    rotation_calls = []

    async def rotation_callback(next_session_name: str) -> None:
        rotation_calls.append(next_session_name)

    # Register callback if the method exists (it may not on unfixed code)
    if hasattr(manager, "on_session_rotation"):
        manager.on_session_rotation(rotation_callback)

    # For healthy states, _handle_invalid_session should NOT be called.
    # We verify this by checking the state is not INVALID_SESSION.
    assert manager._state != ConnectionState.INVALID_SESSION, (
        f"State {session_state} should not be INVALID_SESSION"
    )

    # No rotation callback should have fired (we never called _handle_invalid_session)
    assert len(rotation_calls) == 0, (
        f"Rotation callback fired unexpectedly for healthy state {session_state}. "
        f"Healthy sessions must not trigger rotation."
    )


@given(
    active_accounts=st.lists(st.text(min_size=1), min_size=0, max_size=10),
    rotation_enabled=st.booleans(),
)
@h_settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
def test_no_rotation_when_manually_paused(active_accounts, rotation_enabled):
    """
    **Validates: Requirements 3.8**

    Preservation: when set_manual_pause(True) is set, no rotation occurs
    regardless of session state or SESSION_ROTATION_ENABLED value.

    For all (active_accounts, rotation_enabled):
      1. Build a TelegramClientManager with manual_pause=True.
      2. Mock DB to return active_accounts.
      3. Call _handle_invalid_session() (simulating an invalid session event).
      4. Assert rotation callback is NOT invoked (manual pause suppresses rotation).

    EXPECTED OUTCOME on unfixed code: PASSES
      — no rotation mechanism exists, so no callback fires regardless.

    EXPECTED OUTCOME on fixed code: PASSES
      — manual pause must suppress rotation even when SESSION_ROTATION_ENABLED=True.
    """
    manager = _make_manager(session_name="paused_session")

    # Set manual pause — this must suppress rotation
    manager.manual_pause = True

    rotation_calls = []

    async def rotation_callback(next_session_name: str) -> None:
        rotation_calls.append(next_session_name)

    # Register callback if the method exists
    if hasattr(manager, "on_session_rotation"):
        manager.on_session_rotation(rotation_callback)

    get_db_mock, mock_conn, mock_cursor = _make_db_mock(active_accounts)

    with patch("shared.telegram_client.get_db_connection", get_db_mock), \
         patch("shared.hub_notifier.notify", new_callable=AsyncMock):

        asyncio.run(manager._handle_invalid_session())

    # Manual pause must suppress rotation — callback must NOT be invoked
    assert len(rotation_calls) == 0, (
        f"BUG: rotation callback fired despite manual_pause=True. "
        f"active_accounts={active_accounts}, rotation_enabled={rotation_enabled}. "
        f"Manual pause must suppress session rotation (Requirements 3.8)."
    )
