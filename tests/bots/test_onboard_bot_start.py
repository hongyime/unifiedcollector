from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import NetworkError, TelegramError

from src.bots import onboard_bot


def _update():
    return SimpleNamespace(message=SimpleNamespace())


def _context(args):
    return SimpleNamespace(args=args)


@pytest.mark.asyncio
async def test_send_ephemeral_uses_chat_send_not_reply(monkeypatch):
    sent = SimpleNamespace(delete=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock(return_value=sent))
    message = SimpleNamespace(chat_id=123, get_bot=lambda: bot, reply_text=AsyncMock())
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=456), message=message, get_bot=lambda: bot)

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(onboard_bot.asyncio, "create_task", fake_create_task)

    msg = await onboard_bot.send_ephemeral(update, "hello", delay=10)

    assert msg is sent
    bot.send_message.assert_awaited_once_with(chat_id=456, text="hello")
    message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_without_deeplink_stays_passive(monkeypatch):
    delete_user_message = AsyncMock()
    send_ephemeral = AsyncMock()
    startcollector = AsyncMock()
    monkeypatch.setattr(onboard_bot, "delete_user_message", delete_user_message)
    monkeypatch.setattr(onboard_bot, "send_ephemeral", send_ephemeral)
    monkeypatch.setattr(onboard_bot, "startcollector", startcollector)

    update = _update()
    result = await onboard_bot.start(update, _context([]))

    delete_user_message.assert_awaited_once()
    send_ephemeral.assert_awaited_once_with(update, "Send /startcollector to add an account.")
    startcollector.assert_not_awaited()
    assert result == onboard_bot.ConversationHandler.END


@pytest.mark.asyncio
async def test_start_ignores_unrecognised_deeplink(monkeypatch):
    monkeypatch.setattr(onboard_bot, "delete_user_message", AsyncMock())
    send_ephemeral = AsyncMock()
    startcollector = AsyncMock()
    monkeypatch.setattr(onboard_bot, "send_ephemeral", send_ephemeral)
    monkeypatch.setattr(onboard_bot, "startcollector", startcollector)
    update = _update()

    result = await onboard_bot.start(update, _context(["anything-else"]))

    send_ephemeral.assert_awaited_once_with(update, "Send /startcollector to add an account.")
    startcollector.assert_not_awaited()
    assert result == onboard_bot.ConversationHandler.END


@pytest.mark.asyncio
@pytest.mark.parametrize("arg", ["migrate", "verify", "unlock"])
async def test_start_deeplink_auto_triggers_startcollector(monkeypatch, arg):
    monkeypatch.setattr(onboard_bot, "delete_user_message", AsyncMock())
    send_ephemeral = AsyncMock()
    startcollector = AsyncMock(return_value=onboard_bot.ASK_PHONE)
    monkeypatch.setattr(onboard_bot, "send_ephemeral", send_ephemeral)
    monkeypatch.setattr(onboard_bot, "startcollector", startcollector)
    update = _update()
    context = _context([arg])

    result = await onboard_bot.start(update, context)

    startcollector.assert_awaited_once_with(update, context)
    send_ephemeral.assert_not_awaited()
    assert result == onboard_bot.ASK_PHONE


def test_build_application_keeps_start_entry_in_conversation_only():
    app = onboard_bot.build_application("123:ABC", "testbot")

    start_handlers = [
        handler
        for group in app.handlers.values()
        for handler in group
        if getattr(handler, "commands", None) and "start" in handler.commands
    ]

    assert len(start_handlers) == 0
    conv_handlers = [
        handler
        for group in app.handlers.values()
        for handler in group
        if isinstance(handler, onboard_bot.ConversationHandler)
    ]
    assert len(conv_handlers) == 1
    entry_commands = {
        command
        for handler in conv_handlers[0].entry_points
        for command in getattr(handler, "commands", set())
    }
    assert {"start", "startcollector"} <= entry_commands


def test_polling_error_callback_summarizes_transient_bad_gateway(caplog):
    caplog.set_level("WARNING", logger=onboard_bot.__name__)

    onboard_bot.polling_error_callback(NetworkError("Bad Gateway"))

    assert "Telegram polling transient NetworkError: Bad Gateway" in caplog.text
    assert "Traceback" not in caplog.text


def test_polling_error_callback_logs_non_transient_errors(caplog):
    caplog.set_level("ERROR", logger=onboard_bot.__name__)

    onboard_bot.polling_error_callback(TelegramError("boom"))

    assert "Telegram polling failed: boom" in caplog.text
