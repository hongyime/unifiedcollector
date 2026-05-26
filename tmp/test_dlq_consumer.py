"""Behavioral verification for DLQConsumer (Wave 2.5).

Tests against live postgres on port 5500. Uses isolated source strings
prefixed with `_dlqtest_` so we don't touch real DLQ rows (3236 of them).
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
if ENV_FILE.exists() and "DATABASE_URL" not in os.environ:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("DATABASE_URL="):
            os.environ["DATABASE_URL"] = line.split("=", 1)[1]
        elif line.startswith("POSTGRES_SSL_MODE="):
            os.environ["POSTGRES_SSL_MODE"] = line.split("=", 1)[1]

from src.db.connection import get_pool, close_pool  # noqa: E402
from src.core.dlq_consumer import (  # noqa: E402
    DLQConsumer, PermanentError, compute_next_retry,
)


def assert_eq(name, got, want):
    if got == want:
        print(f"  OK    {name}: {got!r}")
    else:
        print(f"  FAIL  {name}: got={got!r} want={want!r}")
        raise SystemExit(1)


def assert_true(name, cond, detail=""):
    print(f"  {'OK' if cond else 'FAIL'}    {name}{(' ' + detail) if detail else ''}")
    if not cond:
        raise SystemExit(1)


TEST_SOURCE = f"_dlqtest_{os.getpid()}"


async def cleanup(pool):
    await pool.execute(
        "DELETE FROM dead_letter_queue WHERE source LIKE $1", "_dlqtest_%"
    )


async def insert_test_row(
    pool, *, entity_id="x", content_id="x",
    error="simulated", retry_count=0, status="pending",
    next_retry_offset_seconds=-1.0,  # -1 = already due
):
    """Insert one DLQ row with TEST_SOURCE, return id."""
    return await pool.fetchval(
        f"""
        INSERT INTO dead_letter_queue
            (source, entity_id, content_id, error_message, retry_count,
             status, next_retry_at)
        VALUES ($1, $2, $3, $4, $5, $6,
                NOW() + INTERVAL '1 second' * {next_retry_offset_seconds:.6f})
        RETURNING id
        """,
        TEST_SOURCE, entity_id, content_id, error, retry_count, status,
    )


async def main():
    pool = await get_pool()
    # Apply migration in case it wasn't applied yet
    schema = (Path(__file__).resolve().parent.parent / "src" / "db" / "schemas"
              / "collector.sql").read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(schema)

    await cleanup(pool)

    try:
        print("=" * 60)
        print("Test 1: argument validation")
        print("=" * 60)
        try:
            DLQConsumer(None)  # type: ignore[arg-type]
            assert_true("None pool rejected", False)
        except ValueError:
            assert_true("None pool rejected", True)
        for bad in [{"max_retries": 0}, {"batch_size": 0}, {"scan_interval_seconds": 0}]:
            try:
                DLQConsumer(pool, **bad)
                assert_true(f"reject {bad}", False)
            except ValueError:
                assert_true(f"reject {bad}", True)

        print("\n" + "=" * 60)
        print("Test 2: compute_next_retry exponential growth + cap")
        print("=" * 60)
        # base=10, max=200
        delays = [compute_next_retry(i, base_seconds=10, max_backoff_seconds=200)
                  for i in range(10)]
        # i=0 -> ~10..20, i=1 -> ~20..30, i=2 -> ~40..50, ...
        assert_true(f"delay[0] in [10,20]: {delays[0]:.1f}", 10 <= delays[0] <= 20)
        assert_true(f"delay[1] in [20,30]: {delays[1]:.1f}", 20 <= delays[1] <= 30)
        assert_true(f"delay[2] in [40,50]: {delays[2]:.1f}", 40 <= delays[2] <= 50)
        # All capped at max=200
        assert_true(f"delay[9] capped: {delays[9]:.1f}", delays[9] <= 200)
        # Negative retry_count handled gracefully
        d = compute_next_retry(-5, base_seconds=10, max_backoff_seconds=200)
        assert_true(f"negative retry handled: {d:.1f}", 10 <= d <= 20)

        print("\n" + "=" * 60)
        print("Test 3: handler success deletes the row")
        print("=" * 60)
        row_id = await insert_test_row(pool)
        consumer = DLQConsumer(pool)
        called = []
        async def good_handler(row):
            called.append(row["id"])
        consumer.register(TEST_SOURCE, good_handler)
        n = await consumer.run_once()
        assert_eq("processed 1", n, 1)
        assert_eq("handler called once", called, [row_id])
        c = await pool.fetchval(
            "SELECT COUNT(*) FROM dead_letter_queue WHERE id=$1", row_id
        )
        assert_eq("row deleted", c, 0)
        assert_eq("stats.succeeded", consumer.stats["succeeded"], 1)

        print("\n" + "=" * 60)
        print("Test 4: handler failure schedules retry with backoff")
        print("=" * 60)
        row_id = await insert_test_row(pool)
        consumer = DLQConsumer(pool, base_backoff_seconds=5, max_backoff_seconds=60)
        async def bad_handler(row):
            raise RuntimeError("simulated transient")
        consumer.register(TEST_SOURCE, bad_handler)
        await consumer.run_once()

        row = await pool.fetchrow(
            "SELECT * FROM dead_letter_queue WHERE id=$1", row_id
        )
        assert_eq("retry_count incremented", row["retry_count"], 1)
        assert_eq("status back to pending", row["status"], "pending")
        assert_true("error_message updated",
                    "RuntimeError" in row["error_message"])
        # next_retry_at should be ~5-10s in the future (base=5, retry=1 -> 10+jitter)
        delay = (row["next_retry_at"] -
                 row["last_attempt_at"]).total_seconds()
        assert_true(f"delay in [10,20]: {delay:.1f}s", 10 <= delay <= 20)
        assert_true("last_attempt_at set", row["last_attempt_at"] is not None)
        assert_eq("stats.retried", consumer.stats["retried"], 1)
        await pool.execute("DELETE FROM dead_letter_queue WHERE id=$1", row_id)

        print("\n" + "=" * 60)
        print("Test 5: max_retries exhausts -> status=failed")
        print("=" * 60)
        # Insert with retry_count already at max-1 so next failure trips
        row_id = await insert_test_row(pool, retry_count=4)
        consumer = DLQConsumer(pool, max_retries=5)
        async def bad(row):
            raise RuntimeError("permanent transient")
        consumer.register(TEST_SOURCE, bad)
        await consumer.run_once()
        row = await pool.fetchrow(
            "SELECT * FROM dead_letter_queue WHERE id=$1", row_id
        )
        assert_eq("retry_count maxed", row["retry_count"], 5)
        assert_eq("status=failed", row["status"], "failed")
        assert_eq("stats.failed", consumer.stats["failed"], 1)
        await pool.execute("DELETE FROM dead_letter_queue WHERE id=$1", row_id)

        print("\n" + "=" * 60)
        print("Test 6: PermanentError marks failed immediately")
        print("=" * 60)
        row_id = await insert_test_row(pool)
        consumer = DLQConsumer(pool)
        async def perm(row):
            raise PermanentError("schema is wrong")
        consumer.register(TEST_SOURCE, perm)
        await consumer.run_once()
        row = await pool.fetchrow(
            "SELECT * FROM dead_letter_queue WHERE id=$1", row_id
        )
        assert_eq("retry_count NOT incremented", row["retry_count"], 0)
        assert_eq("status=failed", row["status"], "failed")
        assert_true("error_message updated",
                    "schema is wrong" in row["error_message"])
        await pool.execute("DELETE FROM dead_letter_queue WHERE id=$1", row_id)

        print("\n" + "=" * 60)
        print("Test 7: not-yet-due rows are NOT claimed")
        print("=" * 60)
        row_id = await insert_test_row(
            pool, next_retry_offset_seconds=300  # 5 min in future
        )
        consumer = DLQConsumer(pool)
        async def h(row):
            raise AssertionError("should not be called")
        consumer.register(TEST_SOURCE, h)
        n = await consumer.run_once()
        assert_eq("nothing processed", n, 0)
        # Cleanup
        await pool.execute("DELETE FROM dead_letter_queue WHERE id=$1", row_id)

        print("\n" + "=" * 60)
        print("Test 8: failed rows are NOT re-claimed")
        print("=" * 60)
        row_id = await insert_test_row(pool, status="failed")
        consumer = DLQConsumer(pool)
        async def h(row):
            raise AssertionError("should not be called")
        consumer.register(TEST_SOURCE, h)
        n = await consumer.run_once()
        assert_eq("failed row skipped", n, 0)
        await pool.execute("DELETE FROM dead_letter_queue WHERE id=$1", row_id)

        print("\n" + "=" * 60)
        print("Test 9: unregistered source rows are NOT claimed")
        print("=" * 60)
        row_id = await insert_test_row(pool)
        consumer = DLQConsumer(pool)
        # NO handler registered
        n = await consumer.run_once()
        assert_eq("nothing claimed (no handler)", n, 0)
        row = await pool.fetchrow(
            "SELECT * FROM dead_letter_queue WHERE id=$1", row_id
        )
        assert_eq("status still pending", row["status"], "pending")
        assert_eq("retry_count unchanged", row["retry_count"], 0)
        await pool.execute("DELETE FROM dead_letter_queue WHERE id=$1", row_id)

        print("\n" + "=" * 60)
        print("Test 10: SKIP LOCKED — concurrent consumers don't double-claim")
        print("=" * 60)
        # Insert 4 rows
        ids = [await insert_test_row(pool, entity_id=f"e{i}") for i in range(4)]
        c1 = DLQConsumer(pool, batch_size=2)
        c2 = DLQConsumer(pool, batch_size=2)
        seen_by_1, seen_by_2 = [], []
        async def h1(row): seen_by_1.append(row["id"])
        async def h2(row): seen_by_2.append(row["id"])
        c1.register(TEST_SOURCE, h1)
        c2.register(TEST_SOURCE, h2)
        # Run concurrently
        await asyncio.gather(c1.run_once(), c2.run_once())
        all_seen = seen_by_1 + seen_by_2
        assert_eq("each row processed exactly once", sorted(all_seen), sorted(ids))
        assert_eq("no overlap", set(seen_by_1) & set(seen_by_2), set())
        # All 4 should now be deleted
        c = await pool.fetchval(
            "SELECT COUNT(*) FROM dead_letter_queue WHERE source=$1", TEST_SOURCE
        )
        assert_eq("all deleted", c, 0)

        print("\n" + "=" * 60)
        print("Test 11: recover_orphans flips stale in_progress -> pending")
        print("=" * 60)
        # Insert a row stuck in_progress with old last_attempt_at (1 hr ago)
        row_id = await pool.fetchval(
            """
            INSERT INTO dead_letter_queue
                (source, entity_id, error_message, status, last_attempt_at,
                 next_retry_at)
            VALUES ($1, 'orphan', 'crashed', 'in_progress',
                    NOW() - INTERVAL '1 hour',
                    NOW() - INTERVAL '1 hour')
            RETURNING id
            """,
            TEST_SOURCE,
        )
        consumer = DLQConsumer(pool)
        n = await consumer.recover_orphans(stale_after_seconds=300)
        assert_true(f"recovered >= 1: {n}", n >= 1)
        row = await pool.fetchrow(
            "SELECT status FROM dead_letter_queue WHERE id=$1", row_id
        )
        assert_eq("status -> pending", row["status"], "pending")
        await pool.execute("DELETE FROM dead_letter_queue WHERE id=$1", row_id)

        print("\n" + "=" * 60)
        print("Test 12: recover_orphans does NOT touch fresh in_progress")
        print("=" * 60)
        row_id = await pool.fetchval(
            """
            INSERT INTO dead_letter_queue
                (source, entity_id, error_message, status, last_attempt_at,
                 next_retry_at)
            VALUES ($1, 'fresh', 'wip', 'in_progress', NOW(), NOW())
            RETURNING id
            """,
            TEST_SOURCE,
        )
        consumer = DLQConsumer(pool)
        await consumer.recover_orphans(stale_after_seconds=300)
        row = await pool.fetchrow(
            "SELECT status FROM dead_letter_queue WHERE id=$1", row_id
        )
        assert_eq("fresh row untouched", row["status"], "in_progress")
        await pool.execute("DELETE FROM dead_letter_queue WHERE id=$1", row_id)

        print("\n" + "=" * 60)
        print("Test 13: run_forever can be stopped cleanly")
        print("=" * 60)
        consumer = DLQConsumer(pool, scan_interval_seconds=0.1)
        async def noop(row): pass
        consumer.register(TEST_SOURCE, noop)
        task = asyncio.create_task(consumer.run_forever())
        await asyncio.sleep(0.3)  # let it loop a few times
        consumer.stop()
        await asyncio.wait_for(task, timeout=2.0)
        assert_true("run_forever exited cleanly", True)

        print("\n" + "=" * 60)
        print("ALL DLQ-CONSUMER TESTS PASSED")
        print("=" * 60)

    finally:
        await cleanup(pool)
        await close_pool()


asyncio.run(main())
