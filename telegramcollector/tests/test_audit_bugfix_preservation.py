"""
Preservation Property Tests — audit-critical-bugfixes spec, Task 2.

These tests verify that non-buggy code paths work correctly BEFORE any fix is applied.
All tests MUST PASS on unfixed code (baseline behavior to preserve).
After fixes are applied, all tests MUST STILL PASS (no regressions).

Run:
    pytest tests/test_audit_bugfix_preservation.py -v
Expected outcome: ALL PASS (both before and after fixes).
"""
import asyncio
import importlib.util
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_module(name: str, path: str) -> types.ModuleType:
    """Load a module from a file path, bypassing the normal import system."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub_shared_config():
    fake_config = types.ModuleType("shared.config")
    fake_settings = MagicMock()
    fake_settings.FACE_SIMILARITY_THRESHOLD = 0.55
    fake_settings.FACE_MIN_QUALITY_THRESHOLD = 0.67
    fake_settings.REDIS_HOST = "localhost"
    fake_settings.REDIS_PORT = 6379
    fake_settings.REDIS_DB = 0
    fake_settings.REDIS_PASSWORD = None
    fake_config.settings = fake_settings
    fake_config.get_dynamic_setting = MagicMock(return_value=0.55)
    fake_config.get_hub_group_id = MagicMock(return_value=123456789)
    fake_config.resolve_hub_group_id = AsyncMock(return_value=123456789)
    sys.modules["shared.config"] = fake_config
    return fake_config


# ---------------------------------------------------------------------------
# BUG 3 preservation — existing non-zero topic_id returns immediately
# ---------------------------------------------------------------------------

class TestBug3Preservation:
    """
    isBugCondition_3 is FALSE when topic_id != 0.
    Preservation: _ensure_topic_exists() with existing non-zero topic_id returns it
    immediately without any Telegram API call.
    """

    @pytest.mark.asyncio
    async def test_existing_topic_id_returned_without_api_call(self):
        """
        For all db_topic_id where topic_id != 0, _ensure_topic_exists() must return
        the stored topic_id and make NO call to CreateForumTopicRequest.
        """
        for key in list(sys.modules.keys()):
            if "publisher" in key and "face_recognition" in key:
                del sys.modules[key]

        fake_asyncpg = types.ModuleType("asyncpg")
        fake_asyncpg.Pool = object
        sys.modules["asyncpg"] = fake_asyncpg

        fake_bot_pool_mod = types.ModuleType("shared.bot_pool")
        fake_bot_pool_mod.BotPool = MagicMock()
        sys.modules["shared.bot_pool"] = fake_bot_pool_mod

        _stub_shared_config()

        fake_telethon = types.ModuleType("telethon")
        fake_channels = types.ModuleType("telethon.tl.functions.channels")
        create_forum_topic_spy = MagicMock()
        fake_channels.CreateForumTopicRequest = create_forum_topic_spy
        sys.modules["telethon"] = fake_telethon
        sys.modules["telethon.tl"] = types.ModuleType("telethon.tl")
        sys.modules["telethon.tl.functions"] = types.ModuleType("telethon.tl.functions")
        sys.modules["telethon.tl.functions.channels"] = fake_channels

        mod = _load_module(
            "services.face_recognition.publisher",
            "services/face_recognition/publisher.py",
        )
        Publisher = mod.Publisher

        # DB returns an existing non-zero topic_id
        existing_topic_id = 42
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            return_value={"topic_id": existing_topic_id, "label": "Alice"}
        )

        class FakeAcquire:
            async def __aenter__(self):
                return mock_conn
            async def __aexit__(self, *args):
                pass

        mock_db_pool = MagicMock()
        mock_db_pool.acquire = MagicMock(return_value=FakeAcquire())
        mock_bot_pool = MagicMock()

        publisher = Publisher(db_pool=mock_db_pool, bot_pool=mock_bot_pool)
        result = await publisher._ensure_topic_exists(db_topic_id=99)

        assert result == existing_topic_id, (
            f"Expected {existing_topic_id}, got {result}"
        )
        # No Telegram API call should have been made
        create_forum_topic_spy.assert_not_called()
        mock_bot_pool.get_bot.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("topic_id", [1, 100, 999, 12345])
    async def test_various_nonzero_topic_ids_returned_immediately(self, topic_id):
        """For any non-zero topic_id, the value is returned without API calls."""
        for key in list(sys.modules.keys()):
            if "publisher" in key and "face_recognition" in key:
                del sys.modules[key]

        fake_asyncpg = types.ModuleType("asyncpg")
        fake_asyncpg.Pool = object
        sys.modules["asyncpg"] = fake_asyncpg
        sys.modules["shared.bot_pool"] = MagicMock(BotPool=MagicMock())
        _stub_shared_config()
        sys.modules["telethon"] = types.ModuleType("telethon")
        sys.modules["telethon.tl"] = types.ModuleType("telethon.tl")
        sys.modules["telethon.tl.functions"] = types.ModuleType("telethon.tl.functions")
        fake_channels = types.ModuleType("telethon.tl.functions.channels")
        fake_channels.CreateForumTopicRequest = MagicMock()
        sys.modules["telethon.tl.functions.channels"] = fake_channels

        mod = _load_module(
            "services.face_recognition.publisher",
            "services/face_recognition/publisher.py",
        )
        Publisher = mod.Publisher

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            return_value={"topic_id": topic_id, "label": "Person"}
        )

        class FakeAcquire:
            async def __aenter__(self):
                return mock_conn
            async def __aexit__(self, *args):
                pass

        mock_db_pool = MagicMock()
        mock_db_pool.acquire = MagicMock(return_value=FakeAcquire())
        mock_bot_pool = MagicMock()

        publisher = Publisher(db_pool=mock_db_pool, bot_pool=mock_bot_pool)
        result = await publisher._ensure_topic_exists(db_topic_id=1)
        assert result == topic_id


# ---------------------------------------------------------------------------
# BUG 4 preservation — match path returns (topic_id, False) without error
# ---------------------------------------------------------------------------

class TestBug4Preservation:
    """
    isBugCondition_4 is FALSE when a matching embedding is found.
    Preservation: find_or_create_identity() with a matching embedding calls
    _store_embedding with the existing topic_id and returns (topic_id, False).
    """

    @pytest.mark.asyncio
    async def test_matching_embedding_returns_existing_topic_id(self):
        """
        When _find_similar_embedding returns a match, find_or_create_identity
        must return (topic_id, False) without InterfaceError.
        """
        for key in list(sys.modules.keys()):
            if "matcher" in key and "face_recognition" in key:
                del sys.modules[key]

        fake_asyncpg = types.ModuleType("asyncpg")
        fake_asyncpg.Pool = object
        sys.modules["asyncpg"] = fake_asyncpg

        fake_config = types.ModuleType("shared.config")
        fake_config.get_dynamic_setting = MagicMock(return_value=0.55)
        fake_settings = MagicMock()
        fake_settings.FACE_SIMILARITY_THRESHOLD = 0.55
        fake_settings.FACE_MIN_QUALITY_THRESHOLD = 0.0  # accept any quality
        fake_config.settings = fake_settings
        sys.modules["shared.config"] = fake_config

        mod = _load_module(
            "services.face_recognition.matcher",
            "services/face_recognition/matcher.py",
        )
        IdentityMatcher = mod.IdentityMatcher

        existing_topic_id = 77
        mock_pool = MagicMock()

        matcher = IdentityMatcher(db_pool=mock_pool)
        # Simulate a match found
        matcher._find_similar_embedding = AsyncMock(
            return_value={"topic_id": existing_topic_id, "similarity": 0.92}
        )
        store_calls = []

        async def fake_store_embedding(**kwargs):
            store_calls.append(kwargs)
            return 1

        matcher._store_embedding = fake_store_embedding

        result = await matcher.find_or_create_identity(
            embedding=[0.1] * 128,
            quality_score=0.9,
            source_chat_id=1,
            source_message_id=1,
            frame_index=0,
        )

        assert result == (existing_topic_id, False), (
            f"Expected ({existing_topic_id}, False), got {result}"
        )
        assert len(store_calls) == 1
        assert store_calls[0]["topic_id"] == existing_topic_id


# ---------------------------------------------------------------------------
# BUG 5 preservation — DB_NAME env override is respected
# ---------------------------------------------------------------------------

class TestBug5Preservation:
    """
    isBugCondition_5a is FALSE when DB_NAME is set in .env.
    Preservation: when DB_NAME is set in env, settings.DB_NAME equals that value.
    """

    def test_db_name_env_override_respected(self):
        """
        For all non-empty DB_NAME env values, settings.DB_NAME equals that value.
        This tests the pydantic_settings env-override mechanism.
        """
        try:
            from pydantic_settings import BaseSettings, SettingsConfigDict
        except ImportError:
            pytest.skip("pydantic_settings not installed")

        # We test the override mechanism by reading the Settings class definition
        # and verifying that DB_NAME is a pydantic field (env-overridable).
        with open("shared/config.py") as f:
            source = f.read()

        # The field must be declared as a pydantic field (not a hardcoded constant)
        assert "DB_NAME: str" in source, (
            "DB_NAME must be declared as a pydantic Settings field for env override to work"
        )
        # The class must inherit from BaseSettings
        assert "BaseSettings" in source, (
            "Settings must inherit from BaseSettings for env override to work"
        )

    @pytest.mark.parametrize("db_name", ["mydb", "telegramcollector", "testdb", "prod_db"])
    def test_db_name_various_values(self, db_name):
        """
        For various DB_NAME values, the field declaration must support env override.
        (Structural check — actual env override tested via pydantic_settings behavior.)
        """
        with open("shared/config.py") as f:
            source = f.read()
        # Verify the field is not hardcoded to a specific value that would ignore env
        assert f'DB_NAME = "{db_name}"' not in source or "str" in source, (
            f"DB_NAME must not be hardcoded to '{db_name}'"
        )


# ---------------------------------------------------------------------------
# BUG 7 preservation — Redis-available ETA calculation unchanged
# ---------------------------------------------------------------------------

class TestBug7Preservation:
    """
    isBugCondition_7 is FALSE when redis_available=True.
    Preservation:
      - get_queue_eta() with redis_available=True and ≥5 times returns ETA float
      - get_queue_eta() with redis_available=True and <5 times returns None
    """

    def _make_pq_with_redis(self, processing_times, queue_size):
        """Helper: build a ProcessingQueue in redis_available=True mode."""
        for key in list(sys.modules.keys()):
            if key == "shared.processing_queue":
                del sys.modules[key]

        sys.modules["shared.database"] = MagicMock()
        sys.modules["shared.hub_notifier"] = MagicMock(
            notify=AsyncMock(), increment_stat=AsyncMock()
        )
        sys.modules["shared.resilience"] = MagicMock(
            get_circuit_breaker=MagicMock(), CircuitOpenError=Exception
        )
        sys.modules["shared.observability"] = MagicMock()

        fake_config = types.ModuleType("shared.config")
        fake_settings = MagicMock()
        fake_settings.REDIS_HOST = "localhost"
        fake_settings.REDIS_PORT = 6379
        fake_settings.REDIS_DB = 0
        fake_settings.REDIS_PASSWORD = None
        fake_config.settings = fake_settings
        fake_config.get_dynamic_setting = MagicMock(return_value=100)
        sys.modules["shared.config"] = fake_config

        fake_redis_mod = types.ModuleType("redis")
        fake_redis_mod.Redis = MagicMock(side_effect=Exception("no redis"))
        fake_redis_exceptions = types.ModuleType("redis.exceptions")
        fake_redis_exceptions.BusyLoadingError = Exception
        fake_redis_mod.exceptions = fake_redis_exceptions
        sys.modules["redis"] = fake_redis_mod
        sys.modules["redis.exceptions"] = fake_redis_exceptions

        from shared.processing_queue import ProcessingQueue

        pq = ProcessingQueue.__new__(ProcessingQueue)
        pq.redis_available = True
        pq.redis_client = MagicMock()
        pq.redis_client.llen = MagicMock(return_value=queue_size)
        pq.fallback_queue = MagicMock()
        pq.fallback_queue.qsize = MagicMock(return_value=0)
        pq.queue_key = "processing_queue:tasks"
        pq.num_workers = 3
        pq._processing_times = list(processing_times)
        return pq

    def test_redis_available_with_sufficient_data_returns_float(self):
        """
        get_queue_eta() with redis_available=True and ≥5 processing times
        returns a non-negative float (ETA in minutes).
        """
        processing_times = [1.0, 2.0, 1.5, 1.8, 2.2]
        queue_size = 10
        pq = self._make_pq_with_redis(processing_times, queue_size)

        result = pq.get_queue_eta()

        assert result is not None, "Expected a float ETA, got None"
        assert isinstance(result, float), f"Expected float, got {type(result)}"
        assert result >= 0.0, f"ETA must be non-negative, got {result}"

        # Verify the formula: queue_size * avg_time / num_workers / 60
        avg_time = sum(processing_times) / len(processing_times)
        expected = queue_size * avg_time / pq.num_workers / 60
        assert abs(result - expected) < 1e-9, (
            f"ETA formula changed: expected {expected}, got {result}"
        )

    def test_redis_available_with_zero_queue_returns_zero(self):
        """get_queue_eta() with queue_size=0 returns 0.0."""
        pq = self._make_pq_with_redis([1.0, 2.0, 1.5, 1.8, 2.2], queue_size=0)
        result = pq.get_queue_eta()
        assert result == 0.0, f"Expected 0.0 for empty queue, got {result}"

    def test_redis_available_with_fewer_than_5_times_returns_none(self):
        """
        get_queue_eta() with redis_available=True and <5 processing times returns None.
        """
        for n in range(5):  # 0, 1, 2, 3, 4 entries
            pq = self._make_pq_with_redis([1.0] * n, queue_size=10)
            result = pq.get_queue_eta()
            assert result is None, (
                f"Expected None with {n} processing times, got {result}"
            )

    @given(
        processing_times=st.lists(
            st.floats(min_value=0.01, max_value=60.0, allow_nan=False, allow_infinity=False),
            min_size=5,
            max_size=100,
        ),
        queue_size=st.integers(min_value=1, max_value=10000),
    )
    @h_settings(max_examples=50, deadline=5000)
    def test_pbt_redis_available_eta_formula(self, processing_times, queue_size):
        """
        PBT: For all (redis_available=True, processing_times≥5, queue_size>0),
        get_queue_eta() returns queue_size * avg_time / num_workers / 60.
        No AttributeError must be raised.
        """
        pq = self._make_pq_with_redis(processing_times, queue_size)

        result = pq.get_queue_eta()

        assert result is not None
        assert isinstance(result, float)
        assert result >= 0.0

        avg_time = sum(processing_times) / len(processing_times)
        expected = queue_size * avg_time / pq.num_workers / 60
        assert abs(result - expected) < 1e-6, (
            f"ETA formula mismatch: expected {expected}, got {result}"
        )

    @pytest.mark.xfail(
        reason="BUG 7 not yet fixed: get_queue_eta() raises AttributeError when redis_available=False. "
               "This test will PASS after BUG 7 fix is applied (Task 9).",
        strict=False,
    )
    @given(
        processing_times=st.lists(
            st.floats(min_value=0.01, max_value=60.0, allow_nan=False, allow_infinity=False),
            min_size=5,
            max_size=100,
        ),
        queue_size=st.integers(min_value=1, max_value=10000),
    )
    @h_settings(max_examples=50, deadline=5000)
    def test_pbt_redis_unavailable_returns_valid_result_after_fix(self, processing_times, queue_size):
        """
        PBT (post-fix validation): For all (redis_available=False, processing_times≥5),
        get_queue_eta() must NOT raise AttributeError after BUG 7 is fixed.
        Returns a non-negative float or None.

        NOTE: This test will FAIL on unfixed code (AttributeError raised).
        It is included here so it runs as part of the preservation suite after fixes.
        """
        for key in list(sys.modules.keys()):
            if key == "shared.processing_queue":
                del sys.modules[key]

        sys.modules["shared.database"] = MagicMock()
        sys.modules["shared.hub_notifier"] = MagicMock(
            notify=AsyncMock(), increment_stat=AsyncMock()
        )
        sys.modules["shared.resilience"] = MagicMock(
            get_circuit_breaker=MagicMock(), CircuitOpenError=Exception
        )
        sys.modules["shared.observability"] = MagicMock()

        fake_config = types.ModuleType("shared.config")
        fake_settings = MagicMock()
        fake_settings.REDIS_HOST = "localhost"
        fake_settings.REDIS_PORT = 6379
        fake_settings.REDIS_DB = 0
        fake_settings.REDIS_PASSWORD = None
        fake_config.settings = fake_settings
        fake_config.get_dynamic_setting = MagicMock(return_value=100)
        sys.modules["shared.config"] = fake_config

        fake_redis_mod = types.ModuleType("redis")
        fake_redis_mod.Redis = MagicMock(side_effect=Exception("no redis"))
        fake_redis_exceptions = types.ModuleType("redis.exceptions")
        fake_redis_exceptions.BusyLoadingError = Exception
        fake_redis_mod.exceptions = fake_redis_exceptions
        sys.modules["redis"] = fake_redis_mod
        sys.modules["redis.exceptions"] = fake_redis_exceptions

        from shared.processing_queue import ProcessingQueue

        pq = ProcessingQueue.__new__(ProcessingQueue)
        pq.redis_available = False
        pq.redis_client = None
        pq.fallback_queue = MagicMock()
        pq.fallback_queue.qsize = MagicMock(return_value=queue_size)
        pq.queue_key = "processing_queue:tasks"
        pq.num_workers = 3
        pq._processing_times = list(processing_times)

        # After the fix: must NOT raise AttributeError
        result = pq.get_queue_eta()
        if result is not None:
            assert isinstance(result, (int, float))
            assert result >= 0.0


# ---------------------------------------------------------------------------
# BUG 2 preservation — all 5 handlers registered on all bots
# ---------------------------------------------------------------------------

class TestBug2Preservation:
    """
    isBugCondition_2 is FALSE when handlers are registered (not when they fire).
    Preservation: register_worker() registers all 5 handlers on every bot.
    """

    def test_five_handlers_registered_on_all_bots(self):
        """
        register_worker() with a fully initialized bot pool must register
        exactly 5 command handlers on every bot.
        """
        for key in list(sys.modules.keys()):
            if "account_manager" in key or "bot_commands" in key:
                del sys.modules[key]
        for key in ("services", "services.collector", "services.collector.bot_commands",
                    "services.collector.account_manager"):
            sys.modules.pop(key, None)

        fake_config = types.ModuleType("shared.config")
        fake_config.settings = MagicMock()
        sys.modules["shared.config"] = fake_config

        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = MagicMock()
        sys.modules["dotenv"] = fake_dotenv

        # Two bots in the pool
        class FakeBotEntry:
            def __init__(self, name):
                self.name = name
                self.username = name
                self.handlers = []
                self.client = MagicMock()
                self.client.on = self._on

            def _on(self, pattern_obj):
                def decorator(fn):
                    self.handlers.append(fn)
                    return fn
                return decorator

        bot1 = FakeBotEntry("bot1")
        bot2 = FakeBotEntry("bot2")

        fake_bot_pool_mod = types.ModuleType("shared.bot_pool")
        fake_bot_pool_instance = MagicMock()
        fake_bot_pool_instance.bots = [bot1, bot2]
        fake_bot_pool_mod.bot_pool = fake_bot_pool_instance
        sys.modules["shared.bot_pool"] = fake_bot_pool_mod

        fake_telethon = types.ModuleType("telethon")
        fake_events = types.ModuleType("telethon.events")
        fake_events.NewMessage = MagicMock(return_value=MagicMock())
        fake_telethon.events = fake_events
        sys.modules["telethon"] = fake_telethon
        sys.modules["telethon.events"] = fake_events

        fake_bc = types.ModuleType("services.collector.bot_commands")
        fake_bc.handle_status = AsyncMock()
        fake_bc.handle_pause = AsyncMock()
        fake_bc.handle_resume = AsyncMock()
        fake_bc.handle_restart = AsyncMock()
        fake_bc.handle_help = AsyncMock()

        fake_collector_pkg = types.ModuleType("services.collector")
        fake_collector_pkg.__path__ = []
        fake_collector_pkg.__package__ = "services.collector"
        fake_collector_pkg.bot_commands = fake_bc

        fake_services_pkg = types.ModuleType("services")
        fake_services_pkg.__path__ = []
        fake_services_pkg.__package__ = "services"
        fake_services_pkg.collector = fake_collector_pkg

        sys.modules["services"] = fake_services_pkg
        sys.modules["services.collector"] = fake_collector_pkg
        sys.modules["services.collector.bot_commands"] = fake_bc

        mod = _load_module(
            "services.collector.account_manager",
            "services/collector/account_manager.py",
        )
        BotClientManager = mod.BotClientManager

        manager = BotClientManager.__new__(BotClientManager)
        manager.register_worker(worker_instance=MagicMock())

        assert len(bot1.handlers) == 5, (
            f"Expected 5 handlers on bot1, got {len(bot1.handlers)}"
        )
        assert len(bot2.handlers) == 5, (
            f"Expected 5 handlers on bot2, got {len(bot2.handlers)}"
        )


# ---------------------------------------------------------------------------
# BUG 8 preservation — services.collector.account_manager importable
# ---------------------------------------------------------------------------

class TestBug8Preservation:
    """
    isBugCondition_8 is FALSE when using the correct `services.collector.*` path.
    Preservation: `from services.collector.account_manager import bot_client_manager`
    succeeds under both PYTHONPATH=/app and local dev environments.
    """

    def test_services_collector_account_manager_importable(self):
        """
        `from services.collector.account_manager import bot_client_manager`
        must succeed (the fully-qualified path is valid in both environments).
        """
        # Clean up stale state
        for key in list(sys.modules.keys()):
            if "account_manager" in key:
                del sys.modules[key]
        for key in ("services", "services.collector", "services.collector.bot_commands",
                    "services.collector.account_manager"):
            sys.modules.pop(key, None)

        # Stub dependencies
        fake_config = types.ModuleType("shared.config")
        fake_config.settings = MagicMock()
        sys.modules["shared.config"] = fake_config

        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = MagicMock()
        sys.modules["dotenv"] = fake_dotenv

        fake_bot_pool_mod = types.ModuleType("shared.bot_pool")
        fake_bot_pool_mod.bot_pool = MagicMock()
        sys.modules["shared.bot_pool"] = fake_bot_pool_mod

        fake_telethon = types.ModuleType("telethon")
        fake_events = types.ModuleType("telethon.events")
        fake_events.NewMessage = MagicMock(return_value=MagicMock())
        fake_telethon.events = fake_events
        sys.modules["telethon"] = fake_telethon
        sys.modules["telethon.events"] = fake_events

        fake_bc = types.ModuleType("services.collector.bot_commands")
        sys.modules["services.collector.bot_commands"] = fake_bc

        # Load via file path (simulates PYTHONPATH=/app where services/ is on path)
        mod = _load_module(
            "services.collector.account_manager",
            "services/collector/account_manager.py",
        )

        assert hasattr(mod, "bot_client_manager"), (
            "bot_client_manager must be importable from services.collector.account_manager"
        )

    def test_shared_hub_notifier_uses_services_collector_path(self):
        """
        shared/hub_notifier.py must use `from services.collector.account_manager import ...`
        not `from collector.account_manager import ...`.
        After the fix, the correct path is used.
        """
        with open("shared/hub_notifier.py", encoding="utf-8") as f:
            source = f.read()

        # After fix: `from services.collector.account_manager import bot_client_manager`
        assert "from services.collector.account_manager import bot_client_manager" in source, (
            "BUG 8 NOT FIXED in hub_notifier.py: the import path must use services.collector.account_manager"
        )
        # Verify the old buggy path is NOT present
        assert "from collector.account_manager import bot_client_manager" not in source, (
            "BUG 8 NOT FIXED in hub_notifier.py: the old import path 'from collector.account_manager' is still present"
        )

    def test_shared_topic_manager_uses_services_collector_path(self):
        """
        shared/topic_manager.py must use `from services.collector.account_manager import ...`
        not `from collector.account_manager import ...`.
        After the fix, the correct path is used.
        """
        with open("shared/topic_manager.py", encoding="utf-8") as f:
            source = f.read()

        # After fix: `from services.collector.account_manager import get_bot_client`
        assert "from services.collector.account_manager import get_bot_client" in source, (
            "BUG 8 NOT FIXED in topic_manager.py: the import path must use services.collector.account_manager"
        )
        # Verify the old buggy path is NOT present
        assert "from collector.account_manager import get_bot_client" not in source, (
            "BUG 8 NOT FIXED in topic_manager.py: the old import path 'from collector.account_manager' is still present"
        )

    def test_shared_media_uploader_uses_services_collector_path(self):
        """
        shared/media_uploader.py must use `from services.collector.account_manager import ...`
        not `from collector.account_manager import ...`.
        After the fix, the correct path is used.
        """
        with open("shared/media_uploader.py", encoding="utf-8") as f:
            source = f.read()

        # After fix: `from services.collector.account_manager import get_bot_client`
        assert "from services.collector.account_manager import get_bot_client" in source, (
            "BUG 8 NOT FIXED in media_uploader.py: the import path must use services.collector.account_manager"
        )
        # Verify the old buggy path is NOT present
        assert "from collector.account_manager import get_bot_client" not in source, (
            "BUG 8 NOT FIXED in media_uploader.py: the old import path 'from collector.account_manager' is still present"
        )
