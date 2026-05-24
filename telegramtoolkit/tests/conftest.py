#!/usr/bin/env python3
"""
Pytest configuration and shared fixtures for all tests.
"""
import os
import sys
from pathlib import Path
from typing import Dict, List, Any
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Import test fixtures
from tests.fixtures.mock_accounts import (
    MOCK_ACCOUNTS,
    generate_mock_accounts,
    get_mock_account_names,
    get_mock_accounts_dict,
    get_single_mock_account,
)
from tests.fixtures.mock_telegram_client import (
    MockTelegramClient,
    create_mock_client,
    patch_telegram_client,
    patch_connect_function,
)


# ============================================================================
# Pytest Configuration
# ============================================================================

pytest_plugins = ['pytest_asyncio']


def pytest_configure(config):
    """Configure pytest with custom markers"""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests"
    )
    config.addinivalue_line(
        "markers", "e2e: marks tests as end-to-end tests"
    )


# ============================================================================
# Shared Fixtures
# ============================================================================

@pytest.fixture
def mock_accounts() -> List[Dict[str, Any]]:
    """
    Fixture providing mock account data.
    
    Returns:
        List of mock account dictionaries
    """
    return generate_mock_accounts(count=3)


@pytest.fixture
def single_mock_account() -> Dict[str, Any]:
    """
    Fixture providing a single mock account.
    
    Returns:
        Single mock account dictionary
    """
    return get_single_mock_account()


@pytest.fixture
def mock_account_names() -> List[str]:
    """
    Fixture providing mock account names.
    
    Returns:
        List of mock account names
    """
    return get_mock_account_names()


@pytest.fixture
def mock_accounts_dict() -> Dict[str, Dict[str, Any]]:
    """
    Fixture providing mock accounts as dictionary.
    
    Returns:
        Dictionary mapping account names to account data
    """
    return get_mock_accounts_dict()


@pytest.fixture
def mock_telegram_client():
    """
    Fixture providing a mock Telegram client.
    
    Returns:
        MockTelegramClient instance
    """
    return create_mock_client()


@pytest.fixture
def temp_data_dir(tmp_path):
    """
    Fixture providing a temporary data directory for tests.
    
    Args:
        tmp_path: Pytest's built-in temp path fixture
        
    Returns:
        Path object for temporary data directory
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    
    # Create necessary subdirectories
    (data_dir / "users_analysis").mkdir()
    (data_dir / "logs").mkdir()
    
    return data_dir


@pytest.fixture
def mock_state_manager():
    """
    Fixture providing a mock state manager.
    
    Returns:
        Mock StateManager instance
    """
    state = MagicMock()
    state.export_all_to_csv = MagicMock()
    state.export_users_to_csv = MagicMock()
    state.export_memberships_to_csv = MagicMock()
    state.export_all_to_json = MagicMock()
    state.save_scan_progress = MagicMock()
    state.load_scan_progress = MagicMock(return_value={})
    state.save_link = MagicMock()
    state._flush_links = MagicMock()
    state.load_existing_links = MagicMock(return_value=set())
    return state


@pytest.fixture
def mock_progress_logger():
    """
    Fixture providing mock progress logger functions.
    
    Returns:
        Dictionary of mock progress logger functions
    """
    return {
        'log_start': MagicMock(),
        'log_step': MagicMock(),
        'log_info': MagicMock(),
        'log_success': MagicMock(),
        'log_error': MagicMock(),
        'log_warning': MagicMock(),
        'log_complete': MagicMock(),
    }


@pytest.fixture
def mock_parallel_processor():
    """
    Fixture providing a mock parallel processor.
    
    Returns:
        Mock TelegramParallelProcessor instance
    """
    from src.core.parallel_processor import TelegramParallelProcessor
    
    processor = MagicMock(spec=TelegramParallelProcessor)
    processor.max_concurrent_per_account = 3
    processor.max_total_concurrent = 10
    processor.delay_between_batches = 1.0
    processor.min_delay_per_chat = 0.5
    
    # Mock methods
    processor.run_parallel_tasks = AsyncMock()
    processor.connect = MagicMock(side_effect=lambda info, cb: create_mock_client())
    
    return processor


@pytest.fixture
def mock_orchestrator():
    """
    Fixture providing a mock message orchestrator.
    
    Returns:
        Mock MessageOrchestrator instance
    """
    orchestrator = MagicMock()
    orchestrator.run = AsyncMock()
    orchestrator.register_processor = MagicMock()
    orchestrator.processors = []
    
    # Mock get_unified_start_message_id
    orchestrator.get_unified_start_message_id = MagicMock(return_value=0)
    
    # Mock get_unified_progress_snapshot
    orchestrator.get_unified_progress_snapshot = MagicMock(return_value={
        "links": 0,
        "users": 0,
        "media": 0
    })
    
    return orchestrator


# ============================================================================
# Patches
# ============================================================================

@pytest.fixture
def patch_telegram_client_import():
    """
    Fixture to patch TelegramClient imports.
    
    Returns:
        Context manager for patching
    """
    with patch_telegram_client():
        yield


@pytest.fixture
def patch_connect():
    """
    Fixture to patch the connect function.
    
    Returns:
        Context manager for patching
    """
    with patch_connect_function():
        yield


# ============================================================================
# Test Data Fixtures
# ============================================================================

@pytest.fixture
def sample_links_file(tmp_path):
    """
    Fixture providing a sample links file.
    
    Args:
        tmp_path: Pytest's built-in temp path fixture
        
    Returns:
        Path to sample links file
    """
    links_file = tmp_path / "collected_links.txt"
    links_content = """https://t.me/test_group_1
https://t.me/test_group_2
https://t.me/test_channel_1
https://t.me/test_channel_2
"""
    links_file.write_text(links_content)
    return links_file


@pytest.fixture
def sample_users_csv(tmp_path):
    """
    Fixture providing a sample users CSV file.
    
    Args:
        tmp_path: Pytest's built-in temp path fixture
        
    Returns:
        Path to sample users CSV file
    """
    users_file = tmp_path / "Users.csv"
    users_content = """user_id,username,first_name,last_name,phone,is_bot,is_verified,is_premium
123456,user1,Test,User,+1234567890,False,False,False
123457,user2,John,Doe,+1234567891,False,True,False
123458,user3,Jane,Smith,+1234567892,False,False,True
"""
    users_file.write_text(users_content)
    return users_file


# ============================================================================
# Environment Setup
# ============================================================================

@pytest.fixture(autouse=True)
def setup_test_environment():
    """
    Fixture to set up test environment before all tests.
    Sets console encoding and configures console output.
    """
    # Set console encoding
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    if hasattr(sys.stdout, 'reconfigure') and hasattr(sys.stderr, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    
    # Configure console output
    try:
        from src.core.console import configure_console_output
        configure_console_output()
    except Exception:
        pass
    
    yield
    
    # Cleanup
    try:
        from src.core.state_manager import shutdown_state_manager
        shutdown_state_manager()
    except Exception:
        pass


if __name__ == "__main__":
    # Test fixtures
    print("Testing fixtures...")
    
    # Test mock accounts
    accounts = generate_mock_accounts(3)
    print(f"\n✅ Mock accounts: {len(accounts)} accounts")
    
    # Test mock client
    client = create_mock_client()
    print(f"✅ Mock client created: {type(client)}")
    
    # Test account names
    names = get_mock_account_names()
    print(f"✅ Account names: {names}")
    
    print("\n✅ All fixtures loaded successfully!")
