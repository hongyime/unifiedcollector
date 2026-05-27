"""
Profile Scanner — lightweight profile info fetch with optional parallelism.

Fetches profile metadata (followers, following, post count, bio, name,
public/private, verified) for tracked usernames WITHOUT collecting their
followers/following lists.

Cost: 1 API call per username (profile page only).
Skips usernames whose profile was scanned within `max_age_hours` (default 24h).

Parallelism:
  Multiple accounts can scan concurrently (one thread per account).
  Each worker has its own rate limiter with staggered start delays so they
  don't all fire at the same time.  DB writes are serialised through a lock.
  Max workers is capped at min(num_accounts, MAX_SCAN_WORKERS) to avoid
  hammering the same IP.
"""
from __future__ import annotations

import os
import sys
import time
import threading
import queue
import random
import datetime
from typing import Optional

_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from src.config import (
    INSTAGRAM_ACCOUNTS,
    ENUM_PAUSE_EVERY,
    ENUM_PAUSE_SECONDS,
    MAX_RETRIES,
    RETRY_BASE_DELAY,
    RETRY_MAX_DELAY,
    MIN_DELAY,
    MAX_DELAY,
)
from src.account_manager import InstagramAccountManager
from src.user_metadata_manager import UserMetadataManager
from src.rate_limiter import RateLimiter
from src.resilience import _SHUTDOWN

# Hard cap: never run more than this many parallel workers regardless of account count.
# Keeps total IP-level request rate sane even with many accounts.
MAX_SCAN_WORKERS = 3

# Stagger worker start times so they don't all fire simultaneously.
WORKER_START_STAGGER_SECONDS = 8


def _get_db():
    import os as _os
    from db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
    return _get_db._instance


_get_db._instance = None

# Global DB write lock — shared across all worker threads
_DB_WRITE_LOCK = threading.Lock()


def _needs_scan(username: str, max_age_hours: float) -> bool:
    """Return True if this username has no profile data or data is stale."""
    from db.repositories.profile_repository import ProfileRepository
    return ProfileRepository(_get_db()).needs_refresh(username, max_age_hours)


def _fetch_and_save(
    username: str,
    loader,
    account_name: str,
    metadata: UserMetadataManager,
) -> bool:
    """Fetch one profile page and persist it. Returns True on success."""
    import instaloader
    from io_utils import retry_with_backoff
    try:
        profile = retry_with_backoff(
            instaloader.Profile.from_username,
            loader.context,
            username,
            max_retries=MAX_RETRIES,
            base_delay=RETRY_BASE_DELAY,
            max_delay=RETRY_MAX_DELAY,
            label=f"scan:{username}",
        )
        if profile is None:
            print(f"[SCAN:{account_name}] Could not load profile for {username}")
            return False
        with _DB_WRITE_LOCK:
            metadata.update_profile(username, profile, account_name)
        return True
    except instaloader.exceptions.ProfileNotExistsException:
        print(f"[SCAN:{account_name}] Profile does not exist: {username}")
        return False
    except Exception as e:
        print(f"[SCAN:{account_name}] Error scanning {username}: {e}")
        return False


class _ScanWorker(threading.Thread):
    """One worker thread — owns one authenticated account session."""

    def __init__(
        self,
        account: dict,
        work_queue: "queue.Queue[str | None]",
        counters: dict,
        counters_lock: threading.Lock,
        max_age_hours: float,
        stagger_delay: float,
    ):
        super().__init__(daemon=True, name=f"scan-{account['name']}")
        self.account = account
        self.work_queue = work_queue
        self.counters = counters
        self.counters_lock = counters_lock
        self.max_age_hours = max_age_hours
        self.stagger_delay = stagger_delay
        self._manager: Optional[InstagramAccountManager] = None
        self._loader = None
        self._metadata = UserMetadataManager()
        # Each worker gets its own rate limiter so delays are independent
        self._rate = RateLimiter(
            min_delay=MIN_DELAY,
            max_delay=MAX_DELAY,
            label=f"scan-{account['name']}",
        )
        self._local_count = 0

    def _login(self) -> bool:
        self._manager = InstagramAccountManager()
        self._loader = self._manager.get_authenticated_loader(self.account['name'])
        return self._loader is not None

    def run(self):
        # Stagger start so workers don't all fire at t=0
        if self.stagger_delay > 0:
            print(f"[SCAN:{self.account['name']}] Starting in {self.stagger_delay:.0f}s...")
            time.sleep(self.stagger_delay)

        if not self._login():
            print(f"[SCAN:{self.account['name']}] Could not authenticate — worker exiting")
            return

        print(f"[SCAN:{self.account['name']}] Worker ready")

        while not _SHUTDOWN.is_set():
            try:
                username = self.work_queue.get(timeout=2)
            except queue.Empty:
                continue

            if username is None:  # poison pill — stop signal
                self.work_queue.task_done()
                break

            try:
                ok = _fetch_and_save(
                    username, self._loader, self.account['name'], self._metadata
                )
                self._local_count += 1
                with self.counters_lock:
                    if ok:
                        self.counters['scanned'] += 1
                    else:
                        self.counters['failed'] += 1

                # Per-worker rate limiting
                self._rate.periodic(
                    self._local_count,
                    every=ENUM_PAUSE_EVERY,
                    seconds=ENUM_PAUSE_SECONDS,
                )
                self._rate.short_delay()

            except Exception as e:
                print(f"[SCAN:{self.account['name']}] Unexpected error on {username}: {e}")
                with self.counters_lock:
                    self.counters['failed'] += 1
            finally:
                self.work_queue.task_done()

        if self._manager:
            self._manager.logout()
        print(f"[SCAN:{self.account['name']}] Worker done — scanned {self._local_count} profiles")


