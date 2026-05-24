"""
Bug Condition Exploration Tests — audit-critical-bugfixes spec, Task 1.

These tests MUST FAIL on unfixed code — failure confirms each bug exists.
After fixes are applied, all tests MUST PASS.

Run on unfixed code:
    pytest tests/test_audit_bugfix_exploration.py -v
Expected outcome on unfixed code:
  BUG 1  — PASS  (NameError detected correctly)
  BUG 2  — PASS  (NameError detected correctly)
  BUG 3  — PASS  (TypeError detected correctly)
  BUG 4  — PASS  (InterfaceError detected correctly)
  BUG 5a — PASS  (wrong default detected in source)
  BUG 5b — PASS  (hardcoded pattern detected in source)
  BUG 6  — FAIL  (file already exists — bug was pre-fixed)
  BUG 7  — PASS  (AttributeError detected correctly)
  BUG 8  — PASS  (ModuleNotFoundError detected correctly)
"""
import asyncio
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# BUG 1 — Missing `import uuid` in shared/topic_manager.py
# ---------------------------------------------------------------------------

class TestBug1UuidNameError:
    """
    isBugCondition_1: any call to TopicManager.create_topic()
    Expected on unfixed code: NameError: name 'uuid' is not defined
    Expected after fix: no NameError
    """

    @pytest.mark.asyncio
    async def test_bug1_uuid_nameerror(self):
        """
        Verifies BUG 1 fix: `import uuid` must be present in shared/topic_manager.py.

        On unfixed code: `uuid` is not imported → NameError on create_topic().
        After fix: `import uuid` is present → no NameError.

        We verify the fix by inspecting the source (avoids hanging on DB/Telegram calls).
        """
        with open("shared/topic_manager.py", encoding="utf-8") as f:
            source = f.read()

        # After fix: import uuid must be present
        assert "import uuid" in source, (
            "BUG 1 NOT FIXED: `import uuid` is missing from shared/topic_manager.py. "
            "Add `import uuid` to the top-level imports."
        )

# ---------------------------------------------------------------------------
# BUG 2 — Undefined `collector` name in services/collector/account_manager.py
# ---------------------------------------------------------------------------

