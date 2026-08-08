from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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
