#!/usr/bin/env python3
"""Operator script — one-shot Matrix backfill cycle.

Usage:
    python -m scripts.run_matrix_backfill [--concurrency 4] [--target 1000]
                                          [--max-pages 20]   [--room-limit 100]

Reads BEEPER_MATRIX_HOMESERVER / BEEPER_MATRIX_USER_ID /
BEEPER_MATRIX_ACCESS_TOKEN (or BEEPER_MATRIX_PASSWORD), opens an asyncpg
pool via `src.db.connection.get_pool`, constructs the read-only
`BeeperMatrixClient` + `MatrixEventWriter` + `MatrixBackfillDriver`,
syncs once to populate the in-memory rooms map, then runs `backfill_all`
once and prints a summary table.

Exit codes:
    0 — backfill completed with errors == 0 (all rooms either advanced
        cleanly or were already done).
    1 — at least one per-room error (the cycle still ran; the next
        invocation will retry the failing rooms).
    2 — fatal: missing env, login failure, or pool error before any
        per-room work could begin.

NOTE: this does NOT consume the scheduler — it's an out-of-band tool
operators run when they want to drive backfill manually (e.g. on first
deploy when the table is empty, or to catch up after a long outage).
The scheduler will pick the work up on its own once
MATRIX_BACKFILL_ENABLED=1 is set.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Optional

# Ensure repo root on sys.path when invoked as `python scripts/run_matrix_backfill.py`.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src.collectors.matrix_writer import MatrixEventWriter  # noqa: E402
from src.core.matrix_backfill import MatrixBackfillDriver  # noqa: E402
from src.core.matrix_client import BeeperMatrixClient  # noqa: E402
from src.db.connection import close_pool, get_pool  # noqa: E402

logger = logging.getLogger("matrix_backfill")


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run one Matrix backfill cycle.")
    p.add_argument("--concurrency", type=int, default=4,
                   help="Max parallel rooms (default: 4).")
    p.add_argument("--target", type=int, default=1000,
                   help="Per-room event target (default: 1000).")
    p.add_argument("--max-pages", type=int, default=20,
                   help="Per-room max /messages pages (default: 20).")
    p.add_argument("--room-limit", type=int, default=None,
                   help="Cap rooms processed this cycle (default: no cap).")
    p.add_argument("--store-path", default=os.environ.get(
        "BEEPER_MATRIX_STORE_PATH", "/data/matrix_store"),
                   help="matrix-nio store_path for E2EE keys.")
    return p.parse_args(argv)


def _env_or_die(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"ERROR: {name} is required in the environment", file=sys.stderr)
        sys.exit(2)
    return v


def _print_summary(summary: dict) -> None:
    """Render a compact summary table to stdout."""
    print("")
    print("Matrix backfill summary")
    print("=" * 70)
    print(f"rooms_total      : {summary.get('rooms_total', 0)}")
    print(f"rooms_processed  : {summary.get('rooms_processed', 0)}")
    print(f"events_fetched   : {summary.get('events_fetched', 0)}")
    print(f"errors           : {summary.get('errors', 0)}")
    print("-" * 70)
    print(f"{'room_id':<48}{'events':>8}{'pages':>7}{'done':>6}")
    print("-" * 70)
    per_room = summary.get("per_room") or []
    # Show top-20 by events_fetched (and any errored rooms) for sanity.
    sortable = [r for r in per_room if isinstance(r, dict)]
    sortable.sort(
        key=lambda r: (int(r.get("events_fetched") or 0)),
        reverse=True,
    )
    for r in sortable[:20]:
        rid = (r.get("room_id") or "?")[:48]
        ev = int(r.get("events_fetched") or 0)
        pg = int(r.get("pages_used") or 0)
        dn = "yes" if r.get("done") else "no"
        print(f"{rid:<48}{ev:>8}{pg:>7}{dn:>6}")
        err = r.get("error")
        if err:
            print(f"   error: {err}")
    print("=" * 70)


async def _amain(args: argparse.Namespace) -> int:
    homeserver = _env_or_die("BEEPER_MATRIX_HOMESERVER")
    user_id = _env_or_die("BEEPER_MATRIX_USER_ID")
    access_token = os.environ.get("BEEPER_MATRIX_ACCESS_TOKEN", "").strip()
    password = os.environ.get("BEEPER_MATRIX_PASSWORD", "").strip()
    if not access_token and not password:
        print(
            "ERROR: one of BEEPER_MATRIX_ACCESS_TOKEN or BEEPER_MATRIX_PASSWORD required",
            file=sys.stderr,
        )
        return 2

    try:
        pool = await get_pool()
    except Exception as exc:
        logger.error("Failed to open postgres pool: %s", exc)
        return 2

    client = BeeperMatrixClient(
        homeserver=homeserver,
        user_id=user_id,
        store_path=args.store_path,
        pool=pool,
        device_id=os.environ.get("BEEPER_MATRIX_DEVICE_ID") or None,
    )

    try:
        if access_token:
            await client.login(access_token=access_token)
        else:
            await client.login(password=password)
    except Exception as exc:
        logger.error("Matrix login failed: %s", exc)
        await close_pool()
        return 2

    # One sync_once to populate client.rooms; we don't use the SyncResponse.
    try:
        await client.sync_once(timeout_ms=10_000)
    except Exception as exc:
        logger.warning("Initial sync_once failed (continuing anyway): %s", exc)

    writer = MatrixEventWriter(pool)
    driver = MatrixBackfillDriver(client=client, writer=writer, log=logger)

    try:
        summary = await driver.backfill_all(
            concurrency=args.concurrency,
            per_room_target=args.target,
            max_pages=args.max_pages,
            room_limit=args.room_limit,
        )
    finally:
        try:
            await client.close()
        except Exception:
            pass
        await close_pool()

    _print_summary(summary)
    return 1 if int(summary.get("errors") or 0) > 0 else 0


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
