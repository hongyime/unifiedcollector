#!/usr/bin/env python3
"""
Test script to verify failed username handling and username change tracking.
"""
import asyncio
import os
import sys
import tempfile
import sqlite3
from pathlib import Path

# Add the root directory to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.state_manager import StateManager


async def test_failed_lookups_username_support():
    """Test that usernames can be added and checked as failed lookups."""
    print("\n=== Testing Failed Lookups: Username Support ===")
    
    # Create a temporary database for testing
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        state = StateManager(str(db_path))
        
        # Test 1: Check that is_failed_lookup works with usernames (strings)
        print("\n1. Adding failed username lookup...")
        await state.add_failed_lookup("@ernestboey", "username_not_occupied", reference_type="username")
        
        print("2. Checking if username is in failed lookups...")
        is_failed = state.is_failed_lookup("@ernestboey", reference_type="username")
        assert is_failed, "@ernestboey should be marked as failed lookup"
        print("   ✅ Username correctly marked as failed")
        
        # Test 2: Check that auto-detection works
        print("\n3. Testing auto-detection of reference type...")
        await state.add_failed_lookup("@another_user", "channel_private")  # no reference_type
        is_failed = state.is_failed_lookup("@another_user")  # no reference_type
        assert is_failed, "@another_user should be marked as failed lookup (auto-detected)"
        print("   ✅ Auto-detection of reference type works")
        
        # Test 3: Check that user_id references still work
        print("\n4. Testing with user_id (integer) references...")
        await state.add_failed_lookup(123456789, "peer_id_invalid")  # no reference_type
        is_failed = state.is_failed_lookup(123456789)  # no reference_type
        assert is_failed, "123456789 should be marked as failed lookup (auto-detected)"
        print("   ✅ user_id references still work correctly")
        
        # Test 4: Get failed lookups report
        print("\n5. Getting failed lookups report...")
        report = state.get_failed_lookups_report(limit=10)
        print(f"   Found {len(report)} failed lookups:")
        for entry in report:
            print(f"     - {entry['reference']} ({entry['reference_type']}): {entry['error_type']}")
        assert len(report) >= 3, "Should have at least 3 failed lookups"
        print("   ✅ Report generation works")
        
        # Test 5: Get summary
        print("\n6. Getting failed lookups summary...")
        summary = state.get_failed_lookups_summary()
        print(f"   Summary: {summary}")
        assert len(summary) > 0, "Summary should not be empty"
        print("   ✅ Summary generation works")
        
        state.conn.close()
    print("\n=== All failed lookups tests passed! ===\n")


async def test_username_change_tracking():
    """Test that username changes are tracked in the user_changes table."""
    print("\n=== Testing Username Change Tracking ===")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        state = StateManager(str(db_path))
        
        # Test 1: Insert a new user with username
        print("\n1. Inserting new user with username...")
        user_data = {
            'id': 12345,
            'username': 'old_username',
            'first_name': 'Test',
            'last_name': 'User',
            'is_bot': False,
            'is_verified': False,
            'is_premium': False
        }
        await state.upsert_user(user_data)
        print("   ✅ User inserted")
        
        # Check for username_set change
        changes = state.get_user_changes(limit=5)
        assert len(changes) == 1, "Should have 1 change (username_set)"
        assert changes[0]['change_type'] == 'username_set', "Change type should be username_set"
        assert changes[0]['new_value'] == 'old_username', "New value should be old_username"
        print(f"   ✅ username_set event recorded: {changes[0]}")
        
        # Test 2: Update username
        print("\n2. Updating username...")
        user_data['username'] = 'new_username'
        await state.upsert_user(user_data)
        
        changes = state.get_user_changes(limit=5)
        username_changes = [c for c in changes if c['change_type'] == 'username_change']
        assert len(username_changes) == 1, f"Should have 1 username_change, got {len(username_changes)}"
        assert username_changes[0]['old_value'] == 'old_username', "Old username not recorded"
        assert username_changes[0]['new_value'] == 'new_username', "New username not recorded"
        print(f"   ✅ username_change event recorded: {username_changes[0]}")
        
        # Test 3: Clear username
        print("\n3. Clearing username...")
        user_data['username'] = ''
        await state.upsert_user(user_data)
        
        changes = state.get_user_changes(limit=10)
        username_cleared = [c for c in changes if c['change_type'] == 'username_cleared']
        assert len(username_cleared) == 1, f"Should have 1 username_cleared event"
        assert username_cleared[0]['old_value'] == 'new_username', "Cleared username not recorded"
        print(f"   ✅ username_cleared event recorded: {username_cleared[0]}")
        
        state.conn.close()
    print("\n=== All username change tracking tests passed! ===\n")


