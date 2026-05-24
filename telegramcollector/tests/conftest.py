import pytest
import asyncio
import sys
import os
from unittest.mock import MagicMock

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# MOCK BROKEN/HEAVY DEPENDENCIES
# We do this BEFORE importing application modules
sys.modules['av'] = MagicMock()
sys.modules['cv2'] = MagicMock()
sys.modules['insightface'] = MagicMock()
sys.modules['insightface.app'] = MagicMock()

# Mock telethon submodules to prevent import pollution across test files.
# All tests mock telethon anyway (no real Telegram connections), so this is safe.
_telethon_mock = MagicMock()
_telethon_errors_mock = MagicMock()

# Make telethon error classes real exceptions so they can be used as side_effect
class _FloodWaitError(Exception):
    def __init__(self, request=None, seconds=0):
        self.seconds = seconds
        super().__init__(f"FloodWait for {seconds}s")

class _PhoneCodeInvalidError(Exception):
    def __init__(self, request=None):
        super().__init__("Phone code invalid")

class _PhoneCodeExpiredError(Exception):
    def __init__(self, request=None):
        super().__init__("Phone code expired")

class _PasswordHashInvalidError(Exception):
    def __init__(self, request=None):
        super().__init__("Password hash invalid")

class _SessionPasswordNeededError(Exception):
    def __init__(self, request=None):
        super().__init__("Session password needed")

class _UsernameNotOccupiedError(Exception):
    def __init__(self, request=None):
        super().__init__("Username not occupied")

class _ChannelPrivateError(Exception):
    def __init__(self, request=None):
        super().__init__("Channel private")

_telethon_errors_mock.FloodWaitError = _FloodWaitError
_telethon_errors_mock.PhoneCodeInvalidError = _PhoneCodeInvalidError
_telethon_errors_mock.PhoneCodeExpiredError = _PhoneCodeExpiredError
_telethon_errors_mock.PasswordHashInvalidError = _PasswordHashInvalidError
_telethon_errors_mock.SessionPasswordNeededError = _SessionPasswordNeededError
_telethon_errors_mock.UsernameNotOccupiedError = _UsernameNotOccupiedError
_telethon_errors_mock.ChannelPrivateError = _ChannelPrivateError

for _mod in [
    'telethon',
    'telethon.tl',
    'telethon.tl.types',
    'telethon.tl.functions',
    'telethon.tl.functions.stories',
    'telethon.tl.functions.channels',
    'telethon.tl.functions.auth',
    'telethon.sessions',
    'telethon.events',
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Always override telethon.errors with our real exception classes
sys.modules['telethon.errors'] = _telethon_errors_mock

# Pre-import resilience to prevent test files from replacing it with a MagicMock
# (test_backfill_worker_props.py etc. use sys.modules.setdefault("resilience", MagicMock()))
import shared.resilience as _resilience_module  # noqa: F401, E402
sys.modules['resilience'] = _resilience_module
sys.modules['shared.resilience'] = _resilience_module

# Set required environment variables for config.py
os.environ.setdefault('TG_API_ID', '12345')
os.environ.setdefault('TG_API_HASH', 'test_hash')
os.environ.setdefault('BOT_TOKEN', '123:test_token')
os.environ.setdefault('BOT_TOKENS', 'TestBot1:111:test_token_1;TestBot2:222:test_token_2;TestBot3:333:test_token_3')
os.environ.setdefault('HUB_GROUP_ID', '-100123456789')
os.environ.setdefault('DB_PASSWORD', 'test_password')
os.environ.setdefault('REDIS_HOST', 'localhost')
os.environ.setdefault('REDIS_PORT', '6379')
# Avoid loading .env file which might confuse tests
os.environ['DOTENV_KEY'] = 'test' 

# Reload config to ensure it picks up these env vars if it was already imported
if 'shared.config' in sys.modules:
    import importlib
    import shared.config
    importlib.reload(shared.config)

@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def reset_asyncio_after_test():
    """Restore asyncio functions after each test and reset event loop state.
    
    Some tests patch login_bot.main.asyncio.create_task/gather which patches
    the asyncio module globally. This fixture saves and restores them.
    Also resets the event loop to prevent state leakage between tests.
    """
    import asyncio as _asyncio
    import gc
    _real_create_task = _asyncio.create_task
    _real_gather = _asyncio.gather
    _real_ensure_future = _asyncio.ensure_future
    yield
    _asyncio.create_task = _real_create_task
    _asyncio.gather = _real_gather
    _asyncio.ensure_future = _real_ensure_future
    # Force GC to clean up unawaited coroutines
    gc.collect()
    gc.collect()  # Run twice to catch cycles
    # Reset any closed event loop to prevent asyncio.run() from hanging
    try:
        loop = _asyncio.get_event_loop_policy().get_event_loop()
        if loop.is_closed():
            new_loop = _asyncio.new_event_loop()
            _asyncio.set_event_loop(new_loop)
    except Exception:
        pass


def pytest_runtest_setup(item):
    """Before test_no_cross_service_imports, remove forbidden modules from sys.modules
    so the test can verify login_bot doesn't import them at import time."""
    if item.name == "test_no_cross_service_imports":
        forbidden = ['collector', 'face_recognition', 'face_processor',
                     'message_scanner', 'media_downloader', 'media_uploader']
        for mod in list(sys.modules.keys()):
            for f in forbidden:
                if mod == f or mod.startswith(f + '.'):
                    # Don't delete collector.story_scanner — it's needed by story_scanner_props tests
                    # and deleting it causes get_db_connection to be re-bound incorrectly
                    if mod not in ('collector.story_scanner',):
                        del sys.modules[mod]
