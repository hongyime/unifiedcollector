"""
Property-based tests for SmartAccountSelector using Hypothesis.

Validates: Requirements 7.3, 7.5, 3.2, 3.3, 7.2, 7.3, 3.7
"""

import string
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, strategies as st, assume

from src.operation_classifier import OperationType
from src.smart_account_selector import SmartAccountSelector


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

valid_username_strategy = st.text(
    alphabet=string.ascii_letters + string.digits + "._",
    min_size=1,
    max_size=30,
).filter(lambda x: bool(x))

account_name_strategy = st.text(
    alphabet=string.ascii_letters + string.digits + "_",
    min_size=1,
    max_size=20,
).filter(lambda x: bool(x))

operation_type_strategy = st.sampled_from(list(OperationType))


# ---------------------------------------------------------------------------
# Property 7: Complete Username Coverage in Batch Assignment
# ---------------------------------------------------------------------------

class TestCompleteUsernameCoverage:
    """Property 7: Every username appears exactly once in batch assignment output."""

    @given(
        st.lists(valid_username_strategy, min_size=1, max_size=30),
        st.lists(account_name_strategy, min_size=1, max_size=5),
        operation_type_strategy,
    )
    @settings(max_examples=50, deadline=None)
    def test_every_username_appears_exactly_once(self, usernames, accounts, op_type):
        """
        **Property 7: Complete Username Coverage in Batch Assignment**

        For any batch of input usernames and available accounts, every input
        username appears in exactly one account's assignment list.

        Validates: Requirements 7.3, 7.5
        """
        # Deduplicate to avoid ambiguity
        unique_usernames = list(dict.fromkeys(usernames))
        unique_accounts = list(dict.fromkeys(accounts))
        assume(len(unique_usernames) > 0 and len(unique_accounts) > 0)

        selector = SmartAccountSelector()
        assignment = selector.select_for_batch(op_type, unique_usernames, unique_accounts)

        # Collect all assigned usernames
        all_assigned = []
        for assigned_list in assignment.values():
            all_assigned.extend(assigned_list)

        # Every username appears exactly once
        assert sorted(all_assigned) == sorted(unique_usernames)
        assert len(all_assigned) == len(unique_usernames)

    @given(
        st.lists(valid_username_strategy, min_size=1, max_size=20),
        st.lists(account_name_strategy, min_size=1, max_size=5),
        operation_type_strategy,
    )
    @settings(max_examples=50, deadline=None)
    def test_no_username_appears_in_multiple_accounts(self, usernames, accounts, op_type):
        """No username is assigned to more than one account."""
        unique_usernames = list(dict.fromkeys(usernames))
        unique_accounts = list(dict.fromkeys(accounts))
        assume(len(unique_usernames) > 0 and len(unique_accounts) > 0)

        selector = SmartAccountSelector()
        assignment = selector.select_for_batch(op_type, unique_usernames, unique_accounts)

        seen = set()
        for assigned_list in assignment.values():
            for username in assigned_list:
                assert username not in seen, f"'{username}' assigned to multiple accounts"
                seen.add(username)


# ---------------------------------------------------------------------------
# Property 8: Following Relationship Consistency
# ---------------------------------------------------------------------------

class TestFollowingRelationshipConsistency:
    """Property 8: Selected account follows target or is source account."""

    @given(
        valid_username_strategy,
        st.lists(account_name_strategy, min_size=1, max_size=5),
    )
    @settings(max_examples=50, deadline=None)
    def test_selected_account_follows_or_is_source(self, target_username, accounts):
        """
        **Property 8: Following Relationship Consistency**

        For FOLLOWING_REQUIRED operations, if an account is selected it either
        follows the target or is the source account.

        Validates: Requirements 3.2, 3.3
        """
        unique_accounts = list(dict.fromkeys(accounts))
        assume(len(unique_accounts) > 0)

        # Set up mock: first account follows the target
        following_account = unique_accounts[0]

        mock_db = MagicMock()
        mock_record = MagicMock()
        mock_record.following_status = {following_account: True}
        mock_record.source_account = following_account
        mock_db.get_username_record.return_value = mock_record

        selector = SmartAccountSelector(username_db=mock_db)
        result = selector.select_for_operation(
            OperationType.FOLLOWING_REQUIRED, target_username, unique_accounts
        )

        if result is not None:
            # The selected account must follow the target or be the source account
            follows = mock_record.following_status.get(result, False)
            is_source = result == mock_record.source_account
            assert follows or is_source, (
                f"Account '{result}' neither follows '{target_username}' nor is source account"
            )


# ---------------------------------------------------------------------------
# Property 19: Public Operation Single Account Assignment
# ---------------------------------------------------------------------------