async def test_clear_and_retry_failed_lookups():
    """Test clearing and retrying failed lookups."""
    print("\n=== Testing Failed Lookup Management ===")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        state = StateManager(str(db_path))
        
        # Add various failed lookups
        print("\n1. Adding various failed lookups...")
        await state.add_failed_lookup("@user1", "username_not_occupied", reference_type="username")
        await state.add_failed_lookup("@user2", "flood_wait", reference_type="username")
        await state.add_failed_lookup(111, "peer_id_invalid", reference_type="user_id")
        await state.add_failed_lookup(222, "username_not_occupied", reference_type="user_id")
        print("   ✅ Added 4 failed lookups")
        
        # Test get_summary
        print("\n2. Getting summary...")
        summary = state.get_failed_lookups_summary()
        print(f"   Summary: {summary}")
        assert len(summary) == 4, f"Should have 4 (error_type, reference_type) groups, got {len(summary)}"
        print("   ✅ Summary correct")
        
        # Test clear by error_type
        print("\n3. Clearing flood_wait errors...")
        deleted = state.clear_failed_lookups(error_type="flood_wait")
        assert deleted == 1, f"Should delete 1 entry, deleted {deleted}"
        print(f"   ✅ Cleared {deleted} flood_wait entries")
        
        # Test clear by reference_type (usernames only)
        print("\n4. Clearing username-type lookups...")
        deleted = state.clear_failed_lookups(reference_type="username")
        assert deleted == 1, f"Should delete 1 entry (only @user1 left), deleted {deleted}"
        print(f"   ✅ Cleared {deleted} username entries")
        
        # Test retry (delete) by error_types
        print("\n5. Retrying peer_id_invalid errors...")
        retry_count = state.retry_failed_lookups(error_types=["peer_id_invalid"])
        assert retry_count == 1, f"Should retry 1 entry, retried {retry_count}"
        print(f"   ✅ Retried {retry_count} entries")
        
        # Test usernames-only retry
        print("\n6. Testing usernames-only retry...")
        await state.add_failed_lookup("@test1", "some_error", reference_type="username")
        await state.add_failed_lookup(333, "some_error", reference_type="user_id")
        retry_count = state.retry_failed_lookups(usernames_only=True)
        assert retry_count == 1, f"Should retry 1 username entry, retried {retry_count}"
        print(f"   ✅ Retried {retry_count} username entries")
        
        state.conn.close()
    print("\n=== All failed lookup management tests passed! ===\n")


async def main():
    """Run all tests."""
    print("\n" + "="*60)
    print("TESTING FAILED USERNAME HANDLING IMPLEMENTATION")
    print("="*60)
    
    try:
        await test_failed_lookups_username_support()
        await test_username_change_tracking()
        await test_clear_and_retry_failed_lookups()
        
        print("\n" + "="*60)
        print("✅ ALL TESTS PASSED!")
        print("="*60 + "\n")
        return 0
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}\n")
        import traceback
        traceback.print_exc()
        return 1
    except Exception as e:
        print(f"\n❌ UNEXPECTED ERROR: {e}\n")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
