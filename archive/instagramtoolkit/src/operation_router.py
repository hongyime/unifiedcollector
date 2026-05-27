"""
Operation Router - Main entry point for processing Instagram operations with
smart account selection, conservative rate limiting, and error recovery.

Requirements: 7.1–7.8, 8.1–8.7
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from src.operation_classifier import OperationClassifier, OperationType
from src.smart_account_selector import SmartAccountSelector
from src.conservative_rate_limiter import ConservativeRateLimiter
from src.username_database import UsernameDatabase

logger = logging.getLogger(__name__)


class RateLimitException(Exception):
    """Raised when Instagram rate-limits an account."""


def process_operation_with_smart_routing(
    operation_name: str,
    target_usernames: list[str],
    execute_fn: Callable[[str, str], bool],
    *,
    username_db: Optional[UsernameDatabase] = None,
    rate_limiter: Optional[ConservativeRateLimiter] = None,
    account_selector: Optional[SmartAccountSelector] = None,
    available_accounts: Optional[list[str]] = None,
) -> dict:
    """
    Process an Instagram operation with smart account selection and rate limiting.

    Preconditions:
    - operation_name is a registered operation name
    - target_usernames is a non-empty list of valid usernames
    - execute_fn(account_name, username) -> bool performs the actual operation
    - At least one account is available and not in cooldown

    Postconditions:
    - All usernames are processed or marked as failed
    - Username database is updated with access metadata for successes
    - Rate limits are respected throughout execution
    - Returns summary with total, success_count, failed_count

    Requirements: 7.1–7.8, 8.2, 8.6
    """
    if not target_usernames:
        return {"total": 0, "success_count": 0, "failed_count": 0,
                "results": {"success": [], "failed": []}}

    # Step 1: Classify operation (Requirement 7.1)
    classifier = OperationClassifier()
    operation_type = classifier.classify(operation_name)

    # Step 2: Initialise components
    if rate_limiter is None:
        rate_limiter = ConservativeRateLimiter()
    if username_db is None:
        username_db = UsernameDatabase()
    if account_selector is None:
        account_selector = SmartAccountSelector(username_db=username_db)

    # Step 3: Determine available accounts (Requirement 8.1)
    if available_accounts is None:
        try:
            from account_manager import InstagramAccountManager
            mgr = InstagramAccountManager()
            available_accounts = mgr.get_available_accounts(rate_limiter=rate_limiter)
        except Exception:
            available_accounts = []

    if not available_accounts:
        # All accounts in cooldown — wait for shortest cooldown to expire (Requirement 8.1)
        logger.warning("All accounts in cooldown; waiting for shortest cooldown to expire")
        _wait_for_shortest_cooldown(rate_limiter, available_accounts or [])
        # Re-fetch after waiting
        try:
            from account_manager import InstagramAccountManager
            mgr = InstagramAccountManager()
            available_accounts = mgr.get_available_accounts(rate_limiter=rate_limiter)
        except Exception:
            available_accounts = []

    if not available_accounts:
        logger.error("No available accounts after waiting — all usernames marked as failed")
        return {
            "total": len(target_usernames),
            "success_count": 0,
            "failed_count": len(target_usernames),
            "results": {"success": [], "failed": list(target_usernames)},
        }

    # Step 4: Assign usernames to accounts (Requirements 7.2, 7.3)
    account_assignment = account_selector.select_for_batch(
        operation_type, target_usernames, available_accounts
    )

    # Step 5: Process each account's batch
    results: dict[str, list[str]] = {"success": [], "failed": []}
    account_keys = list(account_assignment.keys())

    for idx, (account_name, usernames) in enumerate(account_assignment.items()):
        # Re-check availability (Requirement 7.4)
        if not rate_limiter.check_account_available(account_name):
            logger.warning("Account '%s' entered cooldown; marking %d usernames failed",
                           account_name, len(usernames))
            results["failed"].extend(usernames)
            continue

        # Process usernames for this account
        for i, username in enumerate(usernames):
            # Operation-specific delay (Requirement 7.5)
            rate_limiter.operation_delay(operation_type)

            # Progressive enumeration delay every 10 ops (Requirement 4.8)
            if i > 0 and i % 10 == 0:
                rate_limiter.following_enumeration_delay(i)

            try:
                success = execute_fn(account_name, username)

                if success:
                    results["success"].append(username)
                    # Update metadata (Requirement 7.8)
                    username_db.update_metadata(username, {
                        "last_accessed": time.time(),
                        "last_operation": operation_name,
                        "last_account": account_name,
                    })
                else:
                    results["failed"].append(username)

            except RateLimitException:
                # Emergency cooldown + mark remaining as failed (Requirement 8.2)
                logger.error("Rate limit hit on account '%s'; applying emergency cooldown", account_name)
                rate_limiter.emergency_cooldown(account_name, duration_minutes=15)
                results["failed"].extend(usernames[i:])
                break

            except Exception as exc:
                logger.error("Error processing '%s' with account '%s': %s", username, account_name, exc)
                results["failed"].append(username)

        # Account switch delay before next account (Requirement 4.5)
        if idx < len(account_keys) - 1:
            rate_limiter.account_switch_delay()

    # Step 6: Periodic database save with retry (Requirements 10.4, 8.7)
    _save_with_retry(username_db)

    return {
        "total": len(target_usernames),
        "success_count": len(results["success"]),
        "failed_count": len(results["failed"]),
        "results": results,
    }


def _wait_for_shortest_cooldown(
    rate_limiter: ConservativeRateLimiter,
    account_names: list[str],
) -> None:
    """Wait for the shortest cooldown among all accounts to expire (Requirement 8.1)."""
    if not account_names:
        return
    remaining_times = [
        rate_limiter.get_cooldown_remaining(name) for name in account_names
    ]
    min_wait = min((t for t in remaining_times if t > 0), default=0)
    if min_wait > 0:
        logger.info("Waiting %.0f seconds for shortest cooldown to expire", min_wait)
        time.sleep(min_wait + 1)  # +1s buffer


def _save_with_retry(db: UsernameDatabase, max_retries: int = 3) -> bool:
    """Save database with exponential backoff retry (Requirement 8.7)."""
    for attempt in range(max_retries):
        try:
            if db.save():
                return True
        except Exception as exc:
            logger.warning("DB save attempt %d/%d failed: %s", attempt + 1, max_retries, exc)
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)
    logger.error("Database save failed after %d attempts", max_retries)
    return False