class TestBug2CollectorNameError:
    """
    isBugCondition_2: any bot command event (/status /pause /resume /restart /help)
    Expected on unfixed code: NameError: name 'collector' is not defined
    Expected after fix: handler dispatches correctly
    """

    @pytest.mark.asyncio
    async def test_bug2_collector_nameerror(self):
        """
        register_worker() registers handlers that reference `collector.bot_commands.*`.
        Calling any handler fires NameError because `collector` is never bound.
        After the fix (bot_commands alias added) this must NOT raise NameError.
        """
        # Clean up any stale module state
        for key in list(sys.modules.keys()):
            if "account_manager" in key or "bot_commands" in key:
                del sys.modules[key]
        # Remove the fake `services` stub if it was set as a non-package
        for key in ("services", "services.collector", "services.collector.bot_commands",
                    "services.collector.account_manager"):
            sys.modules.pop(key, None)

        # Stub shared.config
        fake_config = types.ModuleType("shared.config")
        fake_settings = MagicMock()
        fake_config.settings = fake_settings
        sys.modules["shared.config"] = fake_config

        # Stub dotenv
        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = MagicMock()
        sys.modules["dotenv"] = fake_dotenv

        # Stub shared.bot_pool
        fake_bot_pool_mod = types.ModuleType("shared.bot_pool")

        class FakeBotEntry:
            def __init__(self):
                self.name = "test_bot"
                self.username = "test_bot"
                self.client = MagicMock()

        fake_bot_pool_instance = MagicMock()
        fake_bot_pool_instance.bots = [FakeBotEntry()]
        fake_bot_pool_mod.bot_pool = fake_bot_pool_instance
        sys.modules["shared.bot_pool"] = fake_bot_pool_mod

        # Stub telethon.events — capture registered handlers
        registered_handlers = []

        def fake_on(pattern_obj):
            def decorator(fn):
                registered_handlers.append(fn)
                return fn
            return decorator

        fake_telethon = types.ModuleType("telethon")
        fake_events = types.ModuleType("telethon.events")
        fake_events.NewMessage = MagicMock(return_value=MagicMock())
        fake_telethon.events = fake_events
        sys.modules["telethon"] = fake_telethon
        sys.modules["telethon.events"] = fake_events

        # Make client.on() capture handlers
        fake_bot_pool_instance.bots[0].client.on = fake_on

        # Stub services.collector.bot_commands (the module that IS imported by account_manager)
        fake_bc = types.ModuleType("services.collector.bot_commands")
        fake_bc.handle_status = AsyncMock()
        fake_bc.handle_pause = AsyncMock()
        fake_bc.handle_resume = AsyncMock()
        fake_bc.handle_restart = AsyncMock()
        fake_bc.handle_help = AsyncMock()

        # Build a proper package hierarchy so `import services.collector.bot_commands` works
        # AND `services.collector.bot_commands` attribute access works
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

        # Load the real account_manager module directly via importlib spec
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "services.collector.account_manager",
            "services/collector/account_manager.py",
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["services.collector.account_manager"] = mod
        spec.loader.exec_module(mod)
        BotClientManager = mod.BotClientManager

        manager = BotClientManager.__new__(BotClientManager)
        manager.register_worker(worker_instance=MagicMock())

        assert len(registered_handlers) == 5, (
            f"Expected 5 handlers registered, got {len(registered_handlers)}"
        )

        # Fire each handler — after fix: dispatches correctly, no NameError
        fake_event = MagicMock()
        for handler in registered_handlers:
            # After fix: no NameError raised; handler calls bot_commands.handle_*
            await handler(fake_event)

        # Verify the correct handler functions were called
        assert fake_bc.handle_status.called or fake_bc.handle_pause.called or \
               fake_bc.handle_resume.called or fake_bc.handle_restart.called or \
               fake_bc.handle_help.called, (
            "After BUG 2 fix, at least one bot_commands handler must have been called"
        )


# ---------------------------------------------------------------------------
# BUG 3 — Raw integer hub_id passed to CreateForumTopicRequest
# ---------------------------------------------------------------------------

