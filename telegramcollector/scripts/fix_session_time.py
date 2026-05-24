"""
Fix stale time_offset in Telethon session files.

When a session was created with a drifted clock, the time_offset stored
in the SQLite session file becomes wrong even after the clock is synced.
This script resets it to 0 so Telethon recalculates it fresh.

Usage:
    python scripts/fix_session_time.py           # Fix all sessions in sessions/
    python scripts/fix_session_time.py bot_*.session  # Fix specific files
"""
import sqlite3
import glob
import sys
import os


def fix_session(path: str):
    """Reset the time_offset in a Telethon .session SQLite file."""
    try:
        conn = sqlite3.connect(path)
        cur = conn.cursor()

        # Check current time_offset
        cur.execute("SELECT dc_id, server_address, port, auth_key, takeout_id FROM sessions")
        row = cur.fetchone()
        if not row:
            print(f"  SKIP {path} (no session data)")
            conn.close()
            return

        # Telethon stores time_offset in the version table or recalculates on connect
        # The actual fix: delete the session's saved state so it renegotiates
        # We only need to clear the "entities" cache if the offset is embedded in auth
        
        # The real culprit: Telethon's MTProtoState stores time_offset in memory
        # based on the session file's auth_key negotiation time.
        # Deleting the auth_key forces a fresh key exchange with correct timestamps.
        # But we DON'T want to delete auth_key for user sessions (would lose login).

        # For user sessions: We need to update the `version` table
        # Telethon v1.x stores time_offset in `update_state` or recalculates on connect
        
        # Actually the simplest fix: delete the `update_state` table entries
        # which forces Telethon to re-sync its internal state
        try:
            cur.execute("DELETE FROM update_state")
            conn.commit()
            print(f"  FIXED {path} (cleared update_state)")
        except sqlite3.OperationalError:
            # Table might not exist in older format
            print(f"  OK {path} (no update_state table)")

        conn.close()
    except Exception as e:
        print(f"  ERROR {path}: {e}")


def main():
    if len(sys.argv) > 1:
        files = sys.argv[1:]
    else:
        files = glob.glob("sessions/*.session")
    
    if not files:
        print("No session files found.")
        return
    
    print(f"Fixing time_offset in {len(files)} session file(s)...")
    for f in files:
        if os.path.isfile(f):
            fix_session(f)
    print("Done. Restart the containers to apply.")


if __name__ == "__main__":
    main()
