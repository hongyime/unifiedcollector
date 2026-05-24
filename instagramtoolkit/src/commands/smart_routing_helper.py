"""
Smart Routing Helper - Wraps process_operation_with_smart_routing for CLI commands.

Provides a thin helper that adds operation type display and verbose account
selection reasoning on top of the core routing function.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from src.operation_router import process_operation_with_smart_routing
from src.operation_classifier import OperationClassifier
from src.conservative_rate_limiter import ConservativeRateLimiter
from src.smart_account_selector import SmartAccountSelector
from src.username_database import UsernameDatabase

logger = logging.getLogger(__name__)


def run_operation(
    operation_name: str,
    target_usernames: list[str],
    execute_fn: Callable[[str, str], bool],
    *,
    username_db: Optional[UsernameDatabase] = None,
    rate_limiter: Optional[ConservativeRateLimiter] = None,
    account_selector: Optional[SmartAccountSelector] = None,
    available_accounts: Optional[list[str]] = None,
    verbose: bool = False,
) -> dict:
    """
    Wrap process_operation_with_smart_routing with CLI-friendly output.

    Displays the operation type before processing and, in verbose mode,
    shows account selection reasoning for each account assignment.

    Args:
        operation_name: Registered operation name (e.g. "download_stories")
        target_usernames: List of Instagram usernames to process
        execute_fn: Callable(account_name, username) -> bool
        username_db: Optional UsernameDatabase instance
        rate_limiter: Optional ConservativeRateLimiter instance
        account_selector: Optional SmartAccountSelector instance
        available_accounts: Optional list of account names to use
        verbose: If True, print account selection reasoning

    Returns:
        Result dict with total, success_count, failed_count, results
    """
    # Classify and display operation type
    classifier = OperationClassifier()
    operation_type = classifier.classify(operation_name)
    op_meta = classifier.get_operation_metadata(operation_name)

    print(f"[INFO] Operation: {operation_name}")
    print(f"[INFO] Type: {operation_type.value}  (rate limit weight: {op_meta.rate_limit_weight})")

    if verbose and available_accounts:
        _print_account_selection_reasoning(
            operation_name, operation_type, target_usernames,
            available_accounts, account_selector, username_db,
        )

    result = process_operation_with_smart_routing(
        operation_name=operation_name,
        target_usernames=target_usernames,
        execute_fn=execute_fn,
        username_db=username_db,
        rate_limiter=rate_limiter,
        account_selector=account_selector,
        available_accounts=available_accounts,
    )

    # Display summary
    print(
        f"[INFO] Done — total: {result['total']}, "
        f"success: {result['success_count']}, "
        f"failed: {result['failed_count']}"
    )

    return result


def _print_account_selection_reasoning(
    operation_name: str,
    operation_type,
    target_usernames: list[str],
    available_accounts: list[str],
    account_selector: Optional[SmartAccountSelector],
    username_db: Optional[UsernameDatabase],
) -> None:
    """Print verbose account selection reasoning."""
    from src.operation_classifier import OperationType

    print(f"[VERBOSE] Account selection reasoning for '{operation_name}':")
    print(f"[VERBOSE]   Available accounts: {', '.join(available_accounts)}")
    print(f"[VERBOSE]   Target usernames:   {len(target_usernames)}")

    if operation_type == OperationType.PUBLIC:
        print(
            f"[VERBOSE]   Strategy: PUBLIC — any account works; "
            f"assigning all to '{available_accounts[0]}'"
        )
        return

    # FOLLOWING_REQUIRED / MUTUAL_FOLLOWING — show per-username reasoning
    selector = account_selector or SmartAccountSelector(username_db=username_db)
    assignment = selector.select_for_batch(operation_type, target_usernames, available_accounts)

    print(f"[VERBOSE]   Strategy: {operation_type.value} — grouping by following relationships")
    for account, usernames in assignment.items():
        print(f"[VERBOSE]     {account} → {len(usernames)} username(s): {', '.join(usernames[:5])}"
              + (" ..." if len(usernames) > 5 else ""))


__all__ = ["run_operation"]


