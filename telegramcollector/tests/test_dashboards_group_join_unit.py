# Tests for Group Join Queue validation logic
# Requirements: 4.6

import pytest
from unittest.mock import MagicMock


def approve_row(row_id, account_id, db_execute_fn):
    """Approve a group join queue row, validating that an account is selected."""
    if account_id is None:
        raise ValueError(f"Select an account before approving row {row_id}")
    db_execute_fn(
        "UPDATE collector.group_join_queue SET status='approved', account_id=%s WHERE id=%s",
        (account_id, row_id),
    )


def test_group_join_approve_requires_account():
    """Calling approve handler with account_id=None raises validation error and does not issue UPDATE."""
    db_execute = MagicMock()

    # Should raise without calling DB
    with pytest.raises(ValueError, match="Select an account"):
        approve_row(42, None, db_execute)
    db_execute.assert_not_called()

    # With valid account_id, should call DB
    approve_row(42, 7, db_execute)
    db_execute.assert_called_once()
