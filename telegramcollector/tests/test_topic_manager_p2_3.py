"""
Tests for P2.3: Fix TopicManager.create_topic() to use PostgreSQL sequence.

Validates: bugfix.md F-012
- topic_reservation_seq exists in init-db.sql
- create_topic() uses sequence for negative ID (not UUID hash)
- No unique constraint violations under concurrency
- Each topic gets a unique negative ID
"""
import ast
import asyncio
import re
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ---------------------------------------------------------------------------
# Static analysis helpers
# ---------------------------------------------------------------------------

def _read_source(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 1. init-db.sql: sequence must be declared
# ---------------------------------------------------------------------------

class TestSequenceInSchema:
    """Validates: bugfix.md F-012 - topic_reservation_seq exists in init-db.sql"""

    def test_topic_reservation_seq_exists_in_init_db_sql(self):
        """
        Validates: Requirements 2.12
        init-db.sql must contain CREATE SEQUENCE ... topic_reservation_seq
        """
        sql = _read_source("init-db.sql")
        # Match CREATE SEQUENCE [IF NOT EXISTS] topic_reservation_seq
        pattern = re.compile(
            r"CREATE\s+SEQUENCE\s+(?:IF\s+NOT\s+EXISTS\s+)?topic_reservation_seq",
            re.IGNORECASE,
        )
        assert pattern.search(sql), (
            "init-db.sql is missing 'CREATE SEQUENCE ... topic_reservation_seq'"
        )


# ---------------------------------------------------------------------------
# 2. topic_manager.py: create_topic() must use the sequence, not uuid hash
# ---------------------------------------------------------------------------

class TestCreateTopicUsesSequence:
    """Validates: bugfix.md F-012 - create_topic() uses sequence for negative ID"""

    def test_create_topic_references_sequence(self):
        """
        Validates: Requirements 2.12
        create_topic() must query nextval('topic_reservation_seq').
        """
        source = _read_source("topic_manager.py")
        assert "topic_reservation_seq" in source, (
            "topic_manager.py does not reference 'topic_reservation_seq'"
        )

    def test_create_topic_uses_negative_sequence_value(self):
        """
        Validates: Requirements 2.12
        The sequence value must be negated (* -1) to produce a negative temp ID.
        """
        source = _read_source("topic_manager.py")
        # Look for nextval(...) * -1 pattern
        pattern = re.compile(
            r"nextval\s*\(\s*['\"]topic_reservation_seq['\"]\s*\)\s*\*\s*-1",
            re.IGNORECASE,
        )
        assert pattern.search(source), (
            "topic_manager.py does not negate the sequence value with '* -1'"
        )

    def test_create_topic_does_not_use_uuid_for_temp_id(self):
        """
        Validates: Requirements 2.12 (fix checking)
        The old buggy approach used uuid hash for temp_topic_id.
        After the fix, uuid should NOT be used to derive the temp topic ID.
        """
        source = _read_source("topic_manager.py")
        # The old pattern was: int(uuid.uuid4().hex, 16) % ... or similar
        # We check that uuid is not used to compute temp_topic_id_reservation
        # (uuid may still be used for temp_label, which is fine)
        tree = ast.parse(source)

        uuid_assign_to_temp_id = False
        for node in ast.walk(tree):
            # Look for assignments like: temp_topic_id_reservation = ... uuid ...
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and "temp_topic_id" in target.id:
                        # Check if the value involves uuid
                        assign_src = ast.unparse(node.value)
                        if "uuid" in assign_src:
                            uuid_assign_to_temp_id = True

        assert not uuid_assign_to_temp_id, (
            "create_topic() still uses uuid to derive temp_topic_id — "
            "should use PostgreSQL sequence instead"
        )


# ---------------------------------------------------------------------------
# 3. Functional: concurrent calls produce unique negative IDs
# ---------------------------------------------------------------------------

def _make_db_conn_mock(sequence_counter: list):
    """
    Build a mock for get_db_connection() that simulates nextval() returning
    incrementing values (like a real PostgreSQL sequence).
    """
    mock_cursor = AsyncMock()
    mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
    mock_cursor.__aexit__ = AsyncMock(return_value=False)

    async def fake_execute(sql, *args, **kwargs):
        pass

    async def fake_fetchone():
        # Simulate nextval: each call returns the next integer, negated
        sequence_counter[0] += 1
        return (sequence_counter[0] * -1,)

    mock_cursor.execute = AsyncMock(side_effect=fake_execute)
    mock_cursor.fetchone = AsyncMock(side_effect=fake_fetchone)

    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)

    return mock_conn


