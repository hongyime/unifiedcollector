"""
Property-based tests for process_operation_with_smart_routing().

Property 18: Batch Processing Statistics Completeness
  - sum(success_count + failed_count) == total

Validates: Requirements 7.7
"""

import string
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, strategies as st, assume

from src.operation_router import process_operation_with_smart_routing
from src.operation_classifier import OperationType
from src.conservative_rate_limiter import ConservativeRateLimiter
from src.smart_account_selector import SmartAccountSelector


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

valid_username_strategy = st.text(
    alphabet=string.ascii_letters + string.digits + "._",
    min_size=1,
    max_size=20,
).filter(lambda x: bool(x))

account_name_strategy = st.text(
    alphabet=string.ascii_letters + string.digits + "_",
    min_size=1,
    max_size=15,
).filter(lambda x: bool(x))


def _make_no_sleep_limiter():
    """ConservativeRateLimiter that never actually sleeps."""
    mock_cooldown = MagicMock()
    mock_cooldown.is_on_cooldown.return_value = False
    mock_cooldown.get_cooldown_remaining.return_value = 0.0
    mock_cooldown.get_available_accounts.side_effect = lambda names: list(names)
    limiter = ConservativeRateLimiter(
        min_delay=0.0, max_delay=0.0, cooldown_manager=mock_cooldown
    )
    with patch.object(limiter, "_sleep"):
        return limiter


# ---------------------------------------------------------------------------
# Property 18: Batch Processing Statistics Completeness
# ---------------------------------------------------------------------------

class TestBatchProcessingStatisticsCompleteness:
    """Property 18: success_count + failed_count == total for any batch."""

    @given(
        st.lists(valid_username_strategy, min_size=1, max_size=20),
        st.lists(account_name_strategy, min_size=1, max_size=3),
    )
    @settings(max_examples=50, deadline=None)
    def test_statistics_sum_equals_total_all_success(self, usernames, accounts):
        """
        **Property 18: Batch Processing Statistics Completeness (all success)**

        When all operations succeed, success_count + failed_count == total.

        Validates: Requirements 7.7
        """
        unique_usernames = list(dict.fromkeys(usernames))
        unique_accounts = list(dict.fromkeys(accounts))
        assume(len(unique_usernames) > 0 and len(unique_accounts) > 0)

        mock_db = MagicMock()
        mock_db.save.return_value = True
        mock_db.get_username_record.return_value = None

        mock_selector = MagicMock(spec=SmartAccountSelector)
        mock_selector.select_for_batch.return_value = {
            unique_accounts[0]: unique_usernames
        }

        limiter = _make_no_sleep_limiter()
        with patch.object(limiter, "_sleep"):
            result = process_operation_with_smart_routing(
                operation_name="download_profile_pic",
                target_usernames=unique_usernames,
                execute_fn=lambda account, username: True,  # always succeeds
                username_db=mock_db,
                rate_limiter=limiter,
                account_selector=mock_selector,
                available_accounts=unique_accounts,
            )

        assert result["success_count"] + result["failed_count"] == result["total"]
        assert result["total"] == len(unique_usernames)

    @given(
        st.lists(valid_username_strategy, min_size=1, max_size=20),
        st.lists(account_name_strategy, min_size=1, max_size=3),
    )
    @settings(max_examples=50, deadline=None)
    def test_statistics_sum_equals_total_all_failure(self, usernames, accounts):
        """
        **Property 18: Batch Processing Statistics Completeness (all failure)**

        When all operations fail, success_count + failed_count == total.

        Validates: Requirements 7.7
        """
        unique_usernames = list(dict.fromkeys(usernames))
        unique_accounts = list(dict.fromkeys(accounts))
        assume(len(unique_usernames) > 0 and len(unique_accounts) > 0)

        mock_db = MagicMock()
        mock_db.save.return_value = True
        mock_db.get_username_record.return_value = None

        mock_selector = MagicMock(spec=SmartAccountSelector)
        mock_selector.select_for_batch.return_value = {
            unique_accounts[0]: unique_usernames
        }

        limiter = _make_no_sleep_limiter()
        with patch.object(limiter, "_sleep"):
            result = process_operation_with_smart_routing(
                operation_name="download_profile_pic",
                target_usernames=unique_usernames,
                execute_fn=lambda account, username: False,  # always fails
                username_db=mock_db,
                rate_limiter=limiter,
                account_selector=mock_selector,
                available_accounts=unique_accounts,
            )

        assert result["success_count"] + result["failed_count"] == result["total"]
        assert result["total"] == len(unique_usernames)

    @given(
        st.lists(valid_username_strategy, min_size=2, max_size=20),
        st.lists(account_name_strategy, min_size=1, max_size=3),
        st.integers(min_value=0, max_value=100),
    )
    @settings(max_examples=50, deadline=None)
    def test_statistics_sum_equals_total_mixed_results(self, usernames, accounts, seed):
        """
        **Property 18: Batch Processing Statistics Completeness (mixed)**

        For any mix of successes and failures, success_count + failed_count == total.

        Validates: Requirements 7.7
        """
        import random as rnd
        rnd.seed(seed)

        unique_usernames = list(dict.fromkeys(usernames))
        unique_accounts = list(dict.fromkeys(accounts))
        assume(len(unique_usernames) >= 2 and len(unique_accounts) > 0)

        mock_db = MagicMock()
        mock_db.save.return_value = True
        mock_db.get_username_record.return_value = None

        mock_selector = MagicMock(spec=SmartAccountSelector)
        mock_selector.select_for_batch.return_value = {
            unique_accounts[0]: unique_usernames
        }

        # Randomly succeed or fail
        outcomes = {u: rnd.choice([True, False]) for u in unique_usernames}

        limiter = _make_no_sleep_limiter()
        with patch.object(limiter, "_sleep"):
            result = process_operation_with_smart_routing(
                operation_name="download_profile_pic",
                target_usernames=unique_usernames,
                execute_fn=lambda account, username: outcomes.get(username, False),
                username_db=mock_db,
                rate_limiter=limiter,
                account_selector=mock_selector,
                available_accounts=unique_accounts,
            )

        assert result["success_count"] + result["failed_count"] == result["total"]
        assert result["total"] == len(unique_usernames)

    @given(st.lists(valid_username_strategy, min_size=1, max_size=10))
    @settings(max_examples=30, deadline=None)
    def test_empty_accounts_marks_all_failed(self, usernames):
        """
        When no accounts are available, all usernames are marked failed.

        Validates: Requirements 7.7, 8.1
        """
        unique_usernames = list(dict.fromkeys(usernames))
        assume(len(unique_usernames) > 0)

        mock_db = MagicMock()
        mock_db.save.return_value = True

        result = process_operation_with_smart_routing(
            operation_name="download_profile_pic",
            target_usernames=unique_usernames,
            execute_fn=lambda account, username: True,
            username_db=mock_db,
            rate_limiter=MagicMock(
                check_account_available=MagicMock(return_value=False),
                get_cooldown_remaining=MagicMock(return_value=0.0),
                get_available_accounts=MagicMock(return_value=[]),
            ),
            available_accounts=[],
        )

        assert result["total"] == len(unique_usernames)
        assert result["failed_count"] == len(unique_usernames)
        assert result["success_count"] == 0
        assert result["success_count"] + result["failed_count"] == result["total"]
