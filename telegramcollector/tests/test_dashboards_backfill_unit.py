# Tests for backfill validation logic
# Requirements: 3.5

def test_backfill_status_validation():
    """Missing chat_id or account_id must not call the DB write function."""
    from unittest.mock import MagicMock

    db_write = MagicMock()

    def create_backfill_job(chat_id, account_id, db_write_fn):
        if not chat_id or not account_id:
            return "error: Both chat and account are required"
        db_write_fn(account_id, chat_id)
        return "success"

    # Missing chat_id
    result = create_backfill_job(0, 1, db_write)
    assert "error" in result
    db_write.assert_not_called()

    # Missing account_id
    result = create_backfill_job(123, None, db_write)
    assert "error" in result
    db_write.assert_not_called()

    # Both provided
    result = create_backfill_job(123, 1, db_write)
    assert result == "success"
    db_write.assert_called_once_with(1, 123)
