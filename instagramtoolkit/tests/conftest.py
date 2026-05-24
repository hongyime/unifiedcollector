"""Shared test fixtures for the unified_instagram_toolkit test suite.

Offline (unit) tests run with no Instagram API calls.
Integration tests (marked ``@pytest.mark.integration``) hit the real API
using sessions stored in the project ``sessions/`` directory.  They require
network access and a valid session for the configured account.

Run offline tests only:   pytest tests/
Run integration tests:    pytest tests/ -m integration
Run everything:           pytest tests/ --run-integration
"""
import os
import sys
import json
import pytest

# ── Path setup ──────────────────────────────────────────────────────
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# ── Integration-test gate ───────────────────────────────────────────

def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests that call the Instagram API",
    )


def pytest_collection_modifyitems(config, items):
    """Skip tests marked ``integration`` unless --run-integration is passed."""
    if config.getoption("--run-integration"):
        return
    skip_int = pytest.mark.skip(reason="need --run-integration option to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_int)


# ── Sample account data ────────────────────────────────────────────

SAMPLE_ACCOUNTS = [
    {"name": "acct1", "username": "user_one", "password": "pw1"},
    {"name": "acct2", "username": "user_two", "password": "pw2"},
    {"name": "acct3", "username": "user_three", "password": "pw3"},
]


@pytest.fixture
def sample_accounts():
    return list(SAMPLE_ACCOUNTS)


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Create a temporary data directory and return its path."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return str(data_dir)


@pytest.fixture
def tmp_sessions_dir(tmp_path):
    """Create a temporary sessions directory and return its path."""
    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir()
    return str(sess_dir)


@pytest.fixture
def sample_relationships():
    """Return a list of sample relationship dicts for priority testing."""
    return [
        {"source": "user_one", "target": "alice", "type": "followers"},
        {"source": "user_one", "target": "bob", "type": "following"},
        {"source": "user_one", "target": "charlie", "type": "followers"},
        {"source": "user_one", "target": "charlie", "type": "following"},  # mutual
        {"source": "user_one", "target": "diana", "type": "following"},
    ]


@pytest.fixture
def sample_progress_data():
    """Return sample progress JSON data."""
    return {
        "started_at": "2025-01-01T00:00:00",
        "operation_type": "spider",
        "completed": ["alice", "bob"],
        "failed": ["charlie"],
        "pending": ["diana"],
        "current_batch": {},
        "statistics": {
            "total_processed": 3,
            "successful": 2,
            "failed": 1,
            "skipped": 0,
        },
    }


# ── Integration-test fixtures ──────────────────────────────────────

# Default public account used for safe read-only integration tests
INTEGRATION_TARGET = "therock"

# Account alias to authenticate with (must match .env INSTA_ACCOUNT_*_NAME)
INTEGRATION_ACCOUNT = "b"


@pytest.fixture(scope="session")
def integration_loader():
    """Provide an authenticated ``instaloader.Instaloader`` for integration
    tests.  Scoped to the session so we only log in once.

    Yields the (loader, account_name) tuple.
    """
    from account_manager import InstagramAccountManager

    mgr = InstagramAccountManager()
    loader = mgr.get_authenticated_loader(account_name=INTEGRATION_ACCOUNT)
    if loader is None:
        pytest.skip(f"Could not authenticate account '{INTEGRATION_ACCOUNT}'")
    yield loader, INTEGRATION_ACCOUNT
    mgr.logout()


@pytest.fixture(scope="session")
def downloads_dir():
    """Return the project ``downloads/`` directory, creating it if needed."""
    d = os.path.join(ROOT_DIR, "downloads")
    os.makedirs(d, exist_ok=True)
    return d
