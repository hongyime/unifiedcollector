"""Behavioral verification for src/core/profile_access (Wave 2.3).

Runs against the live unifiedcollector postgres. Uses a session-scoped
unique source prefix so we don't pollute real data.
"""
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load DATABASE_URL from .env if not already set
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
if ENV_FILE.exists() and "DATABASE_URL" not in os.environ:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("DATABASE_URL="):
            os.environ["DATABASE_URL"] = line.split("=", 1)[1]
        elif line.startswith("POSTGRES_SSL_MODE="):
            os.environ["POSTGRES_SSL_MODE"] = line.split("=", 1)[1]

from src.db.connection import get_pool, close_pool  # noqa: E402
from src.core.profile_access import (  # noqa: E402
    ProfileAccessRepository, SmartAccountSelector,
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


# Use a unique source string so tests don't interfere with real data
TEST_SOURCE = f"_t_{uuid.uuid4().hex[:8]}"


async def cleanup(pool):
    """Drop all rows for our test source."""
    await pool.execute(
        "DELETE FROM profile_access_attempts WHERE source=$1", TEST_SOURCE
    )
    await pool.execute(
        "DELETE FROM profile_access_summary WHERE source=$1", TEST_SOURCE
    )


async def main():
    # Apply schema first (idempotent)
    pool = await get_pool()
    schema = (Path(__file__).resolve().parent.parent / "src" / "db" / "schemas"
              / "profile_access.sql").read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(schema)
    print(f"Schema applied. TEST_SOURCE={TEST_SOURCE}")

    repo = ProfileAccessRepository(pool)

    try:
        print("\n" + "=" * 60)
        print("Test 1: argument validation")
        print("=" * 60)
        try:
            ProfileAccessRepository(None)  # type: ignore[arg-type]
            assert_true("None pool rejected", False)
        except ValueError as e:
            assert_true("None pool rejected", True, f"-> {e}")

        for bad in [
            ("", "tgt", "acc"),
            ("src", "", "acc"),
            ("src", "tgt", ""),
        ]:
            try:
                await repo.record_attempt(*bad, can_access=True)
                assert_true(f"reject empty {bad}", False)
            except ValueError:
                assert_true(f"reject empty {bad}", True)

        try:
            await repo.cleanup_old_attempts(days=0)
            assert_true("reject days=0", False)
        except ValueError:
            assert_true("reject days=0", True)

        print("\n" + "=" * 60)
        print("Test 2: empty summary returns 'unknown' default")
        print("=" * 60)
        s = await repo.get_profile_summary(TEST_SOURCE, "ghost_target")
        assert_eq("status", s["status"], "unknown")
        assert_eq("accessible_by", s["accessible_by"], [])
        assert_eq("total_attempts", s["total_attempts"], 0)

        print("\n" + "=" * 60)
        print("Test 3: record_attempt creates summary on first attempt")
        print("=" * 60)
        await repo.record_attempt(
            TEST_SOURCE, "alice", "acc1", can_access=True,
            is_public=False, is_followed=True,
        )
        s = await repo.get_profile_summary(TEST_SOURCE, "alice")
        assert_eq("status tracked", s["status"], "tracked")
        assert_eq("is_public", s["is_public"], False)
        assert_eq("accessible_by", s["accessible_by"], ["acc1"])
        assert_eq("total_attempts", s["total_attempts"], 1)

        print("\n" + "=" * 60)
        print("Test 4: subsequent successful attempt appends to accessible_by")
        print("=" * 60)
        await repo.record_attempt(
            TEST_SOURCE, "alice", "acc2", can_access=True, is_public=False,
        )
        s = await repo.get_profile_summary(TEST_SOURCE, "alice")
        assert_eq("accessible_by has both",
                  sorted(s["accessible_by"]), ["acc1", "acc2"])
        assert_eq("total_attempts", s["total_attempts"], 2)

        # Recording acc1 AGAIN should NOT duplicate it
        await repo.record_attempt(
            TEST_SOURCE, "alice", "acc1", can_access=True, is_public=False,
        )
        s = await repo.get_profile_summary(TEST_SOURCE, "alice")
        assert_eq("no duplicate accessible_by",
                  sorted(s["accessible_by"]), ["acc1", "acc2"])
        assert_eq("total_attempts", s["total_attempts"], 3)

        print("\n" + "=" * 60)
        print("Test 5: failed attempt does NOT add to accessible_by")
        print("=" * 60)
        await repo.record_attempt(
            TEST_SOURCE, "alice", "acc3", can_access=False,
            error="403 Forbidden",
        )
        s = await repo.get_profile_summary(TEST_SOURCE, "alice")
        assert_true("acc3 NOT in accessible_by",
                    "acc3" not in s["accessible_by"])
        assert_eq("total_attempts incremented", s["total_attempts"], 4)

        print("\n" + "=" * 60)
        print("Test 6: get_best_account returns most recent winner")
        print("=" * 60)
        # acc2 succeeded most recently in test 4 (after acc1's success in
        # test 3); but we recorded acc1 again at the end of test 4. So
        # most-recent winner is acc1.
        best = await repo.get_best_account(
            TEST_SOURCE, "alice", ["acc1", "acc2", "acc3"]
        )
        assert_eq("best is most-recent successful", best, "acc1")

        # Filtering by available — exclude acc1
        best = await repo.get_best_account(
            TEST_SOURCE, "alice", ["acc2", "acc3"]
        )
        assert_eq("filtered best", best, "acc2")

        # No matching available -> None
        best = await repo.get_best_account(
            TEST_SOURCE, "alice", ["acc3"]
        )
        assert_eq("no winner among available", best, None)

        # Empty available -> None
        best = await repo.get_best_account(TEST_SOURCE, "alice", [])
        assert_eq("empty available", best, None)

        print("\n" + "=" * 60)
        print("Test 7: get_accessible_accounts")
        print("=" * 60)
        accs = await repo.get_accessible_accounts(TEST_SOURCE, "alice")
        assert_eq("accessible accounts", sorted(accs), ["acc1", "acc2"])

        accs = await repo.get_accessible_accounts(TEST_SOURCE, "ghost")
        assert_eq("ghost target -> []", accs, [])

        print("\n" + "=" * 60)
        print("Test 8: concurrent record_attempt — no lost updates")
        print("=" * 60)
        # Spawn 10 concurrent successful attempts on a fresh target.
        # All 10 distinct accounts should end up in accessible_by.
        target = "concurrent_target"
        tasks = [
            asyncio.create_task(repo.record_attempt(
                TEST_SOURCE, target, f"acc_concurrent_{i}",
                can_access=True, is_public=False,
            ))
            for i in range(10)
        ]
        await asyncio.gather(*tasks)
        s = await repo.get_profile_summary(TEST_SOURCE, target)
        accs = sorted(s["accessible_by"])
        assert_eq("all 10 accounts present", len(accs), 10)
        assert_eq("total_attempts", s["total_attempts"], 10)

        print("\n" + "=" * 60)
        print("Test 9: SmartAccountSelector.select_for_operation")
        print("=" * 60)
        sel = SmartAccountSelector(repo)
        winner = await sel.select_for_operation(
            TEST_SOURCE, "alice", ["acc1", "acc2"]
        )
        assert_eq("selector picks recent winner", winner, "acc1")

        # No history target -> None (caller falls back to LRU)
        winner = await sel.select_for_operation(
            TEST_SOURCE, "untouched", ["acc1", "acc2"]
        )
        assert_eq("no history -> None", winner, None)

        # Empty available -> None
        winner = await sel.select_for_operation(TEST_SOURCE, "alice", [])
        assert_eq("empty available -> None", winner, None)

        print("\n" + "=" * 60)
        print("Test 10: SmartAccountSelector.select_for_batch")
        print("=" * 60)
        # Set up: bob is best-accessed by acc1, charlie by acc2
        await repo.record_attempt(TEST_SOURCE, "bob", "acc1", True)
        await repo.record_attempt(TEST_SOURCE, "charlie", "acc2", True)

        result = await sel.select_for_batch(
            TEST_SOURCE, ["bob", "charlie", "noone"], ["acc1", "acc2"]
        )
        assert_eq("bob -> acc1", result["bob"], "acc1")
        assert_eq("charlie -> acc2", result["charlie"], "acc2")
        assert_eq("noone -> None", result["noone"], None)
        # Empty batch -> empty dict
        result = await sel.select_for_batch(TEST_SOURCE, [], ["acc1"])
        assert_eq("empty batch -> {}", result, {})

        print("\n" + "=" * 60)
        print("Test 11: SmartAccountSelector.get_following_overlap")
        print("=" * 60)
        # acc1 succeeded on alice + bob. acc2 succeeded on alice + charlie.
        overlap_acc1 = await sel.get_following_overlap(
            "acc1", TEST_SOURCE, ["alice", "bob", "charlie", "noone"]
        )
        assert_eq("acc1 overlap", sorted(overlap_acc1), ["alice", "bob"])

        overlap_acc2 = await sel.get_following_overlap(
            "acc2", TEST_SOURCE, ["alice", "bob", "charlie", "noone"]
        )
        assert_eq("acc2 overlap", sorted(overlap_acc2), ["alice", "charlie"])

        empty_overlap = await sel.get_following_overlap(
            "", TEST_SOURCE, ["alice"]
        )
        assert_eq("empty account -> set()", empty_overlap, set())

        print("\n" + "=" * 60)
        print("Test 12: get_statistics — overall + per-source")
        print("=" * 60)
        # Per-source — should reflect ONLY our test data
        stats = await repo.get_statistics(source=TEST_SOURCE)
        assert_eq("source label", stats["source"], TEST_SOURCE)
        # We have: 4 alice attempts (3 success, 1 fail) + 10 concurrent
        # all-success + 1 bob + 1 charlie = 16 total, 15 success
        assert_eq("total_attempts", stats["total_attempts"], 16)
        assert_eq("successful_attempts", stats["successful_attempts"], 15)
        assert_eq("unique_profiles", stats["unique_profiles"], 4)

        print("\n" + "=" * 60)
        print("Test 13: cleanup_old_attempts (with days=1, should keep all)")
        print("=" * 60)
        deleted = await repo.cleanup_old_attempts(days=1)
        assert_eq("nothing old to delete", deleted, 0)

        print("\n" + "=" * 60)
        print("ALL PROFILE-ACCESS TESTS PASSED")
        print("=" * 60)

    finally:
        await cleanup(pool)
        await close_pool()


asyncio.run(main())