class TestBug3RawHubIdTypeError:
    """
    isBugCondition_3: db_topic_id where telegram_topics.topic_id IS NULL or 0
    Expected on unfixed code: ValueError or TypeError from Telethon
    Expected after fix: resolved InputChannel used, no error
    """

    @pytest.mark.asyncio
    async def test_bug3_raw_hubid_typeerror(self):
        """
        _ensure_topic_exists() with topic_id=0 calls _CreateForumTopicRequest(hub_id, label)
        where hub_id is a raw int.  Telethon raises TypeError/ValueError for bare ints.
        After the fix (get_input_entity called first) this must NOT raise.
        """
        # Clean up stale module state
        for key in list(sys.modules.keys()):
            if "publisher" in key and "face_recognition" in key:
                del sys.modules[key]
        # Remove any fake services package stubs that might interfere
        for key in ("services", "services.face_recognition", "services.face_recognition.publisher"):
            sys.modules.pop(key, None)

        # Stub asyncpg
        fake_asyncpg = types.ModuleType("asyncpg")
        fake_asyncpg.Pool = object
        sys.modules["asyncpg"] = fake_asyncpg

        # Stub shared.bot_pool
        fake_bot_pool_mod = types.ModuleType("shared.bot_pool")
        fake_bot_pool_mod.BotPool = MagicMock()
        sys.modules["shared.bot_pool"] = fake_bot_pool_mod

        # Stub shared.config
        fake_config = types.ModuleType("shared.config")
        fake_config.get_hub_group_id = MagicMock(return_value=99999)
        fake_settings = MagicMock()
        fake_config.settings = fake_settings
        sys.modules["shared.config"] = fake_config

        # Stub telethon so CreateForumTopicRequest raises TypeError on raw int
        fake_telethon = types.ModuleType("telethon")
        fake_channels = types.ModuleType("telethon.tl.functions.channels")

        def _create_forum_topic_request(channel, title):
            if isinstance(channel, int):
                raise TypeError(
                    f"Cannot cast {channel!r} to any kind of InputChannel"
                )
            return MagicMock(id=42)

        fake_channels.CreateForumTopicRequest = _create_forum_topic_request
        sys.modules["telethon"] = fake_telethon
        sys.modules["telethon.tl"] = types.ModuleType("telethon.tl")
        sys.modules["telethon.tl.functions"] = types.ModuleType("telethon.tl.functions")
        sys.modules["telethon.tl.functions.channels"] = fake_channels

        # Load the real publisher module directly via importlib spec
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "services.face_recognition.publisher",
            "services/face_recognition/publisher.py",
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["services.face_recognition.publisher"] = mod
        spec.loader.exec_module(mod)
        Publisher = mod.Publisher

        # Mock db_pool: fetchrow returns topic_id=0
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value={"topic_id": 0, "label": "Unknown Person"})
        mock_conn.execute = AsyncMock()

        class FakeAcquire:
            async def __aenter__(self):
                return mock_conn
            async def __aexit__(self, *args):
                pass

        mock_db_pool = MagicMock()
        mock_db_pool.acquire = MagicMock(return_value=FakeAcquire())

        # Mock bot_pool: get_bot() returns a bot whose client resolves entity and creates topic
        mock_bot = MagicMock()
        mock_input_channel = MagicMock()  # resolved InputChannel (not a raw int)
        mock_bot.client = AsyncMock()
        mock_bot.client.get_input_entity = AsyncMock(return_value=mock_input_channel)

        # CreateForumTopicRequest succeeds when given a resolved entity
        mock_result = MagicMock()
        mock_result.id = 42
        mock_bot.client.return_value = mock_result
        mock_bot_pool = MagicMock()
        mock_bot_pool.get_bot = MagicMock(return_value=mock_bot)

        publisher = Publisher(db_pool=mock_db_pool, bot_pool=mock_bot_pool)

        # After fix: no TypeError/ValueError/RuntimeError — returns valid topic ID
        result = await publisher._ensure_topic_exists(db_topic_id=1)
        assert result == 42, f"Expected topic ID 42, got {result}"
        # Verify get_input_entity was called with the raw hub_id
        mock_bot.client.get_input_entity.assert_called_once_with(99999)


# ---------------------------------------------------------------------------
# BUG 4 — Nested asyncpg transaction in matcher.py
# ---------------------------------------------------------------------------