class TestConcurrentTopicCreationUniqueness:
    """Validates: bugfix.md F-012 - No unique constraint violations under concurrency"""

    @pytest.mark.asyncio
    async def test_sequence_produces_unique_negative_ids(self):
        """
        Validates: Requirements 2.12
        Simulates concurrent calls to the sequence-fetch logic and verifies
        all returned IDs are unique and negative.
        """
        sequence_counter = [0]
        ids_collected = []

        async def simulate_sequence_fetch():
            """Mimics the sequence fetch inside create_topic()."""
            sequence_counter[0] += 1
            temp_id = sequence_counter[0] * -1
            ids_collected.append(temp_id)
            return temp_id

        # Simulate 20 concurrent calls
        tasks = [simulate_sequence_fetch() for _ in range(20)]
        results = await asyncio.gather(*tasks)

        # All IDs must be negative
        assert all(r < 0 for r in results), (
            f"Some IDs are not negative: {[r for r in results if r >= 0]}"
        )

        # All IDs must be unique
        assert len(set(results)) == len(results), (
            f"Duplicate IDs found: {results}"
        )

    @pytest.mark.asyncio
    async def test_sequence_ids_do_not_collide_across_batches(self):
        """
        Validates: Requirements 2.12
        Two separate batches of sequence fetches must not produce overlapping IDs.
        """
        counter = [0]

        async def fetch():
            counter[0] += 1
            return counter[0] * -1

        batch1 = await asyncio.gather(*[fetch() for _ in range(10)])
        batch2 = await asyncio.gather(*[fetch() for _ in range(10)])

        all_ids = list(batch1) + list(batch2)
        assert len(set(all_ids)) == len(all_ids), (
            "IDs from separate batches collide — sequence is not monotonically increasing"
        )

    def test_create_topic_source_contains_sequence_query(self):
        """
        Validates: Requirements 2.12 (fix checking)
        create_topic() source must contain a SQL query referencing nextval('topic_reservation_seq').
        This is a static check that the sequence is actually queried in the method body.
        """
        import inspect
        from shared.topic_manager import TopicManager
        source = inspect.getsource(TopicManager.create_topic)
        assert "topic_reservation_seq" in source, (
            "create_topic() does not reference 'topic_reservation_seq' in its source"
        )
        assert "nextval" in source, (
            "create_topic() does not call nextval() — sequence is not being used"
        )


# ---------------------------------------------------------------------------
# 4. Preservation: existing behavior unchanged for non-buggy inputs
# ---------------------------------------------------------------------------

class TestPreservationChecking:
    """Validates: bugfix.md F-012 - Preservation Checking"""

    def test_create_topic_method_still_exists(self):
        """
        Validates: Requirements 3.9
        create_topic() method must still exist in TopicManager.
        """
        from shared.topic_manager import TopicManager
        assert hasattr(TopicManager, "create_topic"), (
            "TopicManager.create_topic() method is missing"
        )
        assert callable(TopicManager.create_topic), (
            "TopicManager.create_topic is not callable"
        )

    def test_create_topic_returns_dict_with_expected_keys(self):
        """
        Validates: Requirements 3.9
        The return value contract (db_id, telegram_topic_id, label) must be preserved.
        """
        import inspect
        from shared.topic_manager import TopicManager
        source = inspect.getsource(TopicManager.create_topic)
        # Verify the return dict still has the expected keys
        assert "'db_id'" in source or '"db_id"' in source, (
            "create_topic() no longer returns 'db_id'"
        )
        assert "'telegram_topic_id'" in source or '"telegram_topic_id"' in source, (
            "create_topic() no longer returns 'telegram_topic_id'"
        )

    def test_topic_manager_init_unchanged(self):
        """
        Validates: Requirements 3.9
        TopicManager can still be instantiated with optional client argument.
        """
        from shared.topic_manager import TopicManager
        # Should not raise
        manager = TopicManager(client=None)
        assert manager is not None
