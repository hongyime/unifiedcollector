#!/usr/bin/env python3
"""
Mock account fixtures for testing.
Provides simulated account data to avoid real Telegram API calls.
"""
from typing import Dict, List, Any


def generate_mock_accounts(count: int = 3) -> List[Dict[str, Any]]:
    """
    Generate mock account data for testing.
    
    Args:
        count: Number of accounts to generate
        
    Returns:
        List of mock account dictionaries
    """
    accounts = []
    for i in range(1, count + 1):
        account = {
            'name': f'test_account_{i}',
            'phone': f'+123456789{i:03d}',
            'api_id': 10000 + i,
            'api_hash': f'test_api_hash_{i}' * 8,
        }
        accounts.append(account)
    return accounts


# Default set of mock accounts
MOCK_ACCOUNTS = generate_mock_accounts(count=3)


def get_mock_account_names() -> List[str]:
    """Get list of mock account names"""
    return [acc['name'] for acc in MOCK_ACCOUNTS]


def get_mock_accounts_dict() -> Dict[str, Dict[str, Any]]:
    """Get mock accounts as dictionary keyed by name"""
    return {acc['name']: acc for acc in MOCK_ACCOUNTS}


def get_single_mock_account() -> Dict[str, Any]:
    """Get a single mock account for single-account tests"""
    return MOCK_ACCOUNTS[0]


if __name__ == "__main__":
    # Test the fixtures
    print("Mock Accounts:")
    for i, account in enumerate(generate_mock_accounts(), 1):
        print(f"\n{i}. {account['name']}")
        print(f"   Phone: {account['phone']}")
        print(f"   API ID: {account['api_id']}")
