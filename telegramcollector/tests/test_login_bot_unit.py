"""Unit tests for SessionRouter (task 1.5).

Tests Requirements 8.3, 8.4, 12.2.
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.login_bot.session_router import SessionRouter  # noqa: E402


# ---------------------------------------------------------------------------
# test_missing_target_dir_created
# Requirement 8.3: target dir absent before copy → created automatically
# ---------------------------------------------------------------------------

def test_missing_target_dir_created(tmp_path: Path) -> None:
    """_copy_session creates the destination directory when it does not exist
    and returns True on a successful copy.

    Validates: Requirements 8.3
    """
    # Set up source: base_path/collector/<stem>.session
    collector = tmp_path / "collector"
    collector.mkdir()
    src = collector / "phone123.session"
    src.write_bytes(b"valid session data")

    # Destination directory does NOT exist yet
    dst_dir = tmp_path / "svc_new"
    assert not dst_dir.exists()

    router = SessionRouter(base_path=str(tmp_path))
    result = router._copy_session(src, dst_dir)

    assert result is True
    assert dst_dir.is_dir(), "destination directory should have been created"
    assert (dst_dir / src.name).exists(), "session file should have been copied"


# ---------------------------------------------------------------------------
# test_copy_failure_continues
# Requirement 8.4: failure in one dir does not abort remaining dirs
# ---------------------------------------------------------------------------

def test_copy_failure_continues(tmp_path: Path) -> None:
    """distribute() continues to copy to remaining directories when one fails.

    Validates: Requirements 8.4
    """
    # Set up source
    collector = tmp_path / "collector"
    collector.mkdir()
    src = collector / "phone123.session"
    src.write_bytes(b"valid session data")

    # Two target directories
    svc_a = tmp_path / "svc_a"
    svc_b = tmp_path / "svc_b"
    svc_a.mkdir()
    svc_b.mkdir()

    original_copy2 = __import__("shutil").copy2

    def failing_copy2(src_path, dst_path):
        # Fail only when copying into svc_a
        if "svc_a" in str(dst_path):
            raise OSError("simulated write failure for svc_a")
        return original_copy2(src_path, dst_path)

    router = SessionRouter(base_path=str(tmp_path))

    with patch("services.login_bot.session_router.shutil.copy2", side_effect=failing_copy2):
        result = asyncio.run(router.distribute("phone123"))

    # svc_b must be in the success list despite svc_a failing
    assert "svc_b" in result, f"svc_b should be in success list, got: {result}"
    assert "svc_a" not in result, f"svc_a should NOT be in success list, got: {result}"
    assert "collector" not in result


# ---------------------------------------------------------------------------
# test_zero_byte_copy_excluded_from_success
# Requirement 12.2: zero-byte destination excluded from success list
# ---------------------------------------------------------------------------

def test_zero_byte_copy_excluded_from_success(tmp_path: Path) -> None:
    """_copy_session returns False when the copied file has zero bytes.

    Validates: Requirements 12.2
    """
    # Set up source
    collector = tmp_path / "collector"
    collector.mkdir()
    src = collector / "phone123.session"
    src.write_bytes(b"valid session data")

    dst_dir = tmp_path / "svc_target"
    dst_dir.mkdir()

    def zero_byte_copy2(src_path, dst_path):
        # Write nothing — produce a zero-byte file
        Path(dst_path).write_bytes(b"")

    router = SessionRouter(base_path=str(tmp_path))

    with patch("services.login_bot.session_router.shutil.copy2", side_effect=zero_byte_copy2):
        result = router._copy_session(src, dst_dir)

    assert result is False, (
        f"_copy_session should return False for a zero-byte copy, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Auth handler unit tests (task 4.6)
# Requirements: 3.1, 3.3, 3.5, 3.9, 3.10, 3.11, 3.12, 4.2, 6.2, 6.3
# ---------------------------------------------------------------------------

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import services.login_bot.main as main_mod
from services.login_bot.main import (
    LoginState,
    handle_cancel,
    handle_code,
    handle_message,
    handle_phone,
    handle_start,
    handle_2fa,
    login_sessions,
    messages_to_delete,
    user_bot_mapping,
    active_login_bots,
)


def _make_event(sender_id: int = 1, chat_id: int = 100, text: str = ""):
    """Build a minimal mock event."""
    event = MagicMock()
    event.sender_id = sender_id
    event.chat_id = chat_id
    event.message.id = 999
    event.message.text = text
    event.delete = AsyncMock()

    bot = AsyncMock()
    me = MagicMock()
    me.username = "testbot"
    bot.get_me = AsyncMock(return_value=me)
    bot.send_message = AsyncMock(return_value=MagicMock(id=1001))
    bot.delete_messages = AsyncMock()
    event.client = bot
    return event, bot


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# test_start_prompts_phone
# Requirement 3.1: /startcollector triggers phone prompt
# ---------------------------------------------------------------------------

def test_start_prompts_phone():
    """handle_start sends a phone-number prompt when rate limit is not exceeded.

    Validates: Requirements 3.1
    """
    login_sessions.clear()
    user_bot_mapping.clear()
    active_login_bots.clear()
    main_mod.login_attempts.clear()

    event, bot = _make_event(sender_id=42)

    run(handle_start(event))

    bot.send_message.assert_called_once()
    call_text = bot.send_message.call_args[0][1]
    assert "phone" in call_text.lower()
    assert 42 in login_sessions
    assert login_sessions[42].state == LoginState.WAITING_PHONE


# ---------------------------------------------------------------------------
# test_invalid_phone_rejected
# Requirement 3.3: non-E.164 input stays in WAITING_PHONE
# ---------------------------------------------------------------------------

def test_invalid_phone_rejected():
    """handle_phone rejects invalid phone and keeps state at WAITING_PHONE.

    Validates: Requirements 3.3
    """
    login_sessions.clear()
    event, bot = _make_event(sender_id=10, text="not-a-phone")
    session = LoginState()
    login_sessions[10] = session

    run(handle_phone(bot, event, session, "not-a-phone"))

    assert session.state == LoginState.WAITING_PHONE
    bot.send_message.assert_called_once()
    call_text = bot.send_message.call_args[0][1]
    assert "invalid" in call_text.lower() or "format" in call_text.lower()


# ---------------------------------------------------------------------------
# test_code_stripped_to_digits
# Requirement 3.5: spaces/dashes in code are stripped to first 5 digits
# ---------------------------------------------------------------------------

def test_code_stripped_to_digits():
    """handle_code strips non-digit characters and uses first 5 digits.

    Validates: Requirements 3.5
    """
    login_sessions.clear()
    event, bot = _make_event(sender_id=20, text="1 2 3 4 5")
    session = LoginState()
    session.state = LoginState.WAITING_CODE
    session.phone = "+12345678900"
    session.phone_code_hash = "abc"

    mock_client = AsyncMock()
    mock_me = MagicMock()
    mock_me.id = 99
    mock_client.sign_in = AsyncMock(return_value=mock_me)
    session.client = mock_client
    login_sessions[20] = session

    with patch.object(main_mod, "save_account", new=AsyncMock(return_value=1)), \
         patch.object(main_mod, "nuke_tracked_messages", new=AsyncMock()), \
         patch.object(main_mod, "perform_post_login_cleanup", new=AsyncMock()):
        run(handle_code(bot, event, session, "1 2 3 4 5"))

    # sign_in should have been called with digits-only "12345"
    call_args = mock_client.sign_in.call_args
    assert call_args[0][1] == "12345" or call_args[1].get("code") == "12345" or \
           (len(call_args[0]) > 1 and call_args[0][1] == "12345")


# ---------------------------------------------------------------------------
# test_cancel_clears_state
# Requirement 3.9: /cancel disconnects client and removes session
# ---------------------------------------------------------------------------

def test_cancel_clears_state():
    """handle_cancel removes session and user_bot_mapping entries.

    Validates: Requirements 3.9
    """
    login_sessions.clear()
    user_bot_mapping.clear()

    event, bot = _make_event(sender_id=30)
    session = LoginState()
    mock_client = AsyncMock()
    session.client = mock_client
    login_sessions[30] = session
    user_bot_mapping[30] = bot

    run(handle_cancel(event))

    assert 30 not in login_sessions
    assert 30 not in user_bot_mapping
    bot.send_message.assert_called_once()
    call_text = bot.send_message.call_args[0][1]
    assert "cancel" in call_text.lower()


# ---------------------------------------------------------------------------
# test_phone_code_invalid_allows_retry
# Requirement 3.10: PhoneCodeInvalidError keeps state at WAITING_CODE
# ---------------------------------------------------------------------------

def test_phone_code_invalid_allows_retry():
    """handle_code keeps state at WAITING_CODE on PhoneCodeInvalidError.

    Validates: Requirements 3.10
    """
    if not main_mod._TELETHON_AVAILABLE:
        pytest.skip("telethon not installed")

    from telethon.errors import PhoneCodeInvalidError

    login_sessions.clear()
    event, bot = _make_event(sender_id=40, text="99999")
    session = LoginState()
    session.state = LoginState.WAITING_CODE
    session.phone = "+12345678900"
    session.phone_code_hash = "abc"

    mock_client = AsyncMock()
    mock_client.sign_in = AsyncMock(side_effect=PhoneCodeInvalidError(None))
    session.client = mock_client
    login_sessions[40] = session

    run(handle_code(bot, event, session, "99999"))

    assert session.state == LoginState.WAITING_CODE
    bot.send_message.assert_called_once()
    call_text = bot.send_message.call_args[0][1]
    assert "invalid" in call_text.lower() or "try again" in call_text.lower()


# ---------------------------------------------------------------------------
# test_phone_code_expired_clears_state
# Requirement 3.11: PhoneCodeExpiredError clears session state
# ---------------------------------------------------------------------------

def test_phone_code_expired_clears_state():
    """handle_code clears session on PhoneCodeExpiredError.

    Validates: Requirements 3.11
    """
    if not main_mod._TELETHON_AVAILABLE:
        pytest.skip("telethon not installed")

    from telethon.errors import PhoneCodeExpiredError

    login_sessions.clear()
    event, bot = _make_event(sender_id=50, text="00000")
    session = LoginState()
    session.state = LoginState.WAITING_CODE
    session.phone = "+12345678900"
    session.phone_code_hash = "abc"

    mock_client = AsyncMock()
    mock_client.sign_in = AsyncMock(side_effect=PhoneCodeExpiredError(None))
    mock_client.disconnect = AsyncMock()
    session.client = mock_client
    login_sessions[50] = session

    run(handle_code(bot, event, session, "00000"))

    assert 50 not in login_sessions
    mock_client.disconnect.assert_called_once()
    bot.send_message.assert_called_once()
    call_text = bot.send_message.call_args[0][1]
    assert "expired" in call_text.lower()


# ---------------------------------------------------------------------------
# test_password_hash_invalid_allows_retry
# Requirement 3.12: PasswordHashInvalidError keeps state at WAITING_2FA
# ---------------------------------------------------------------------------

def test_password_hash_invalid_allows_retry():
    """handle_2fa keeps state at WAITING_2FA on PasswordHashInvalidError.

    Validates: Requirements 3.12
    """
    if not main_mod._TELETHON_AVAILABLE:
        pytest.skip("telethon not installed")

    from telethon.errors import PasswordHashInvalidError

    login_sessions.clear()
    event, bot = _make_event(sender_id=60, text="wrongpass")
    session = LoginState()
    session.state = LoginState.WAITING_2FA
    session.phone = "+12345678900"

    mock_client = AsyncMock()
    mock_client.sign_in = AsyncMock(side_effect=PasswordHashInvalidError(None))
    session.client = mock_client
    login_sessions[60] = session

    run(handle_2fa(bot, event, session, "wrongpass"))

    assert session.state == LoginState.WAITING_2FA
    bot.send_message.assert_called_once()
    call_text = bot.send_message.call_args[0][1]
    assert "wrong" in call_text.lower() or "password" in call_text.lower()


# ---------------------------------------------------------------------------
# test_2fa_password_deleted_immediately
# Requirement 4.2: 2FA password message deleted before sign_in
# ---------------------------------------------------------------------------

def test_2fa_password_deleted_immediately():
    """handle_2fa deletes the password message immediately via event.delete().

    Validates: Requirements 4.2
    """
    if not main_mod._TELETHON_AVAILABLE:
        pytest.skip("telethon not installed")

    from telethon.errors import PasswordHashInvalidError

    login_sessions.clear()
    event, bot = _make_event(sender_id=70, text="mypassword")
    session = LoginState()
    session.state = LoginState.WAITING_2FA
    session.phone = "+12345678900"

    mock_client = AsyncMock()
    # Use PasswordHashInvalidError so we don't need save_account etc.
    mock_client.sign_in = AsyncMock(side_effect=PasswordHashInvalidError(None))
    session.client = mock_client
    login_sessions[70] = session

    delete_called_before_sign_in = []

    original_sign_in = mock_client.sign_in

    async def tracking_sign_in(**kwargs):
        delete_called_before_sign_in.append(event.delete.called)
        raise PasswordHashInvalidError(None)

    mock_client.sign_in = tracking_sign_in

    run(handle_2fa(bot, event, session, "mypassword"))

    event.delete.assert_called_once()
    assert delete_called_before_sign_in[0] is True, \
        "event.delete() must be called before sign_in"


# ---------------------------------------------------------------------------
# test_locked_bot_redirects
# Requirement 6.2: locked bot names an alternative
# ---------------------------------------------------------------------------

def test_locked_bot_redirects():
    """handle_start redirects to an alternative bot when current bot is locked.

    Validates: Requirements 6.2
    """
    login_sessions.clear()
    active_login_bots.clear()
    main_mod.login_attempts.clear()

    event, bot = _make_event(sender_id=80)
    # Set up: testbot is locked, altbot is available
    active_login_bots["testbot"] = {
        "client": bot, "name": "testbot", "token": "t",
        "locked": True, "locked_until": 9999999999.0,
    }
    active_login_bots["altbot"] = {
        "client": AsyncMock(), "name": "altbot", "token": "t2",
        "locked": False, "locked_until": 0.0,
    }

    run(handle_start(event))

    bot.send_message.assert_called_once()
    call_text = bot.send_message.call_args[0][1]
    assert "altbot" in call_text
    # Session should NOT have been created
    assert 80 not in login_sessions


# ---------------------------------------------------------------------------
# test_no_alternative_bot_message
# Requirement 6.3: no alternative → retry message
# ---------------------------------------------------------------------------

def test_no_alternative_bot_message():
    """handle_start tells user to retry when all bots are locked.

    Validates: Requirements 6.3
    """
    login_sessions.clear()
    active_login_bots.clear()
    main_mod.login_attempts.clear()

    event, bot = _make_event(sender_id=90)
    # All bots locked
    active_login_bots["testbot"] = {
        "client": bot, "name": "testbot", "token": "t",
        "locked": True, "locked_until": 9999999999.0,
    }

    run(handle_start(event))

    bot.send_message.assert_called_once()
    call_text = bot.send_message.call_args[0][1]
    assert "retry" in call_text.lower() or "unavailable" in call_text.lower()
    assert 90 not in login_sessions


# ---------------------------------------------------------------------------
# Post-auth unit tests (task 6.6)
# Requirements: 4.3, 4.4, 4.5, 10.3, 11.4
# ---------------------------------------------------------------------------

import sys
from types import ModuleType

from services.login_bot.main import (
    perform_post_login_cleanup,
    save_account,
    create_backfill_jobs,
)


def _make_mock_db_module(side_effect=None, ctx_factory=None):
    """Return a fake 'database' module whose get_db_connection is controllable.

    If *side_effect* is given, get_db_connection raises that exception.
    If *ctx_factory* is given, get_db_connection calls it to produce a context manager.
    Otherwise get_db_connection returns a no-op async context manager.
    """
    mod = ModuleType("database")

    if side_effect is not None:
        def _get_db():
            raise side_effect
        mod.get_db_connection = _get_db
    elif ctx_factory is not None:
        mod.get_db_connection = ctx_factory
    else:
        def _noop_ctx():
            conn = AsyncMock()
            cur = AsyncMock()
            cur.__aenter__ = AsyncMock(return_value=cur)
            cur.__aexit__ = AsyncMock(return_value=False)
            conn.cursor = MagicMock(return_value=cur)
            conn.__aenter__ = AsyncMock(return_value=conn)
            conn.__aexit__ = AsyncMock(return_value=False)
            ctx = MagicMock()
            ctx.__aenter__ = AsyncMock(return_value=conn)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx
        mod.get_db_connection = _noop_ctx

    return mod


# ---------------------------------------------------------------------------
# test_success_nukes_tracked_messages
# Requirement 4.3: tracked messages cleared on success
# ---------------------------------------------------------------------------

def test_success_nukes_tracked_messages():
    """handle_code clears messages_to_delete for the chat on success.

    Validates: Requirements 4.3
    """
    login_sessions.clear()
    messages_to_delete.clear()

    chat_id = 555
    messages_to_delete[(chat_id, 1)] = 9999999999.0
    messages_to_delete[(chat_id, 2)] = 9999999999.0

    event, bot = _make_event(sender_id=200, chat_id=chat_id, text="12345")
    session = LoginState()
    session.state = LoginState.WAITING_CODE
    session.phone = "+12345678900"
    session.phone_code_hash = "abc"

    mock_client = AsyncMock()
    mock_me = MagicMock()
    mock_me.id = 42
    mock_client.sign_in = AsyncMock(return_value=mock_me)
    session.client = mock_client
    login_sessions[200] = session

    with patch.object(main_mod, "save_account", new=AsyncMock(return_value=1)), \
         patch.object(main_mod, "perform_post_login_cleanup", new=AsyncMock()):
        run(handle_code(bot, event, session, "12345"))

    remaining = [k for k in messages_to_delete if k[0] == chat_id]
    assert remaining == [], (
        f"messages_to_delete should be empty for chat {chat_id}, got {remaining}"
    )


# ---------------------------------------------------------------------------
# test_post_login_deletes_bot_dialog
# Requirement 4.4: delete_dialog called for bot username
# ---------------------------------------------------------------------------

def test_post_login_deletes_bot_dialog():
    """perform_post_login_cleanup calls delete_dialog with the bot username.

    Validates: Requirements 4.4
    """
    mock_client = AsyncMock()
    mock_client.session = MagicMock()
    mock_client.session.filename = "/data/sessions/collector/12345678900"
    mock_client.delete_dialog = AsyncMock()
    mock_client.disconnect = AsyncMock()

    mock_router = MagicMock()
    mock_router.distribute = AsyncMock(return_value=[])

    with patch("services.login_bot.main.create_backfill_jobs", new=AsyncMock()), \
         patch("services.login_bot.session_router.SessionRouter", return_value=mock_router), \
         patch("services.login_bot.main._get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(SESSIONS_BASE_PATH="/data/sessions")
        run(perform_post_login_cleanup(mock_client, "testbot", 0))

    calls = [str(c.args[0]) for c in mock_client.delete_dialog.call_args_list]
    assert any("testbot" in c for c in calls), (
        f"delete_dialog should have been called with 'testbot', calls: {calls}"
    )


# ---------------------------------------------------------------------------
# test_post_login_deletes_777000_dialog
# Requirement 4.5: delete_dialog called for 777000
# ---------------------------------------------------------------------------

def test_post_login_deletes_777000_dialog():
    """perform_post_login_cleanup calls delete_dialog with 777000.

    Validates: Requirements 4.5
    """
    mock_client = AsyncMock()
    mock_client.session = MagicMock()
    mock_client.session.filename = "/data/sessions/collector/12345678900"
    mock_client.delete_dialog = AsyncMock()
    mock_client.disconnect = AsyncMock()

    mock_router = MagicMock()
    mock_router.distribute = AsyncMock(return_value=[])

    with patch("services.login_bot.main.create_backfill_jobs", new=AsyncMock()), \
         patch("services.login_bot.session_router.SessionRouter", return_value=mock_router), \
         patch("services.login_bot.main._get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(SESSIONS_BASE_PATH="/data/sessions")
        run(perform_post_login_cleanup(mock_client, "testbot", 0))

    all_args = [c.args[0] for c in mock_client.delete_dialog.call_args_list]
    assert 777000 in all_args, (
        f"delete_dialog should have been called with 777000, got: {all_args}"
    )


# ---------------------------------------------------------------------------
# test_db_failure_does_not_block_distribution
# Requirement 10.3: DB error in save_account does not prevent distribute call
# ---------------------------------------------------------------------------

def test_db_failure_does_not_block_distribution():
    """save_account returns 0 without raising when DB connection fails.

    Validates: Requirements 10.3
    """
    session = LoginState()
    session.phone = "+12345678900"
    session.session_file_name = "12345678900"

    me = MagicMock()
    me.id = 99

    fake_db = _make_mock_db_module(side_effect=Exception("DB down"))
    original = sys.modules.get("database")
    sys.modules["database"] = fake_db
    try:
        with patch("services.login_bot.main._get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(SESSIONS_BASE_PATH="/data/sessions")
            result = run(save_account(session, me))
    finally:
        if original is None:
            sys.modules.pop("database", None)
        else:
            sys.modules["database"] = original

    assert result == 0, f"save_account should return 0 on DB failure, got {result}"


# ---------------------------------------------------------------------------
# test_backfill_insert_failure_continues
# Requirement 11.4: one failing chat does not abort remaining inserts
# ---------------------------------------------------------------------------

def test_backfill_insert_failure_continues():
    """create_backfill_jobs continues processing remaining dialogs after one fails.

    Validates: Requirements 11.4
    """
    dialog_a = MagicMock()
    dialog_a.id = 1001
    dialog_b = MagicMock()
    dialog_b.id = 1002
    dialog_c = MagicMock()
    dialog_c.id = 1003

    mock_client = AsyncMock()
    mock_client.get_dialogs = AsyncMock(return_value=[dialog_a, dialog_b, dialog_c])

    call_count = 0

    def flaky_ctx_factory():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("DB failure on first dialog")
        conn = AsyncMock()
        cur = AsyncMock()
        cur.__aenter__ = AsyncMock(return_value=cur)
        cur.__aexit__ = AsyncMock(return_value=False)
        conn.cursor = MagicMock(return_value=cur)
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    fake_db = _make_mock_db_module(ctx_factory=flaky_ctx_factory)
    original = sys.modules.get("database")
    sys.modules["database"] = fake_db
    try:
        run(create_backfill_jobs(mock_client, 1))
    finally:
        if original is None:
            sys.modules.pop("database", None)
        else:
            sys.modules["database"] = original


# ---------------------------------------------------------------------------
# Startup behaviour unit tests (task 7.2)
# Requirements: 1.2, 2.2, 2.3
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# test_failed_token_continues
# Requirement 2.2: one bad token does not abort remaining bots
# ---------------------------------------------------------------------------

def test_failed_token_continues():
    """main() continues starting remaining bots when one token fails.

    Validates: Requirements 2.2
    """
    if not main_mod._TELETHON_AVAILABLE:
        pytest.skip("telethon not installed")

    active_login_bots.clear()

    tokens = [
        {"name": "bot1", "token": "bad"},
        {"name": "bot2", "token": "good"},
    ]

    mock_settings = MagicMock()
    mock_settings.parsed_bot_tokens = tokens
    mock_settings.TG_API_ID = 12345
    mock_settings.TG_API_HASH = "abc"

    call_count = 0

    def make_client(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        client = AsyncMock()
        if call_count == 1:
            # First client: start() raises
            client.start = AsyncMock(side_effect=Exception("bad token"))
        else:
            # Second client: start() succeeds
            client.start = AsyncMock(return_value=None)
            me = MagicMock()
            me.username = "bot2"
            client.get_me = AsyncMock(return_value=me)
            client.add_event_handler = MagicMock()
            client.run_until_disconnected = AsyncMock(return_value=None)
        return client

    with patch("services.login_bot.main._get_settings", return_value=mock_settings), \
         patch("services.login_bot.main.TelegramClient", side_effect=make_client), \
         patch("services.login_bot.main.asyncio.create_task", return_value=None), \
         patch("services.login_bot.main.asyncio.gather", new=AsyncMock(return_value=None)):
        asyncio.run(main_mod.main())

    assert "bot2" in active_login_bots, (
        f"bot2 should be in active_login_bots after bot1 fails, got: {list(active_login_bots.keys())}"
    )


# ---------------------------------------------------------------------------
# test_no_bots_exits
# Requirement 2.3: all tokens fail → sys.exit(1)
# ---------------------------------------------------------------------------

def test_no_bots_exits():
    """main() calls sys.exit(1) when no bots start successfully.

    Validates: Requirements 2.3
    """
    if not main_mod._TELETHON_AVAILABLE:
        pytest.skip("telethon not installed")

    active_login_bots.clear()

    tokens = [{"name": "bot1", "token": "bad"}]

    mock_settings = MagicMock()
    mock_settings.parsed_bot_tokens = tokens
    mock_settings.TG_API_ID = 12345
    mock_settings.TG_API_HASH = "abc"

    def make_client(*args, **kwargs):
        client = AsyncMock()
        client.start = AsyncMock(side_effect=Exception("bad token"))
        return client

    with patch("services.login_bot.main._get_settings", return_value=mock_settings), \
         patch("services.login_bot.main.TelegramClient", side_effect=make_client), \
         patch("services.login_bot.main.asyncio.create_task", return_value=None), \
         patch("services.login_bot.main.asyncio.gather", new=AsyncMock(return_value=None)):
        with pytest.raises(SystemExit) as exc_info:
            asyncio.run(main_mod.main())

    assert exc_info.value.code == 1, (
        f"sys.exit should be called with code 1, got: {exc_info.value.code}"
    )


# ---------------------------------------------------------------------------
# test_no_cross_service_imports
# Requirement 1.2: login_bot has no import-time dependency on other services
# ---------------------------------------------------------------------------

def test_no_cross_service_imports():
    """login_bot.main and login_bot.session_router have no import-time
    dependency on collector/face_recognition/other service modules.

    Validates: Requirements 1.2
    """
    import services.login_bot.main  # noqa: F401
    import services.login_bot.session_router  # noqa: F401

    forbidden = [
        "collector",
        "face_recognition",
        "face_processor",
        "message_scanner",
        "media_downloader",
        "media_uploader",
    ]

    for module_name in forbidden:
        assert module_name not in sys.modules, (
            f"login_bot should not import '{module_name}' at import time, "
            f"but it was found in sys.modules"
        )


# ---------------------------------------------------------------------------
# Distribute script unit tests (task 10.2)
# Requirements: 9.3, 9.5
# ---------------------------------------------------------------------------

import importlib.util as _importlib_util
import os as _os
import types as _types


def _load_distribute_mod():
    """Load scripts/distribute_sessions.py with shared.config pre-mocked
    so the module-level import of settings does not trigger pydantic."""
    script_path = _os.path.join(
        _os.path.dirname(__file__), "..", "scripts", "distribute_sessions.py"
    )
    # Provide a fake shared.config module so the import at the top of the
    # script does not attempt to load pydantic settings.
    fake_config = _types.ModuleType("shared.config")
    fake_config.settings = MagicMock()  # placeholder; tests will patch it
    fake_shared = _types.ModuleType("shared")
    fake_shared.config = fake_config

    spec = _importlib_util.spec_from_file_location("distribute_sessions", script_path)
    mod = _importlib_util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {"shared": fake_shared, "shared.config": fake_config},
        clear=False,
    ):
        spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# test_distribute_script_missing_source
# Requirement 9.3: missing session file → non-zero exit
# ---------------------------------------------------------------------------

def test_distribute_script_missing_source(tmp_path: Path) -> None:
    """main() exits with code 1 when the source session file does not exist.

    Validates: Requirements 9.3
    """
    dist_mod = _load_distribute_mod()

    # Set up base dir with collector/ but NO session file
    collector = tmp_path / "collector"
    collector.mkdir()

    mock_settings = MagicMock()
    mock_settings.SESSIONS_BASE_PATH = str(tmp_path)

    with patch.object(dist_mod, "settings", mock_settings), \
         patch("sys.argv", ["distribute_sessions.py", "+12345678900"]):
        with pytest.raises(SystemExit) as exc_info:
            dist_mod.main()

    assert exc_info.value.code == 1, (
        f"Expected exit code 1 for missing source file, got {exc_info.value.code}"
    )


# ---------------------------------------------------------------------------
# test_distribute_script_summary
# Requirement 9.5: successful run prints per-directory summary
# ---------------------------------------------------------------------------

def test_distribute_script_summary(tmp_path: Path) -> None:
    """main() prints a per-directory success line containing the target dir name.

    Validates: Requirements 9.5
    """
    dist_mod = _load_distribute_mod()

    # Set up: collector/ with a real session file, and one target dir svc_a/
    collector = tmp_path / "collector"
    collector.mkdir()
    session_file = collector / "12345678900.session"
    session_file.write_bytes(b"fake session data")

    svc_a = tmp_path / "svc_a"
    svc_a.mkdir()

    mock_settings = MagicMock()
    mock_settings.SESSIONS_BASE_PATH = str(tmp_path)

    from io import StringIO
    captured = StringIO()
    with patch.object(dist_mod, "settings", mock_settings), \
         patch("sys.argv", ["distribute_sessions.py", "+12345678900"]), \
         patch("sys.stdout", captured):
        dist_mod.main()

    output = captured.getvalue()
    assert "svc_a" in output, (
        f"Expected 'svc_a' in output summary, got:\n{output}"
    )
    # Check for a success indicator (✓ or "copied" or "successfully")
    assert any(indicator in output for indicator in ("✓", "copied", "successfully", "success")), (
        f"Expected a success indicator in output, got:\n{output}"
    )
