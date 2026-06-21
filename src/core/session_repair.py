"""Self-healing for SQLite session files.

Learned from the 2026-06-21 incident: Telethon `.session` files live on the WSL2
Docker volume and get their `entities` btree corrupted by hard kills / OOM
mid-write ("database disk image is malformed" / "rowid out of order"). A corrupt
session leaves the worker permanently "disconnected", which silently churns the
spider queue to `failed`.

This module detects corruption on startup and rebuilds the file via sqlite3
`.recover`, which preserves the `auth_key` (stored in the separate `sessions`
table) so the account stays logged in — NO re-login. It is safe + idempotent:
it integrity-checks first, backs up before touching, and only replaces the
original with a recovered file that itself passes integrity_check AND still has
the auth_key.

Requires the `sqlite3` CLI in the image (added to the Dockerfile alongside this).
"""
import logging
import os
import shutil
import sqlite3
import subprocess
import time

logger = logging.getLogger(__name__)


def _integrity_ok(path: str) -> bool:
    """True iff PRAGMA integrity_check returns 'ok' (and the DB is readable)."""
    try:
        c = sqlite3.connect(path, timeout=10)
        try:
            row = c.execute("PRAGMA integrity_check").fetchone()
            return bool(row) and row[0] == "ok"
        finally:
            c.close()
    except Exception:
        return False  # malformed / unreadable => needs recovery


def _has_authkey(path: str) -> bool:
    """True iff a sessions row with a real auth_key survives (login preserved)."""
    try:
        c = sqlite3.connect(path, timeout=10)
        try:
            r = c.execute("SELECT length(auth_key) FROM sessions").fetchone()
            return bool(r) and bool(r[0]) and r[0] >= 32
        finally:
            c.close()
    except Exception:
        return False


def ensure_healthy_session(session_path: str) -> bool:
    """Integrity-check `session_path`; if corrupt, rebuild via sqlite3 `.recover`,
    preserving the auth_key. Returns True if the file ends up healthy (already
    fine, or repaired), False if it could not be salvaged (caller should surface
    a possible re-auth need). Never destructive: backs up before any change and
    only replaces on a verified-good recovery.
    """
    spath = session_path if session_path.endswith(".session") else f"{session_path}.session"
    if not os.path.exists(spath):
        return True  # nothing to check; Telethon will create a fresh authed file
    if _integrity_ok(spath):
        return True

    logger.warning("session_repair: %s is CORRUPT — attempting sqlite3 .recover", spath)
    if not shutil.which("sqlite3"):
        logger.error("session_repair: sqlite3 CLI unavailable; cannot recover %s "
                     "(soft-corrupt sessions may still connect)", spath)
        return _has_authkey(spath)

    # Fold any WAL into the main DB first (best-effort; ignore if too broken).
    try:
        c = sqlite3.connect(spath, timeout=10)
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
    except Exception:
        pass

    bak = f"{spath}.corrupt-{time.strftime('%Y%m%d%H%M%S')}.bak"
    rec = f"{spath}.rec"
    try:
        shutil.copy2(spath, bak)
        if os.path.exists(rec):
            os.remove(rec)
        # `sqlite3 file ".recover"` salvages a SQL dump of every readable page;
        # piping it into a fresh DB rebuilds clean, in-order btrees.
        dump = subprocess.run(["sqlite3", spath, ".recover"],
                              capture_output=True, timeout=300)
        if not dump.stdout:
            logger.error("session_repair: .recover yielded nothing for %s", spath)
            return _has_authkey(spath)
        subprocess.run(["sqlite3", rec], input=dump.stdout,
                       capture_output=True, timeout=300)
        if _integrity_ok(rec) and _has_authkey(rec):
            os.replace(rec, spath)
            for ext in ("-wal", "-shm"):
                p = spath + ext
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            logger.warning("session_repair: %s RECOVERED ok (corrupt backup -> %s)",
                           spath, bak)
            return True
        logger.error("session_repair: recovered file failed verify for %s", spath)
        if os.path.exists(rec):
            os.remove(rec)
        return _has_authkey(spath)
    except Exception:
        logger.exception("session_repair: recovery raised for %s", spath)
        return False