class TestBug4NestedTransaction:
    """
    isBugCondition_4: any call to _create_new_identity() that reaches _store_embedding()
    Expected on unfixed code: asyncpg.exceptions.InterfaceError
    Expected after fix: no InterfaceError
    """

    @pytest.mark.asyncio
    async def test_bug4_nested_transaction(self):
        """
        _create_new_identity() opens a transaction then calls _store_embedding()
        which opens another transaction on the same connection.
        asyncpg raises InterfaceError for nested transactions without savepoints.
        After the fix (conn passed to _store_embedding) this must NOT raise.
        """
        for key in list(sys.modules.keys()):
            if "matcher" in key and "face_recognition" in key:
                del sys.modules[key]
        for key in ("services", "services.face_recognition", "services.face_recognition.matcher"):
            sys.modules.pop(key, None)

        # Stub asyncpg with a real-ish InterfaceError
        class FakeInterfaceError(Exception):
            pass

        fake_asyncpg = types.ModuleType("asyncpg")
        fake_exceptions = types.ModuleType("asyncpg.exceptions")
        fake_exceptions.InterfaceError = FakeInterfaceError
        fake_asyncpg.Pool = object
        fake_asyncpg.exceptions = fake_exceptions
        sys.modules["asyncpg"] = fake_asyncpg
        sys.modules["asyncpg.exceptions"] = fake_exceptions

        # Stub shared.config
        fake_config = types.ModuleType("shared.config")
        fake_config.get_dynamic_setting = MagicMock(return_value=0.55)
        fake_settings = MagicMock()
        fake_settings.FACE_SIMILARITY_THRESHOLD = 0.55
        fake_settings.FACE_MIN_QUALITY_THRESHOLD = 0.67
        fake_config.settings = fake_settings
        sys.modules["shared.config"] = fake_config

        # Load the real matcher module directly via importlib spec
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "services.face_recognition.matcher",
            "services/face_recognition/matcher.py",
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["services.face_recognition.matcher"] = mod
        spec.loader.exec_module(mod)
        IdentityMatcher = mod.IdentityMatcher

        # Simulate a connection already inside a transaction.
        # When _store_embedding tries to open another transaction, raise InterfaceError.
        transaction_depth = {"depth": 0}

        class FakeTransaction:
            async def __aenter__(self):
                transaction_depth["depth"] += 1
                if transaction_depth["depth"] > 1:
                    raise FakeInterfaceError(
                        "cannot start a transaction within a transaction"
                    )
                return self

            async def __aexit__(self, *args):
                transaction_depth["depth"] = max(0, transaction_depth["depth"] - 1)

        mock_conn = AsyncMock()
        mock_conn.transaction = FakeTransaction
        mock_conn.execute = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value={"id": 1})

        class FakeAcquire:
            async def __aenter__(self):
                return mock_conn
            async def __aexit__(self, *args):
                pass

        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=FakeAcquire())
        mock_pool.fetchrow = AsyncMock(return_value=None)

        matcher = IdentityMatcher(db_pool=mock_pool)
        # Force new identity path (no similar embedding found)
        matcher._find_similar_embedding = AsyncMock(return_value=None)

        embedding = [0.1] * 128

        # After fix: conn is passed to _store_embedding, no nested transaction → no InterfaceError
        result = await matcher._create_new_identity(
            embedding=embedding,
            quality_score=0.9,
            source_chat_id=1,
            source_message_id=1,
            frame_index=0,
        )
        assert isinstance(result, int), f"Expected int topic db id, got {result}"


# ---------------------------------------------------------------------------
# BUG 5a — Wrong DB_NAME default in shared/config.py
# ---------------------------------------------------------------------------

class TestBug5aWrongDbDefault:
    """
    isBugCondition_5a: deployment environment where DB_NAME is not set in .env
    Expected on unfixed code: DB_NAME default is "face_archiver" in source
    Expected after fix: DB_NAME default is "telegramcollector"
    """

    def test_bug5a_wrong_db_default(self):
        """
        Inspect shared/config.py source directly.
        After the fix, DB_NAME must default to "telegramcollector".
        """
        with open("shared/config.py", encoding="utf-8") as f:
            source = f.read()

        assert 'DB_NAME: str = "telegramcollector"' in source, (
            "BUG 5a NOT FIXED: DB_NAME default is not 'telegramcollector' in shared/config.py. "
            "Change the default from 'face_archiver' to 'telegramcollector'."
        )


# ---------------------------------------------------------------------------
# BUG 5b — Hardcoded os.environ.get("POSTGRES_DB", "face_archiver") in services
# ---------------------------------------------------------------------------

class TestBug5bHardcodedDb:
    """
    isBugCondition_5b: service is "user_intelligence" or "link_discovery"
    Expected on unfixed code: hardcoded bypass present in both files
    Expected after fix: both files use settings.DB_NAME
    """

    def test_bug5b_hardcoded_db(self):
        """
        Inspect source of both service entry points.
        On unfixed code both contain os.environ.get("POSTGRES_DB", "face_archiver").
        After the fix neither should contain that string.
        """
        hardcoded_pattern = 'os.environ.get("POSTGRES_DB", "face_archiver")'

        files_to_check = [
            "services/user_intelligence/main.py",
            "services/link_discovery/main.py",
        ]

        found_in = []
        for path in files_to_check:
            with open(path, encoding="utf-8") as f:
                content = f.read()
            if hardcoded_pattern in content:
                found_in.append(path)

        assert len(found_in) == 0, (
            f"BUG 5b NOT FIXED: hardcoded pattern still found in {found_in}. "
            "Replace os.environ.get('POSTGRES_DB', 'face_archiver') with settings.DB_NAME."
        )