class TestPublicOperationSingleAccount:
    """Property 19: All usernames assigned to single account for PUBLIC operations."""

    @given(
        st.lists(valid_username_strategy, min_size=1, max_size=30),
        st.lists(account_name_strategy, min_size=1, max_size=5),
    )
    @settings(max_examples=50, deadline=None)
    def test_public_operation_uses_single_account(self, usernames, accounts):
        """
        **Property 19: Public Operation Single Account Assignment**

        For PUBLIC operations, all usernames are assigned to a single account.

        Validates: Requirements 7.2, 3.1
        """
        unique_usernames = list(dict.fromkeys(usernames))
        unique_accounts = list(dict.fromkeys(accounts))
        assume(len(unique_usernames) > 0 and len(unique_accounts) > 0)

        selector = SmartAccountSelector()
        assignment = selector.select_for_batch(
            OperationType.PUBLIC, unique_usernames, unique_accounts
        )

        # All usernames should be in exactly one account's list
        assert len(assignment) == 1
        assigned_account = list(assignment.keys())[0]
        assert assigned_account in unique_accounts
        assert sorted(assignment[assigned_account]) == sorted(unique_usernames)


# ---------------------------------------------------------------------------
# Property 20: Following-Required Operation Smart Grouping
# ---------------------------------------------------------------------------

class TestFollowingRequiredSmartGrouping:
    """Property 20: Usernames grouped by following relationships for FOLLOWING_REQUIRED."""

    @given(
        st.lists(valid_username_strategy, min_size=1, max_size=20),
        st.lists(account_name_strategy, min_size=2, max_size=4),
    )
    @settings(max_examples=50, deadline=None)
    def test_following_required_groups_by_relationship(self, usernames, accounts):
        """
        **Property 20: Following-Required Operation Smart Grouping**

        For FOLLOWING_REQUIRED operations, usernames are grouped by accounts
        that follow them, and every username appears exactly once.

        Validates: Requirements 7.3, 3.2
        """
        unique_usernames = list(dict.fromkeys(usernames))
        unique_accounts = list(dict.fromkeys(accounts))
        assume(len(unique_usernames) > 0 and len(unique_accounts) >= 2)

        # Set up mock: alternate accounts follow different usernames
        mock_db = MagicMock()

        def get_record(username):
            idx = unique_usernames.index(username) if username in unique_usernames else 0
            account = unique_accounts[idx % len(unique_accounts)]
            mock_record = MagicMock()
            mock_record.following_status = {account: True}
            mock_record.source_account = account
            return mock_record

        mock_db.get_username_record.side_effect = get_record

        selector = SmartAccountSelector(username_db=mock_db)
        assignment = selector.select_for_batch(
            OperationType.FOLLOWING_REQUIRED, unique_usernames, unique_accounts
        )

        # Every username must appear exactly once
        all_assigned = []
        for assigned_list in assignment.values():
            all_assigned.extend(assigned_list)

        assert sorted(all_assigned) == sorted(unique_usernames)

        # All assigned accounts must be in available_accounts
        for account in assignment.keys():
            assert account in unique_accounts


# ---------------------------------------------------------------------------
# Property 24: Cache Update Consistency
# ---------------------------------------------------------------------------

class TestCacheUpdateConsistency:
    """Property 24: Following status from tracker updates cache."""

    @given(
        valid_username_strategy,
        account_name_strategy,
    )
    @settings(max_examples=50, deadline=None)
    def test_tracker_result_updates_cache(self, target_username, account):
        """
        **Property 24: Cache Update Consistency**

        When following status is found in ProfileAccessTracker, the
        UsernameRecord cache is updated with that information.

        Validates: Requirements 3.7
        """
        # Mock: no cache hit, but tracker says account can access
        mock_db = MagicMock()
        mock_record = MagicMock()
        mock_record.following_status = {}
        mock_record.source_account = "other_account"
        mock_db.get_username_record.return_value = mock_record

        mock_tracker = MagicMock()
        mock_tracker.get_profile_summary.return_value = {
            "accessible_by": [account],
            "status": "tracked",
        }

        selector = SmartAccountSelector(
            username_db=mock_db, profile_tracker=mock_tracker
        )
        result = selector.select_for_operation(
            OperationType.FOLLOWING_REQUIRED, target_username, [account]
        )

        # Should have selected the account
        assert result == account

        # Cache should have been updated
        mock_db.update_metadata.assert_called_once()
        call_args = mock_db.update_metadata.call_args
        assert call_args[0][0] == target_username
        updated_following_status = call_args[0][1].get("following_status", {})
        assert updated_following_status.get(account) is True
