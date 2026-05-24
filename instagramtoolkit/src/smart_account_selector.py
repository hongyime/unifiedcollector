"""
Smart Account Selector - Selects optimal Instagram accounts based on operation
requirements and following relationships.

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7
"""

import logging
from typing import Optional

from src.operation_classifier import OperationType

logger = logging.getLogger(__name__)


class SmartAccountSelector:
    """
    Selects optimal Instagram accounts for operations based on following relationships.

    For PUBLIC operations, any available account is returned.
    For FOLLOWING_REQUIRED operations, the selector checks:
      1. following_status cache in UsernameRecord
      2. ProfileAccessTracker for accessible_by list
      3. Source account as fallback
      4. Returns None if no following relationship found

    Requirements: 3.1, 3.2, 3.3, 3.4, 3.6, 3.7
    """

    def __init__(self, username_db=None, profile_tracker=None):
        """
        Initialize the selector with optional dependencies.

        Args:
            username_db: UsernameDatabase instance for following_status cache
            profile_tracker: ProfileAccessTracker instance for relationship data
        """
        self._username_db = username_db
        self._profile_tracker = profile_tracker

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def select_for_operation(
        self,
        operation_type: OperationType,
        target_username: str,
        available_accounts: list[str],
    ) -> Optional[str]:
        """
        Select the optimal account for a single-username operation.

        Args:
            operation_type: Type of operation (PUBLIC, FOLLOWING_REQUIRED, etc.)
            target_username: Instagram username being targeted
            available_accounts: List of account names available for use

        Returns:
            Account name to use, or None if no suitable account found
        """
        if not available_accounts:
            return None

        # PUBLIC operations: any account works (Requirement 3.1)
        if operation_type == OperationType.PUBLIC:
            return available_accounts[0]

        # FOLLOWING_REQUIRED / MUTUAL_FOLLOWING: need following relationship
        return self._select_following_account(target_username, available_accounts)

    def select_for_batch(
        self,
        operation_type: OperationType,
        target_usernames: list[str],
        available_accounts: list[str],
    ) -> dict[str, list[str]]:
        """
        Group usernames by optimal account for batch processing.

        Postconditions:
        - Every input username appears in exactly one account's list
        - For PUBLIC: all usernames assigned to a single account
        - For FOLLOWING_REQUIRED: grouped by following relationships

        Args:
            operation_type: Type of operation
            target_usernames: List of usernames to process
            available_accounts: List of available account names

        Returns:
            Dict mapping account name -> list of usernames
        """
        if not available_accounts or not target_usernames:
            return {}

        # PUBLIC: assign all to first available account (Requirement 3.5 / 7.2)
        if operation_type == OperationType.PUBLIC:
            return {available_accounts[0]: list(target_usernames)}

        # FOLLOWING_REQUIRED: group by optimal account (Requirement 7.3)
        assignment: dict[str, list[str]] = {}

        for username in target_usernames:
            account = self._select_following_account(username, available_accounts)
            if account is None:
                # Fallback: assign to first available account so no username is lost
                account = available_accounts[0]
                logger.warning(
                    "No following relationship found for '%s'; assigning to '%s'",
                    username,
                    account,
                )
            assignment.setdefault(account, []).append(username)

        return assignment

    def get_following_overlap(
        self,
        account: str,
        target_usernames: list[str],
    ) -> dict[str, bool]:
        """
        Return a mapping of username -> is_following for the given account.

        Checks the UsernameDatabase cache first, then ProfileAccessTracker.

        Args:
            account: Account name to check following status for
            target_usernames: List of usernames to check

        Returns:
            Dict mapping username -> True/False (is_following)
        """
        result: dict[str, bool] = {}

        for username in target_usernames:
            is_following = self._check_following(account, username)
            result[username] = is_following

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _select_following_account(
        self,
        target_username: str,
        available_accounts: list[str],
    ) -> Optional[str]:
        """
        Select an account that follows target_username using the fallback chain.

        Fallback order:
          1. following_status cache in UsernameRecord
          2. ProfileAccessTracker accessible_by list
          3. Source account (likely follows if it scraped this username)
          4. None

        Requirements: 3.2, 3.3, 3.4, 3.6, 3.7
        """
        record = None
        if self._username_db is not None:
            record = self._username_db.get_username_record(target_username)

        # Step 1: Check following_status cache (Requirement 3.6)
        if record and record.following_status:
            for account in available_accounts:
                if record.following_status.get(account, False):
                    logger.debug(
                        "Cache hit: account '%s' follows '%s'", account, target_username
                    )
                    return account

        # Step 2: Check ProfileAccessTracker (Requirement 3.6, 3.7)
        if self._profile_tracker is not None:
            profile_summary = self._profile_tracker.get_profile_summary(target_username)
            accessible_by = profile_summary.get("accessible_by", [])

            for account in available_accounts:
                if account in accessible_by:
                    logger.debug(
                        "Tracker hit: account '%s' can access '%s'", account, target_username
                    )
                    # Update cache (Requirement 3.7)
                    if record is not None and self._username_db is not None:
                        if not record.following_status:
                            record.following_status = {}
                        record.following_status[account] = True
                        self._username_db.update_metadata(
                            target_username,
                            {"following_status": record.following_status},
                        )
                    return account

        # Step 3: Fall back to source account (Requirement 3.3)
        if record and record.source_account in available_accounts:
            logger.debug(
                "Fallback: using source account '%s' for '%s'",
                record.source_account,
                target_username,
            )
            return record.source_account

        # Step 4: No following relationship found (Requirement 3.4)
        logger.debug(
            "No following relationship found for '%s' among %s",
            target_username,
            available_accounts,
        )
        return None

    def _check_following(self, account: str, username: str) -> bool:
        """Check if account follows username using cache then tracker."""
        # Check cache
        if self._username_db is not None:
            record = self._username_db.get_username_record(username)
            if record and record.following_status:
                cached = record.following_status.get(account)
                if cached is not None:
                    return cached

        # Check tracker
        if self._profile_tracker is not None:
            summary = self._profile_tracker.get_profile_summary(username)
            accessible_by = summary.get("accessible_by", [])
            return account in accessible_by

        return False


