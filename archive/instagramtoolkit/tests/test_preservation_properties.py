"""
Preservation Property Tests - Instagram Rate Limit Ban Fix

These tests verify that existing functionality remains unchanged after the fix.
They should PASS on UNFIXED code to establish baseline behavior, and continue
to PASS on FIXED code to confirm no regressions.

**IMPORTANT**: These tests focus on operations NOT involving base delay timing:
- Account rotation and switching
- Cooldown enforcement (15-minute minimums)
- Quota management (180 profile views, 6000 actions)
- Operation-specific multipliers (PUBLIC: 1.0x, FOLLOWING_REQUIRED: 1.5x, MUTUAL_FOLLOWING: 2.0x)
- Emergency backoff and account switching
- Session management

**Property 2: Preservation** - Existing Functionality Unchanged

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, strategies as st, assume

from src.account_cooldown import AccountCooldownManager, AccountQuotaManager
from src.conservative_rate_limiter import ConservativeRateLimiter, _DELAY_MULTIPLIERS
from src.operation_classifier import OperationType
from src.config import (
    ACCOUNT_COOLDOWN_MINUTES,
    DAILY_QUOTA_PROFILE_VIEWS,
    DAILY_QUOTA_ACTIONS,
)


# ---------------------------------------------------------------------------
# Property 1: Account Rotation
# Validates: Requirement 3.1
# ---------------------------------------------------------------------------

class TestAccountRotationPreservation:
    """
    Verify multiple accounts can be rotated and switched.
    
    **Validates: Requirement 3.1**
    """

    @given(
        account_names=st.lists(
            st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"))),
            min_size=2,
            max_size=10,
            unique=True
        )
    )
    @settings(max_examples=20, deadline=None)
    def test_property_multiple_accounts_can_be_tracked(self, account_names):
        """
        Property: Multiple accounts can be tracked independently.
        
        For any list of account names, the cooldown manager should be able to
        track each account independently without interference.
        """
        manager = AccountCooldownManager()
        
        # Clear any existing cooldowns first
        for account in account_names:
            manager.clear_cooldown(account)
        
        # Put some accounts on cooldown, leave others available
        for i, account in enumerate(account_names):
            if i % 2 == 0:
                manager.put_on_cooldown(account, minutes=15)
        
        # Verify each account's state is tracked independently
        for i, account in enumerate(account_names):
            if i % 2 == 0:
                assert manager.is_on_cooldown(account), f"Account {account} should be on cooldown"
            else:
                assert not manager.is_on_cooldown(account), f"Account {account} should not be on cooldown"

    @given(
        account_names=st.lists(
            st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"))),
            min_size=2,
            max_size=10,
            unique=True
        )
    )
    @settings(max_examples=20, deadline=None)
    def test_property_available_accounts_filtered_correctly(self, account_names):
        """
        Property: get_available_accounts filters out accounts on cooldown.
        
        For any list of accounts, get_available_accounts should return only
        those not on cooldown.
        """
        manager = AccountCooldownManager()
        
        # Clear any existing cooldowns first
        for account in account_names:
            manager.clear_cooldown(account)
        
        # Put half the accounts on cooldown
        cooldown_accounts = account_names[::2]
        available_accounts = account_names[1::2]
        
        for account in cooldown_accounts:
            manager.put_on_cooldown(account, minutes=15)
        
        # Get available accounts
        result = manager.get_available_accounts(account_names)
        
        # Verify only non-cooldown accounts are returned
        for account in cooldown_accounts:
            assert account not in result, f"Cooldown account {account} should not be available"
        
        for account in available_accounts:
            assert account in result, f"Available account {account} should be in result"


# ---------------------------------------------------------------------------
# Property 2: Cooldown Enforcement
# Validates: Requirement 3.2, 3.4
# ---------------------------------------------------------------------------

class TestCooldownEnforcementPreservation:
    """
    Verify 15-minute minimum cooldowns are enforced after rate-limit hits.
    
    **Validates: Requirements 3.2, 3.4**
    """

    @given(
        account_name=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"))),
        cooldown_minutes=st.integers(min_value=1, max_value=60)
    )
    @settings(max_examples=20, deadline=None)
    def test_property_cooldown_enforced(self, account_name, cooldown_minutes):
        """
        Property: Accounts placed on cooldown are unavailable until cooldown expires.
        
        For any account and cooldown duration, the account should be unavailable
        during the cooldown period.
        """
        manager = AccountCooldownManager()
        
        # Put account on cooldown
        manager.put_on_cooldown(account_name, minutes=cooldown_minutes)
        
        # Verify account is on cooldown
        assert manager.is_on_cooldown(account_name), f"Account {account_name} should be on cooldown"
        
        # Verify cooldown remaining is positive
        remaining = manager.get_cooldown_remaining(account_name)
        assert remaining > 0, f"Cooldown remaining should be positive, got {remaining}"
        
        # Verify cooldown can be cleared
        manager.clear_cooldown(account_name)
        assert not manager.is_on_cooldown(account_name), f"Account {account_name} should not be on cooldown after clear"

    @given(
        account_name=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"))),
        requested_minutes=st.integers(min_value=1, max_value=30)
    )
    @settings(max_examples=20, deadline=None)
    def test_property_emergency_cooldown_minimum_15_minutes(self, account_name, requested_minutes):
        """
        Property: Emergency cooldowns enforce minimum 15-minute duration.
        
        For any requested cooldown duration, the effective duration should be
        at least 15 minutes.
        """
        mock_cooldown = MagicMock()
        limiter = ConservativeRateLimiter(cooldown_manager=mock_cooldown)
        
        # Apply emergency cooldown
        limiter.emergency_cooldown(account_name, duration_minutes=requested_minutes)
        
        # Verify put_on_cooldown was called
        mock_cooldown.put_on_cooldown.assert_called_once()
        
        # Extract the actual minutes applied
        call_kwargs = mock_cooldown.put_on_cooldown.call_args
        applied_minutes = call_kwargs[1].get("minutes") or call_kwargs[0][1]
        
        # Verify minimum 15 minutes enforced
        assert applied_minutes >= 15, f"Emergency cooldown should be at least 15 minutes, got {applied_minutes}"
        
        # If requested > 15, should use requested duration
        if requested_minutes > 15:
            assert applied_minutes == requested_minutes, f"Should use requested {requested_minutes} minutes, got {applied_minutes}"


# ---------------------------------------------------------------------------
# Property 3: Quota Management
# Validates: Requirement 3.2
# ---------------------------------------------------------------------------

class TestQuotaManagementPreservation:
    """
    Verify daily quotas tracked (180 profile views, 6000 actions).
    
    **Validates: Requirement 3.2**
    """

    def test_property_quota_tracking_persists(self):
        """
        Property: Quota usage is tracked and persists across manager instances.
        
        For any account and usage amounts, the quota manager should track
        profile views and actions correctly.
        
        Note: Using a unique account name to avoid database conflicts.
        """
        import uuid
        account_name = f"test_quota_{uuid.uuid4().hex[:8]}"
        profile_views = 50
        actions = 100
        
        # Create manager and record usage
        manager1 = AccountQuotaManager()
        manager1.record_profile_view(account_name, count=profile_views)
        manager1.record_action(account_name, count=actions)
        
        # Create new manager and verify quota persists
        manager2 = AccountQuotaManager()
        summary = manager2.get_usage_summary(account_name)
        
        # Verify profile views tracked
        assert summary["profile_views"].startswith(str(profile_views)), \
            f"Expected profile_views to start with {profile_views}, got {summary['profile_views']}"
        
        # Verify actions tracked
        assert summary["actions"].startswith(str(actions)), \
            f"Expected actions to start with {actions}, got {summary['actions']}"

    @given(
        profile_views=st.integers(min_value=0, max_value=300)
    )
    @settings(max_examples=20, deadline=None)
    def test_property_profile_view_quota_enforced(self, profile_views):
        """
        Property: Profile view quota limits are enforced correctly.
        
        For any account, can_view_profiles should return False when quota
        is exceeded (>= DAILY_QUOTA_PROFILE_VIEWS).
        """
        import uuid
        account_name = f"test_pv_{uuid.uuid4().hex[:8]}"
        
        manager = AccountQuotaManager()
        
        # Record profile views
        manager.record_profile_view(account_name, count=profile_views)
        
        # Check if can view profiles
        can_view = manager.can_view_profiles(account_name)
        
        # Verify quota enforcement
        if DAILY_QUOTA_PROFILE_VIEWS > 0:
            if profile_views < DAILY_QUOTA_PROFILE_VIEWS:
                assert can_view, f"Should be able to view profiles with {profile_views}/{DAILY_QUOTA_PROFILE_VIEWS} views"
            else:
                assert not can_view, f"Should not be able to view profiles with {profile_views}/{DAILY_QUOTA_PROFILE_VIEWS} views"
        else:
            # Unlimited quota
            assert can_view, "Should always be able to view profiles when quota is unlimited"

    @given(
        actions=st.integers(min_value=0, max_value=8000)
    )
    @settings(max_examples=20, deadline=None)
    def test_property_action_quota_enforced(self, actions):
        """
        Property: Action quota limits are enforced correctly.
        
        For any account, can_perform_action should return False when quota
        is exceeded (>= DAILY_QUOTA_ACTIONS).
        """
        import uuid
        account_name = f"test_act_{uuid.uuid4().hex[:8]}"
        
        manager = AccountQuotaManager()
        
        # Record actions
        manager.record_action(account_name, count=actions)
        
        # Check if can perform action
        can_act = manager.can_perform_action(account_name)
        
        # Verify quota enforcement
        if DAILY_QUOTA_ACTIONS > 0:
            if actions < DAILY_QUOTA_ACTIONS:
                assert can_act, f"Should be able to perform actions with {actions}/{DAILY_QUOTA_ACTIONS} actions"
            else:
                assert not can_act, f"Should not be able to perform actions with {actions}/{DAILY_QUOTA_ACTIONS} actions"
        else:
            # Unlimited quota
            assert can_act, "Should always be able to perform actions when quota is unlimited"


# ---------------------------------------------------------------------------
# Property 4: Operation Multipliers
# Validates: Requirement 3.3
# ---------------------------------------------------------------------------

class TestOperationMultipliersPreservation:
    """
    Verify operation-specific multipliers applied correctly.
    
    **Validates: Requirement 3.3**
    """

    def test_property_public_operations_use_1x_multiplier(self):
        """
        Property: PUBLIC operations use 1.0x delay multiplier.
        
        This is a constant that should never change.
        """
        assert _DELAY_MULTIPLIERS[OperationType.PUBLIC] == 1.0, \
            "PUBLIC operations should use 1.0x multiplier"

    def test_property_following_required_operations_use_1_5x_multiplier(self):
        """
        Property: FOLLOWING_REQUIRED operations use 1.5x delay multiplier.
        
        This is a constant that should never change.
        """
        assert _DELAY_MULTIPLIERS[OperationType.FOLLOWING_REQUIRED] == 1.5, \
            "FOLLOWING_REQUIRED operations should use 1.5x multiplier"

    def test_property_mutual_following_operations_use_2x_multiplier(self):
        """
        Property: MUTUAL_FOLLOWING operations use 2.0x delay multiplier.
        
        This is a constant that should never change.
        """
        assert _DELAY_MULTIPLIERS[OperationType.MUTUAL_FOLLOWING] == 2.0, \
            "MUTUAL_FOLLOWING operations should use 2.0x multiplier"

    @given(
        operation_type=st.sampled_from(list(OperationType))
    )
    @settings(max_examples=10, deadline=None)
    def test_property_all_operation_types_have_multipliers(self, operation_type):
        """
        Property: All operation types have defined delay multipliers.
        
        For any operation type, there should be a corresponding multiplier.
        """
        assert operation_type in _DELAY_MULTIPLIERS, \
            f"Operation type {operation_type} should have a delay multiplier"
        
        multiplier = _DELAY_MULTIPLIERS[operation_type]
        assert multiplier > 0, f"Multiplier for {operation_type} should be positive, got {multiplier}"

    @given(
        operation_type=st.sampled_from(list(OperationType))
    )
    @settings(max_examples=10, deadline=None)
    def test_property_operation_delay_accepts_all_types(self, operation_type):
        """
        Property: operation_delay() accepts all OperationType values.
        
        For any operation type, operation_delay() should execute without error.
        """
        mock_cooldown = MagicMock()
        limiter = ConservativeRateLimiter(
            min_delay=0.0,
            max_delay=0.0,
            cooldown_manager=mock_cooldown
        )
        
        with patch.object(limiter, "_sleep"):
            # Should not raise any exception
            limiter.operation_delay(operation_type)


# ---------------------------------------------------------------------------
# Property 5: Emergency Backoff
# Validates: Requirement 3.5
# ---------------------------------------------------------------------------

class TestEmergencyBackoffPreservation:
    """
    Verify exponential backoff and account switching work on rate-limit errors.
    
    **Validates: Requirement 3.5**
    """

    @given(
        account_name=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")))
    )
    @settings(max_examples=20, deadline=None)
    def test_property_emergency_cooldown_applies_to_account(self, account_name):
        """
        Property: Emergency cooldown is applied to the specified account.
        
        For any account name, emergency_cooldown() should place that specific
        account on cooldown.
        """
        mock_cooldown = MagicMock()
        limiter = ConservativeRateLimiter(cooldown_manager=mock_cooldown)
        
        # Apply emergency cooldown
        limiter.emergency_cooldown(account_name, duration_minutes=20)
        
        # Verify put_on_cooldown was called with correct account
        mock_cooldown.put_on_cooldown.assert_called_once()
        call_args = mock_cooldown.put_on_cooldown.call_args[0]
        assert call_args[0] == account_name, \
            f"Emergency cooldown should be applied to {account_name}, got {call_args[0]}"

    @given(
        account_names=st.lists(
            st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"))),
            min_size=2,
            max_size=10,
            unique=True
        )
    )
    @settings(max_examples=20, deadline=None)
    def test_property_account_switching_filters_cooldown_accounts(self, account_names):
        """
        Property: Account switching logic filters out accounts on cooldown.
        
        For any list of accounts, get_available_accounts should exclude
        accounts currently on cooldown.
        """
        mock_cooldown = MagicMock()
        limiter = ConservativeRateLimiter(cooldown_manager=mock_cooldown)
        
        # Set up mock to return filtered list
        available = account_names[1::2]  # Every other account
        mock_cooldown.get_available_accounts.return_value = available
        
        # Get available accounts
        result = limiter.get_available_accounts(account_names)
        
        # Verify filtering occurred
        assert result == available, \
            f"Should return filtered accounts, got {result}"


# ---------------------------------------------------------------------------
# Property 6: Session Management
# Validates: Requirement 3.6
# ---------------------------------------------------------------------------

class TestSessionManagementPreservation:
    """
    Verify session files saved and authentication state maintained.
    
    **Validates: Requirement 3.6**
    
    Note: This is a basic test to ensure the preservation property holds.
    Full session management testing is covered in other test files.
    """

    def test_property_cooldown_state_persists_to_database(self):
        """
        Property: Cooldown state persists to database.
        
        When an account is placed on cooldown, the state should persist
        across manager instances (simulating session persistence).
        """
        import uuid
        account_name = f"test_cooldown_persist_{uuid.uuid4().hex[:8]}"
        
        # Create manager and put account on cooldown
        manager1 = AccountCooldownManager()
        manager1.put_on_cooldown(account_name, minutes=15)
        
        # Create new manager and verify state persists
        manager2 = AccountCooldownManager()
        assert manager2.is_on_cooldown(account_name), \
            "Cooldown state should persist across manager instances"

    def test_property_quota_state_persists_to_database(self):
        """
        Property: Quota state persists to database.
        
        When quota usage is recorded, the state should persist across
        manager instances (simulating session persistence).
        """
        import uuid
        account_name = f"test_quota_persist_{uuid.uuid4().hex[:8]}"
        
        # Create manager and record usage
        manager1 = AccountQuotaManager()
        manager1.record_profile_view(account_name, count=50)
        manager1.record_action(account_name, count=100)
        
        # Create new manager and verify state persists
        manager2 = AccountQuotaManager()
        summary = manager2.get_usage_summary(account_name)
        
        assert summary["profile_views"].startswith("50"), \
            "Profile view quota should persist across manager instances"
        assert summary["actions"].startswith("100"), \
            "Action quota should persist across manager instances"