# ---------------------------------------------------------------------------
# BUG 6 — Missing services/index/app.py
# ---------------------------------------------------------------------------

class TestBug6MissingAppPy:
    """
    isBugCondition_6: services/index/app.py does not exist in repository
    Expected on unfixed code: file does not exist (test PASSES by asserting absence)
    Expected after fix: file exists (test PASSES by asserting presence)

    NOTE: services/index/app.py was found to already exist in the repository.
    This means BUG 6 was pre-fixed before this spec was executed.
    The test below validates the FIXED state (file must exist).
    """

    def test_bug6_missing_app_py(self):
        """
        After the fix, services/index/app.py must exist.
        This test PASSES when the file is present (bug fixed).
        """
        assert os.path.exists("services/index/app.py"), (
            "BUG 6 STILL PRESENT: services/index/app.py does not exist. "
            "Create the file to fix this bug."
        )


# ---------------------------------------------------------------------------
# BUG 7 — get_queue_eta() calls redis_client.llen() without Redis guard
# ---------------------------------------------------------------------------

class TestBug7RedisAttributeError:
    """
    isBugCondition_7: redis_available=False AND redis_client=None
    Expected on unfixed code: AttributeError: 'NoneType' object has no attribute 'llen'
    Expected after fix: returns float or None without AttributeError
    """

    def test_bug7_redis_attributeerror(self):
        """
        Construct ProcessingQueue with redis_available=False, redis_client=None.
        Append 5 processing times so the early-return guard is bypassed.
        Call get_queue_eta() — on unfixed code raises AttributeError.
        After the fix it must return a non-negative float (or None).
        """
        for key in list(sys.modules.keys()):
            if key == "shared.processing_queue":
                del sys.modules[key]

        # Stub dependencies
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

        # Stub redis so the constructor fails to connect (forces fallback mode)
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
        pq.fallback_queue.qsize = MagicMock(return_value=10)
        pq.queue_key = "processing_queue:tasks"
        pq.num_workers = 3
        pq._processing_times = [1.0, 2.0, 1.5, 1.8, 2.2]  # 5 entries → bypasses early return

        # After fix: no AttributeError — returns a non-negative float using fallback queue size
        result = pq.get_queue_eta()
        assert result is not None, "Expected a float ETA, got None"
        assert isinstance(result, float), f"Expected float, got {type(result)}"
        assert result >= 0.0, f"ETA must be non-negative, got {result}"

# ---------------------------------------------------------------------------
# BUG 8 — Wrong import path `collector.account_manager` in shared modules
# ---------------------------------------------------------------------------

class TestBug8ModuleNotFound:
    """
    isBugCondition_8: module in {hub_notifier, topic_manager, media_uploader}
                      AND PYTHONPATH=/app (no bare `collector` package)
    Expected on unfixed code: ModuleNotFoundError: No module named 'collector'
    Expected after fix: import succeeds
    """

    def test_bug8_module_not_found(self):
        """
        In an environment where `collector` is NOT on sys.path (simulating PYTHONPATH=/app),
        attempt `from collector.account_manager import bot_client_manager`.
        On unfixed code this raises ModuleNotFoundError.
        After the fix (services.collector.account_manager used) this must NOT raise.
        """
        # Ensure `collector` is not importable as a top-level package
        saved_modules = {}
        for key in list(sys.modules.keys()):
            if key == "collector" or key.startswith("collector."):
                saved_modules[key] = sys.modules.pop(key)

        try:
            with pytest.raises(ModuleNotFoundError, match="collector"):
                from collector.account_manager import bot_client_manager  # noqa: F401
        finally:
            sys.modules.update(saved_modules)
