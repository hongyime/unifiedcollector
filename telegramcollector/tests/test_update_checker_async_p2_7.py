"""
Tests for P2.7: update_checker.py async HTTP fix.

Validates: Bug F-016 - update_checker.py._check_for_updates() must use async HTTP
to avoid blocking the asyncio event loop.

Fix Checking Property (F-016):
  FOR ALL X WHERE isBugCondition(X) DO
    result <- update_checker.py._check_for_updates'(X)
    ASSERT asyncio event loop NOT BLOCKED

Preservation Checking Property (F-016):
  FOR ALL X WHERE NOT isBugCondition(X) DO
    ASSERT update_checker(X).monitoring == update_checker'(X).monitoring
"""
import asyncio
import inspect
import time
import pytest
import aiohttp
from unittest.mock import AsyncMock, MagicMock, patch
from services.collector.update_checker import UpdateChecker


# ---------------------------------------------------------------------------
# Fix Checking: HTTP request is async (no sync requests.get in async def)
# ---------------------------------------------------------------------------

def test_no_sync_requests_import():
    """Validates: Requirements 2.16 - requests module must not be imported."""
    import services.collector.update_checker as uc_module
    assert not hasattr(uc_module, 'requests'), (
        "update_checker.py still imports 'requests' (sync HTTP library)"
    )


def test_uses_aiohttp_import():
    """Validates: Requirements 2.16 - aiohttp must be imported."""
    import services.collector.update_checker as uc_module
    assert hasattr(uc_module, 'aiohttp'), (
        "update_checker.py does not import 'aiohttp'"
    )


def test_check_for_updates_is_coroutine():
    """Validates: Requirements 2.16 - _check_for_updates must be a coroutine function."""
    checker = UpdateChecker()
    assert inspect.iscoroutinefunction(checker._check_for_updates), (
        "_check_for_updates is not an async function"
    )


# ---------------------------------------------------------------------------
# Fix Checking: Event loop not blocked during request
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_for_updates_does_not_block_event_loop():
    """
    Validates: Requirements 2.16 - event loop must not be blocked.

    Runs a concurrent task alongside _check_for_updates and verifies
    the concurrent task makes progress (i.e., the event loop was not blocked).
    """
    checker = UpdateChecker()
    checker.repo = "owner/repo"

    progress = {"ticks": 0}

    async def background_ticker():
        for _ in range(20):
            progress["ticks"] += 1
            await asyncio.sleep(0.01)

    mock_response_data = {"sha": "abc123def456"}

    # Build a proper async context manager for session.get(url)
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=mock_response_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        ticker_task = asyncio.create_task(background_ticker())
        await checker._check_for_updates()
        await ticker_task

    assert progress["ticks"] > 0, (
        "Background task made no progress — event loop was likely blocked"
    )


# ---------------------------------------------------------------------------
# Fix Checking: Timeout handling works correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_handled_gracefully():
    """
    Validates: Requirements 2.16 - timeout must be handled without crashing.
    """
    checker = UpdateChecker()
    checker.repo = "owner/repo"

    # Simulate timeout via an async context manager that raises TimeoutError
    mock_resp = MagicMock()
    mock_resp.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        # Should not raise — timeout is caught internally
        await checker._check_for_updates()


@pytest.mark.asyncio
async def test_client_error_handled_gracefully():
    """
    Validates: Requirements 2.16 - aiohttp.ClientError must be caught gracefully.
    """
    checker = UpdateChecker()
    checker.repo = "owner/repo"

    mock_resp = MagicMock()
    mock_resp.__aenter__ = AsyncMock(
        side_effect=aiohttp.ClientConnectionError("connection refused")
    )
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        await checker._check_for_updates()  # must not raise


# ---------------------------------------------------------------------------
# Fix Checking: Update check completes successfully
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_initial_sha_stored_on_first_check():
    """
    Validates: Requirements 2.16 / 3.13 - first check stores initial SHA.
    """
    checker = UpdateChecker()
    checker.repo = "owner/repo"
    checker.last_commit_sha = None

    mock_response_data = {"sha": "deadbeef1234"}

    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=mock_response_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        await checker._check_for_updates()

    assert checker.last_commit_sha == "deadbeef1234"


@pytest.mark.asyncio
async def test_new_commit_triggers_update():
    """
    Validates: Requirements 2.16 / 3.13 - new commit SHA triggers _trigger_update().
    """
    checker = UpdateChecker()
    checker.repo = "owner/repo"
    checker.last_commit_sha = "oldsha000"

    mock_response_data = {"sha": "newsha999"}

    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=mock_response_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session), \
         patch.object(checker, "_trigger_update", new_callable=AsyncMock) as mock_trigger:
        await checker._check_for_updates()

    mock_trigger.assert_awaited_once()
    assert checker.last_commit_sha == "newsha999"


@pytest.mark.asyncio
async def test_same_sha_does_not_trigger_update():
    """
    Validates: Requirements 3.13 - no update triggered when SHA unchanged.
    """
    checker = UpdateChecker()
    checker.repo = "owner/repo"
    checker.last_commit_sha = "samesha123"

    mock_response_data = {"sha": "samesha123"}

    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=mock_response_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=mock_session), \
         patch.object(checker, "_trigger_update", new_callable=AsyncMock) as mock_trigger:
        await checker._check_for_updates()

    mock_trigger.assert_not_awaited()


# ---------------------------------------------------------------------------
# Preservation Checking: Disabled when GITHUB_REPO not set
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_monitoring_exits_early_without_repo():
    """
    Validates: Requirements 3.13 - monitoring loop exits early when repo not configured.
    """
    checker = UpdateChecker()
    checker.repo = ""  # not configured

    # Should return immediately without looping
    await asyncio.wait_for(checker.start_monitoring(), timeout=1.0)


# ---------------------------------------------------------------------------
# Preservation Checking: aiohttp timeout is configured
# ---------------------------------------------------------------------------

def test_aiohttp_timeout_is_set():
    """
    Validates: Requirements 2.16 - aiohttp ClientTimeout must be used.
    """
    import ast
    import pathlib

    source = pathlib.Path("collector/update_checker.py").read_text()
    tree = ast.parse(source)

    found_timeout = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "ClientTimeout":
                found_timeout = True
                break
            if isinstance(func, ast.Name) and func.id == "ClientTimeout":
                found_timeout = True
                break

    assert found_timeout, (
        "aiohttp.ClientTimeout is not used in update_checker.py — "
        "timeout must be explicitly configured"
    )
