"""Rebuild account_access table from existing relationships.

Reads all (source, target, type) rows where type='following' and source matches
a configured account username. For each match, upserts account_access with follows=1.

Run after any spider batch to keep the access map current (spider auto-updates
account_access incrementally, but this rebuilds from scratch if needed):

    python scripts/rebuild_access_map.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db.manager import DatabaseManager
from src.config import INSTAGRAM_ACCOUNTS

db = DatabaseManager(os.environ.get("DATABASE_URL", ""))

# Build lookup: username -> account_name for all configured accounts
acct_by_username = {a['username']: a['name'] for a in INSTAGRAM_ACCOUNTS}

print(f"[ACCESS-MAP] Rebuilding from relationships for {len(INSTAGRAM_ACCOUNTS)} accounts...")

# For each configured account username, find everyone they follow in relationships
total = 0
for acct in INSTAGRAM_ACCOUNTS:
    rows = db.fetchall(
        "SELECT target FROM relationships WHERE source=? AND type='following'",
        (acct['username'],),
    )
    if not rows:
        print(f"  {acct['name']}: no following relationships found (run spider --seed first)")
        continue
    now = time.time()
    with db.get_connection() as conn:
        conn.executemany(
            """INSERT INTO account_access (username, account_name, follows, last_checked_ts)
               VALUES (?, ?, 1, ?)
               ON CONFLICT(username, account_name) DO UPDATE SET
                   follows=1, last_checked_ts=excluded.last_checked_ts""",
            [(r['target'], acct['name'], now) for r in rows],
        )
    print(f"  {acct['name']}: {len(rows)} following entries written")
    total += len(rows)

print(f"[ACCESS-MAP] Done. {total} entries in account_access table.")
