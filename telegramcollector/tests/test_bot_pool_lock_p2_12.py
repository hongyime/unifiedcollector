"""
Tests for P2.12: BotPool.__init__() asyncio.Lock() deferred creation.

Validates: bugfix.md F-021
- asyncio.Lock() is NOT created in __init__()
- Lock is created lazily on first async use via _get_async_lock property
- No RuntimeError when BotPool is instantiated at module level (before event loop)
- Lock works correctly once created inside an async context
"""
import asyncio
import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def reset_pool():
    """Reset singleton before/after each test."""
    from shared.bot_pool import BotPool
    BotPool.reset_instance()
    yield
    BotPool.reset_instance()


class TestBotPoolLockDeferredCreation:
    """Validates: bugfix.md F-021 - Fix Checking"""

    def test_async_lock_is_none_after_init(self):
        """
        Validates: bugfix.md F-021
        asyncio.Lock() must NOT be created in __init__().
        _async_lock should be None immediately after construction.
        """
        from shared.bot_pool import BotPool

        pool = BotPool()
        assert pool._async_lock is None, (
            "asyncio.Lock() must not be created in __init__() — "
            "_async_lock should be None until first async use"
        )

    def test_no_runtime_error_on_module_level_instantiation(self):
        """
        Validates: bugfix.md F-021
        Instantiating BotPool outside an event loop (e.g. at module level)
        must NOT raise RuntimeError.
        """
        from shared.bot_pool import BotPool

        # Simulate module-level instantiation: no running event loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        assert loop is None, "This test must run outside a running event loop"

        # Should not raise RuntimeError
        try:
            pool = BotPool()
        except RuntimeError as e:
            pytest.fail(f"BotPool() raised RuntimeError at module level: {e}")

        assert pool is not None

    def test_get_async_lock_creates_lock_lazily(self):
        """
        Validates: bugfix.md F-021
        _get_async_lock property creates the lock on first access inside
        an async context, and caches it for subsequent calls.
        """
        from shared.bot_pool import BotPool

        async def _inner():
            pool = BotPool()
            assert pool._async_lock is None

            lock1 = pool._get_async_lock
            assert isinstance(lock1, asyncio.Lock), "Should return an asyncio.Lock"
            assert pool._async_lock is not None, "Lock should be cached after first access"

            lock2 = pool._get_async_lock
            assert lock1 is lock2, "Same lock instance should be returned on subsequent calls"

        asyncio.run(_inner())

    def test_lock_is_functional_after_lazy_creation(self):
        """
        Validates: bugfix.md F-021
        The lazily-created lock must be acquirable and releasable correctly.
        """
        from shared.bot_pool import BotPool

        async def _inner():
            pool = BotPool()
            lock = pool._get_async_lock

            acquired = False
            async with lock:
                acquired = True

            assert acquired, "Lock should be acquirable after lazy creation"

        asyncio.run(_inner())

    def test_lock_prevents_concurrent_access(self):
        """
        Validates: bugfix.md F-021
        The lazily-created lock must correctly serialize concurrent coroutines.
        """
        from shared.bot_pool import BotPool

        async def _inner():
            pool = BotPool()
            lock = pool._get_async_lock

            results = []

            async def task(n):
                async with lock:
                    results.append(f"start-{n}")
                    await asyncio.sleep(0)  # yield to event loop
                    results.append(f"end-{n}")

            await asyncio.gather(task(1), task(2), task(3))

            # Each task's start and end must be adjacent (no interleaving)
            for i in range(0, len(results), 2):
                start = results[i]
                end = results[i + 1]
                n = start.split("-")[1]
                assert start == f"start-{n}"
                assert end == f"end-{n}", (
                    f"Lock did not serialize access: {results}"
                )

        asyncio.run(_inner())

    def test_singleton_preserves_lock_across_calls(self):
        """
        Validates: bugfix.md F-021 - Preservation Checking
        BotPool singleton returns the same lock instance across multiple
        calls to _get_async_lock within the same event loop.
        """
        from shared.bot_pool import BotPool

        async def _inner():
            pool_a = BotPool()
            pool_b = BotPool()  # same singleton

            lock_a = pool_a._get_async_lock
            lock_b = pool_b._get_async_lock

            assert lock_a is lock_b, (
                "Singleton must return the same lock instance"
            )

        asyncio.run(_inner())

    def test_reset_instance_clears_lock(self):
        """
        Validates: bugfix.md F-021 - Preservation Checking
        After reset_instance(), a new BotPool starts with _async_lock = None again.
        """
        from shared.bot_pool import BotPool

        async def _inner():
            pool = BotPool()
            _ = pool._get_async_lock  # create the lock
            assert pool._async_lock is not None

        asyncio.run(_inner())

        # Reset and verify fresh state
        BotPool.reset_instance()
        pool2 = BotPool()
        assert pool2._async_lock is None, (
            "After reset_instance(), _async_lock should be None again"
        )
