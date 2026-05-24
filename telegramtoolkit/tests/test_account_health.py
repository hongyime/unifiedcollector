#!/usr/bin/env python3
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.core.state_manager as state_manager_module
from src.core.account_health import (
    AccountFailureError,
    AccountHealthPolicy,
    classify_account_error,
    summarize_account_health_drift,
)
from src.core.message_orchestrator import MessageOrchestrator
from src.core.parallel_processor import TelegramParallelProcessor
from src.core.state_manager import StateManager, shutdown_state_manager
from src.managers.processors.user_analyzer_processor import UserAnalyzerProcessor
from tests.test_user_analyzer_processor import FakeClient, FakeMessage, FakeUser


def test_classify_account_error_matches_disconnect_and_auth():
    disconnected = classify_account_error(RuntimeError("Cannot send requests while disconnected"))
    assert disconnected is not None
    assert disconnected.code == "disconnected"
    assert disconnected.reconnectable is True

    auth = classify_account_error(RuntimeError("AUTH_KEY_UNREGISTERED"))
    assert auth is not None
    assert auth.code == "auth"
    assert auth.reconnectable is False

    assert classify_account_error(RuntimeError("USER_NOT_PARTICIPANT")) is None


@pytest.mark.asyncio
async def test_account_health_retires_non_reconnectable_account():
    policy = AccountHealthPolicy()
    client = MagicMock()
    client.disconnect = AsyncMock()

    recovered = await policy.handle_account_failure(
        client,
        {"name": "acct1", "phone": "+1000"},
        RuntimeError("AUTH_KEY_UNREGISTERED"),
        "startup",
    )

    assert recovered is False
    assert policy.is_retired("acct1") is True


def test_user_analyzer_escalates_disconnected_reply_lookup():
    shutdown_state_manager()
    StateManager._instance = None
    state = StateManager(":memory:")
    state._shutdown = True
    state_manager_module._state_manager = state

    processor = UserAnalyzerProcessor()
    processor.state = state
    processor.max_retries = 1
    processor.retry_delay = 0

    message = FakeMessage(
        500,
        text="reply fault",
        sender_id=1,
        reply_to_msg_id=44,
        reply_message=None,
    )

    async def disconnected_reply():
        raise RuntimeError("Cannot send requests while disconnected")

    message.get_reply_message = disconnected_reply
    client = FakeClient(entities={1: FakeUser(1, "sender")})

    with pytest.raises(AccountFailureError):
        asyncio.run(
            processor.process_message(
                {
                    "message": message,
                    "group_id": "1001",
                    "group_name": "Graceful Group",
                    "account_name": "acct1",
                    "client": client,
                }
            )
        )

    shutdown_state_manager()
    StateManager._instance = None
    state_manager_module._state_manager = None


@pytest.mark.asyncio
async def test_orchestrator_stops_after_account_retirement():
    orchestrator = MessageOrchestrator()
    orchestrator._discover_scan_targets = AsyncMock(
        return_value=[
            {"entity": object(), "group_id": "1", "group_name": "One"},
            {"entity": object(), "group_id": "2", "group_name": "Two"},
        ]
    )
    orchestrator.scan_group = AsyncMock(
        side_effect=AccountFailureError("acct1", RuntimeError("Cannot send requests while disconnected"), "scan_group")
    )
    orchestrator.account_health.ensure_connected = AsyncMock(return_value=True)
    orchestrator.account_health.handle_account_failure = AsyncMock(return_value=False)

    with patch("src.core.message_orchestrator.cleanup_scan_target", new=AsyncMock()):
        await orchestrator.scan_account(MagicMock(), {"name": "acct1"}, None)

    assert orchestrator.scan_group.await_count == 1
    orchestrator.account_health.handle_account_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_parallel_processor_retired_account_stops_work():
    processor = TelegramParallelProcessor()
    processor.account_health.ensure_connected = AsyncMock(return_value=True)
    processor.account_health.handle_account_failure = AsyncMock(return_value=False)

    client = MagicMock()
    target = {"id": "42", "title": "Target"}

    async def operation_func(*args, **kwargs):
        raise RuntimeError("Cannot send requests while disconnected")

    result = await processor._safe_operation_wrapper(
        client=client,
        account={"name": "acct1", "phone": "+1000"},
        account_name="acct1",
        target=target,
        operation_func=operation_func,
        account_semaphore=asyncio.Semaphore(1),
        global_semaphore=asyncio.Semaphore(1),
    )

    assert result is None
    processor.account_health.handle_account_failure.assert_awaited_once()


def test_drift_summary_mentions_unified_policy():
    summary = summarize_account_health_drift()
    assert "single account-health policy" in summary or "share a single account-health policy" in summary