class ProfileScanner:
    """Fetch and store profile metadata for a list of usernames.

    Single-account mode (workers=1): sequential, safe, simple.
    Multi-account mode (workers>1): parallel workers, one per account,
      staggered starts, shared work queue, serialised DB writes.

    Args:
        account_name:   use a specific account (single-worker mode)
        max_age_hours:  skip profiles scanned more recently than this
        workers:        number of parallel workers (None = auto from account count,
                        capped at MAX_SCAN_WORKERS)
    """

    def __init__(
        self,
        account_name: str | None = None,
        max_age_hours: float = 24.0,
        workers: int | None = None,
    ):
        self.max_age_hours = max_age_hours

        # Determine which accounts to use
        if account_name:
            acct = next((a for a in INSTAGRAM_ACCOUNTS if a['name'] == account_name), None)
            if not acct:
                raise ValueError(f"Account '{account_name}' not found in config")
            self._accounts = [acct]
        else:
            self._accounts = list(INSTAGRAM_ACCOUNTS)

        if not self._accounts:
            raise RuntimeError("No Instagram accounts configured")

        # Cap workers
        max_w = min(len(self._accounts), MAX_SCAN_WORKERS)
        self._num_workers = min(workers, max_w) if workers else max_w

        print(f"[SCAN] Using {self._num_workers} worker(s) across "
              f"{len(self._accounts)} account(s) (IP cap: {MAX_SCAN_WORKERS})")

    # ── helpers ──────────────────────────────────────────────────────────

    def _build_work_list(
        self,
        usernames: list[str] | None,
        force: bool,
    ) -> tuple[list[str], int]:
        """Return (to_scan, skipped_count)."""
        if usernames is None:
            from db.repositories.username_repository import UsernameRepository
            rows = UsernameRepository(_get_db()).get_all()
            usernames = [r["username"] for r in rows]

        if force:
            return list(usernames), 0

        to_scan = []
        skipped = 0
        for u in usernames:
            if _needs_scan(u, self.max_age_hours):
                to_scan.append(u)
            else:
                skipped += 1
        return to_scan, skipped

    # ── public API ────────────────────────────────────────────────────────

    def scan_one(self, username: str, force: bool = False) -> bool:
        """Scan a single username. Skips if fresh unless *force* is True."""
        if not force and not _needs_scan(username, self.max_age_hours):
            print(f"[SCAN] {username} — profile fresh, skipping")
            return True
        print(f"[SCAN] Fetching profile: {username}")
        # Single-account fetch for one-off calls
        mgr = InstagramAccountManager()
        loader = mgr.get_authenticated_loader(self._accounts[0]['name'])
        if not loader:
            print("[SCAN] Could not authenticate")
            return False
        meta = UserMetadataManager()
        ok = _fetch_and_save(username, loader, self._accounts[0]['name'], meta)
        mgr.logout()
        return ok

    def scan_all(
        self,
        usernames: list[str] | None = None,
        force: bool = False,
        max_users: int | None = None,
    ) -> dict:
        """Scan all (or a subset of) tracked usernames, using parallel workers.

        Args:
            usernames:  explicit list; if None, reads all from DB
            force:      re-scan even if profile is fresh
            max_users:  stop after this many actual API calls (None = no limit)

        Returns:
            dict with keys: scanned, skipped, failed, total
        """
        to_scan, skipped = self._build_work_list(usernames, force)

        if max_users:
            to_scan = to_scan[:max_users]

        total_input = len(to_scan) + skipped
        
        # Session banner
        from src.session_tracker import SessionTracker
        session = SessionTracker("Profile Scan", self._accounts[0]['name'])
        session.print_start_banner(total_items=len(to_scan))
        
        print(f"[SCAN] {len(to_scan)} to scan, {skipped} already fresh "
              f"(total tracked: {total_input})")

        if not to_scan:
            print("[SCAN] Nothing to do.")
            return {"scanned": 0, "skipped": skipped, "failed": 0, "total": total_input}

        # Shared state
        work_q: queue.Queue[str | None] = queue.Queue()
        counters = {"scanned": 0, "failed": 0}
        counters_lock = threading.Lock()

        # Fill queue
        for u in to_scan:
            work_q.put(u)

        # Poison pills — one per worker
        for _ in range(self._num_workers):
            work_q.put(None)

        # Launch workers (use only as many accounts as workers needed)
        workers_to_use = self._accounts[: self._num_workers]
        threads: list[_ScanWorker] = []
        for i, acct in enumerate(workers_to_use):
            stagger = i * WORKER_START_STAGGER_SECONDS
            t = _ScanWorker(
                account=acct,
                work_queue=work_q,
                counters=counters,
                counters_lock=counters_lock,
                max_age_hours=self.max_age_hours,
                stagger_delay=stagger,
            )
            threads.append(t)
            t.start()

        # Wait for all work to finish (or shutdown) with progress updates
        last_progress = 0
        try:
            while any(t.is_alive() for t in threads):
                if _SHUTDOWN.is_set():
                    print("[SCAN] Shutdown — draining queue")
                    # Drain remaining items so workers can exit
                    while not work_q.empty():
                        try:
                            work_q.get_nowait()
                            work_q.task_done()
                        except queue.Empty:
                            break
                    break
                
                # Progress update every 10 scans
                with counters_lock:
                    current = counters["scanned"] + counters["failed"]
                if current > last_progress and current % 10 == 0:
                    session.print_progress(current, len(to_scan), f"| workers: {self._num_workers}")
                    last_progress = current
                
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass

        for t in threads:
            t.join(timeout=10)

        result = {
            "scanned": counters["scanned"],
            "skipped": skipped,
            "failed": counters["failed"],
            "total": total_input,
        }
        
        session.print_end_banner(success=True, items_processed=result['scanned'])
        
        print(f"\n[SCAN] Done — scanned={result['scanned']}, "
              f"skipped={result['skipped']}, failed={result['failed']}")
        return result

    def print_profile_report(self, username: str) -> None:
        """Print a human-readable profile summary including change history."""
        from db.repositories.profile_repository import ProfileRepository
        repo = ProfileRepository(_get_db())
        profile = repo.get_profile(username)
        if not profile:
            print(f"[SCAN] No profile data for {username}. Run scan-profiles first.")
            return

        print(f"\n  {'─'*50}")
        print(f"  @{username}")
        print(f"  {'─'*50}")
        print(f"  Name      : {profile.get('full_name', '—')}")
        bio = (profile.get('biography') or '—').replace('\n', ' ')
        print(f"  Bio       : {bio[:80]}")
        print(f"  Followers : {profile.get('followers_count', 0):,}")
        print(f"  Following : {profile.get('following_count', 0):,}")
        print(f"  Posts     : {profile.get('media_count', 0):,}")
        print(f"  Public    : {'Yes' if profile.get('is_public') else 'No'}")
        print(f"  Verified  : {'Yes' if profile.get('is_verified') else 'No'}")
        last_ts = profile.get('last_collected_ts')
        if last_ts:
            dt = datetime.datetime.fromtimestamp(last_ts).strftime('%Y-%m-%d %H:%M')
            print(f"  Last scan : {dt}")

        # Show recent changes
        changes = repo.get_profile_changes(username, limit=5)
        if changes:
            print(f"\n  Recent changes:")
            for ch in changes:
                dt = datetime.datetime.fromtimestamp(ch['snapshot_ts']).strftime('%Y-%m-%d %H:%M')
                for field, vals in ch['changes'].items():
                    print(f"    {dt}  {field}: {vals['from']!r} → {vals['to']!r}")

        # Show username history (renames)
        user_id = profile.get('user_id')
        if user_id:
            history = repo.get_username_history(user_id)
            if len(history) > 1:
                print(f"\n  Username history (same account ID {user_id}):")
                for h in history:
                    first = datetime.datetime.fromtimestamp(h['first_seen_ts']).strftime('%Y-%m-%d')
                    last_seen = datetime.datetime.fromtimestamp(h['last_seen_ts']).strftime('%Y-%m-%d')
                    print(f"    @{h['username']}  (first seen {first}, last seen {last_seen})")

        print(f"  {'─'*50}\n")


__all__ = ["ProfileScanner", "MAX_SCAN_WORKERS"]
