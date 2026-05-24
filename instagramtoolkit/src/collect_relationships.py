# Collect followers and following relationships using Instaloader
import os
import sys
import time
import random
import instaloader

_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from src.account_manager import InstagramAccountManager
from src.config import (
    INSTAGRAM_ACCOUNTS, DATA_DIR, MIN_DELAY, MAX_DELAY,
    ENUM_PAUSE_EVERY, ENUM_PAUSE_SECONDS,
    ENUM_ITEM_SLEEP_MIN, ENUM_ITEM_SLEEP_MAX,
    MAX_RETRIES, RETRY_BASE_DELAY, RETRY_MAX_DELAY,
    MAX_FOLLOWERS_PER_SESSION, MAX_FOLLOWING_PER_SESSION,
)
from src.profile_access_tracker import ProfileAccessTracker
from src.user_metadata_manager import UserMetadataManager
from src.io_utils import retry_with_backoff
from src.rate_limiter import RateLimiter
from src.resilience import _SHUTDOWN, _interruptible_sleep, with_internet_retry


# Read scraper filters from .env
FILTER_MAX_FOLLOWERS = int(os.environ.get('FILTER_MAX_FOLLOWERS', '0'))
FILTER_MIN_FOLLOWERS = int(os.environ.get('FILTER_MIN_FOLLOWERS', '0'))
FILTER_MAX_FOLLOWING = int(os.environ.get('FILTER_MAX_FOLLOWING', '0'))
FILTER_MIN_FOLLOWING = int(os.environ.get('FILTER_MIN_FOLLOWING', '0'))
FILTER_PUBLIC_ONLY = os.environ.get('FILTER_PUBLIC_ONLY', 'false').lower() == 'true'


def _get_db():
    """Return a module-level DatabaseManager singleton."""
    import os as _os
    from db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
    return _get_db._instance


_get_db._instance = None


def _apply_follower_filter(profile_obj) -> str | None:
    """Return a filter_reason string if profile_obj should be skipped, else None."""
    try:
        followers = getattr(profile_obj, 'followers', 0) or 0
        following = getattr(profile_obj, 'followees', 0) or 0
        is_private = getattr(profile_obj, 'is_private', False)

        if FILTER_MAX_FOLLOWERS > 0 and followers > FILTER_MAX_FOLLOWERS:
            return 'max_followers'
        if FILTER_MIN_FOLLOWERS > 0 and followers < FILTER_MIN_FOLLOWERS:
            return 'min_followers'
        if FILTER_MAX_FOLLOWING > 0 and following > FILTER_MAX_FOLLOWING:
            return 'max_following'
        if FILTER_MIN_FOLLOWING > 0 and following < FILTER_MIN_FOLLOWING:
            return 'min_following'
        if FILTER_PUBLIC_ONLY and is_private:
            return 'public_only'
    except Exception:
        pass
    return None


class RelationshipCollector:
    def __init__(self, account_name=None):
        os.makedirs(DATA_DIR, exist_ok=True)

        self.manager = InstagramAccountManager()
        self.loader = self.manager.get_authenticated_loader(account_name)

        if not self.loader:
            raise RuntimeError(f"Failed to authenticate account")

        # Initialize access tracker and metadata manager
        self.access_tracker = ProfileAccessTracker()
        self.metadata_manager = UserMetadataManager()
        self.rate = RateLimiter(label="spider")

        # Repository-backed storage
        from db.repositories.relationship_repository import RelationshipRepository
        from db.repositories.username_repository import UsernameRepository
        db = _get_db()
        self._rel_repo = RelationshipRepository(db)
        self._usr_repo = UsernameRepository(db)

        # In-memory caches (populated lazily for backward compat)
        self._usernames_cache: list[str] | None = None
        self._relationships_cache: list[dict] | None = None

    # ── Backward-compat properties ────────────────────────────────────────

    @property
    def usernames(self) -> list[str]:
        if self._usernames_cache is None:
            self._usernames_cache = self._load_usernames()
        return self._usernames_cache

    @usernames.setter
    def usernames(self, value):
        self._usernames_cache = value

    @property
    def relationships(self) -> list[dict]:
        if self._relationships_cache is None:
            self._relationships_cache = self._load_relationships()
        return self._relationships_cache

    @relationships.setter
    def relationships(self, value):
        self._relationships_cache = value

    def cleanup(self):
        """Cleanup resources."""
        if self.manager:
            self.manager.logout()

    # ── Private helpers (now delegate to repositories) ────────────────────

    def _load_usernames(self) -> list[str]:
        """Load usernames from DB, falling back to usernames.txt."""
        try:
            rows = self._usr_repo.get_all()
            usernames = [r["username"] for r in rows]
            if usernames:
                print(f"[LIST] Loaded {len(usernames)} usernames")
                return usernames
        except Exception as e:
            print(f"[ERROR] Error loading usernames from DB: {e}")
        # Fall back to flat file (backward compat / fresh install)
        filepath = os.path.join(DATA_DIR, "usernames.txt")
        try:
            with open(filepath) as f:
                usernames = [l.strip() for l in f if l.strip()]
            if usernames:
                print(f"[LIST] Loaded {len(usernames)} usernames from file")
            return usernames
        except FileNotFoundError:
            return []

    def _save_usernames(self):
        """Persist username cache to DB and backward-compat flat file."""
        cache = self._usernames_cache
        if cache is None:
            return
        for username in cache:
            try:
                self._usr_repo.add_username(username, source_account="collected")
            except Exception:
                pass
        try:
            unique_sorted = sorted(set(cache))
            filepath = os.path.join(DATA_DIR, "usernames.txt")
            with open(filepath, "w") as f:
                for u in unique_sorted:
                    f.write(u + "\n")
        except Exception:
            pass

    def _load_relationships(self) -> list[dict]:
        """Load relationships from DB, falling back to relationships.json."""
        try:
            rows = self._rel_repo.get_relationships()
            if rows:
                print(f"[STATS] Loaded {len(rows)} relationships")
                return rows
        except Exception as e:
            print(f"[ERROR] Error loading relationships from DB: {e}")
        # Fall back to JSON file (backward compat)
        import json as _json
        filepath = os.path.join(DATA_DIR, "relationships.json")
        try:
            with open(filepath) as f:
                return _json.load(f)
        except (FileNotFoundError, ValueError):
            return []

    def _save_relationships(self):
        """Persist relationship cache to DB and backward-compat JSON file."""
        if self._relationships_cache is None:
            return
        try:
            self._rel_repo.bulk_upsert(self._relationships_cache)
        except Exception as e:
            print(f"[ERROR] Error saving relationships to DB: {e}")
        import json as _json
        try:
            filepath = os.path.join(DATA_DIR, "relationships.json")
            with open(filepath, "w") as f:
                _json.dump(self._relationships_cache, f)
        except Exception:
            pass

    # ── Public API (unchanged signatures) ─────────────────────────────────

    def collect_for_user(self, username, max_followers=1000, max_following=1000):
        """Collect followers and following for a specific user."""
        if not username or username.strip() == '':
            print("[ERROR] Invalid username provided")
            return

        print(f"[SPIDER] Collecting relationships for: {username}")

        try:
            if not self.loader or not hasattr(self.loader, 'context'):
                raise RuntimeError("Invalid loader or context")

            profile = retry_with_backoff(
                instaloader.Profile.from_username,
                self.loader.context,
                username,
                max_retries=MAX_RETRIES,
                base_delay=RETRY_BASE_DELAY,
                max_delay=RETRY_MAX_DELAY,
                label=f"profile:{username}",
            )
            if profile is None:
                print(f"[ERROR] Could not load profile {username} after retries")
                return

            current_account_name = self.manager.current_account['name'] if self.manager.current_account else 'unknown'
            self.access_tracker.record_profile_access(username, current_account_name, {
                'can_access': True,
                'is_public': not profile.is_private,
                'is_followed': profile.followed_by_viewer if hasattr(profile, 'followed_by_viewer') else False,
            })

            self.metadata_manager.update_profile(username, profile, current_account_name)

            is_followed = profile.followed_by_viewer if hasattr(profile, 'followed_by_viewer') else False
            if profile.is_private and not is_followed:
                try:
                    profile = instaloader.Profile.from_username(self.loader.context, username)
                    is_followed = profile.followed_by_viewer if hasattr(profile, 'followed_by_viewer') else False
                except Exception:
                    pass

            if profile.is_private and not is_followed:
                print(f"[PRIVATE] Profile {username} is private and not followed by authenticated user")
                self.access_tracker.record_profile_access(username, current_account_name, {
                    'can_access': False,
                    'is_public': False,
                    'is_followed': False,
                    'error': 'Private profile not followed',
                })
                return

            # Add username to DB
            self._usr_repo.add_username(username, source_account=current_account_name)

            collected_count = 0
            batch: list[dict] = []
            new_rels: list[dict] = []
            new_usernames: list[str] = [username]
            # Flush batch to DB every N records so data is safe if process is killed
            FLUSH_EVERY = 25

            def _flush_batch():
                nonlocal collected_count, batch
                if batch:
                    new_rels.extend(batch)
                    collected_count += self._rel_repo.bulk_upsert(batch)
                    # Update account_access map for 'following' relationships
                    following_targets = [
                        r['target'] for r in batch if r.get('type') == 'following'
                    ]
                    if following_targets:
                        import time as _time
                        _now = _time.time()
                        try:
                            db = _get_db()
                            with db.get_connection() as conn:
                                conn.executemany(
                                    """INSERT INTO account_access (username, account_name, follows, last_checked_ts)
                                       VALUES (?, ?, 1, ?)
                                       ON CONFLICT(username, account_name) DO UPDATE SET
                                           follows=1, last_checked_ts=excluded.last_checked_ts""",
                                    [(t, current_account_name, _now) for t in following_targets],
                                )
                        except Exception:
                            pass
                    batch = []

            # Collect followers
            if max_followers > 0:
                # PHASE 1: Enforce session batch size limit
                effective_max_followers = min(max_followers, MAX_FOLLOWERS_PER_SESSION)
                if effective_max_followers < max_followers:
                    print(f"[LIMIT] Limiting followers to {effective_max_followers} per session (config: MAX_FOLLOWERS_PER_SESSION)")
                
                print(f"[SPIDER] Collecting followers for {username} (max: {effective_max_followers})")
                try:
                    followers_count = 0
                    for follower in profile.get_followers():
                        if _SHUTDOWN.is_set():
                            print("[STOPPED] Shutdown requested — stopping follower collection")
                            _flush_batch()
                            break
                        if followers_count >= effective_max_followers:
                            print(f"[LIMIT] Reached session limit of {effective_max_followers} followers")
                            break
                        follower_username = follower.username

                        # Apply filters
                        filter_reason = _apply_follower_filter(follower)
                        if filter_reason:
                            try:
                                db = _get_db()
                                db.execute(
                                    "INSERT OR IGNORE INTO usernames (username, source_account, spider_status, filter_reason)"
                                    " VALUES (?, ?, 'filtered', ?)",
                                    (follower_username, current_account_name, filter_reason),
                                )
                            except Exception:
                                pass
                            continue

                        self._usr_repo.add_username(follower_username, source_account=current_account_name)
                        new_usernames.append(follower_username)
                        # Save profile info for this follower — we already have the object, 0 extra API calls
                        try:
                            self.metadata_manager.update_profile(follower_username, follower, current_account_name)
                        except Exception:
                            pass
                        batch.append({
                            'source': username,
                            'target': follower_username,
                            'type': 'followers',
                            'collected_by': current_account_name,
                            'source_is_public': not profile.is_private,
                        })
                        followers_count += 1
                        # Per-item sleep — mimics human scroll speed, prevents machine-rate fingerprint
                        _interruptible_sleep(random.uniform(ENUM_ITEM_SLEEP_MIN, ENUM_ITEM_SLEEP_MAX))
                        # Flush to DB periodically so data survives a kill
                        if followers_count % FLUSH_EVERY == 0:
                            _flush_batch()
                            print(f"[💾 SAVED] {followers_count} followers → database (safe to Ctrl+C)", flush=True)
                        self.rate.periodic(followers_count, every=ENUM_PAUSE_EVERY, seconds=ENUM_PAUSE_SECONDS)
                    _flush_batch()  # flush remainder
                    print(f"[OK] Collected {followers_count} followers for {username}", flush=True)
                except instaloader.exceptions.PrivateProfileNotFollowedException:
                    _flush_batch()
                    print(f"[PRIVATE] Cannot access followers of private profile {username}")
                except Exception as e:
                    _flush_batch()
                    print(f"[ERROR] Error collecting followers for {username}: {e}")

            # Collect following
            if max_following > 0 and not _SHUTDOWN.is_set():
                # PHASE 1: Enforce session batch size limit
                effective_max_following = min(max_following, MAX_FOLLOWING_PER_SESSION)
                if effective_max_following < max_following:
                    print(f"[LIMIT] Limiting following to {effective_max_following} per session (config: MAX_FOLLOWING_PER_SESSION)")
                
                print(f"[SPIDER] Collecting following for {username} (max: {effective_max_following})")
                try:
                    following_count = 0
                    for followee in profile.get_followees():
                        if _SHUTDOWN.is_set():
                            print("[STOPPED] Shutdown requested — stopping following collection")
                            _flush_batch()
                            break
                        if following_count >= effective_max_following:
                            print(f"[LIMIT] Reached session limit of {effective_max_following} following")
                            break
                        followee_username = followee.username

                        # Apply filters
                        filter_reason = _apply_follower_filter(followee)
                        if filter_reason:
                            try:
                                db = _get_db()
                                db.execute(
                                    "INSERT OR IGNORE INTO usernames (username, source_account, spider_status, filter_reason)"
                                    " VALUES (?, ?, 'filtered', ?)",
                                    (followee_username, current_account_name, filter_reason),
                                )
                            except Exception:
                                pass
                            continue

                        self._usr_repo.add_username(followee_username, source_account=current_account_name)
                        new_usernames.append(followee_username)
                        # Save profile info for this followee — we already have the object, 0 extra API calls
                        try:
                            self.metadata_manager.update_profile(followee_username, followee, current_account_name)
                        except Exception:
                            pass
                        batch.append({
                            'source': username,
                            'target': followee_username,
                            'type': 'following',
                            'collected_by': current_account_name,
                            'source_is_public': not profile.is_private,
                        })
                        following_count += 1
                        _interruptible_sleep(random.uniform(ENUM_ITEM_SLEEP_MIN, ENUM_ITEM_SLEEP_MAX))
                        if following_count % FLUSH_EVERY == 0:
                            _flush_batch()
                            print(f"[💾 SAVED] {following_count} following → database (safe to Ctrl+C)", flush=True)
                        self.rate.periodic(following_count, every=ENUM_PAUSE_EVERY, seconds=ENUM_PAUSE_SECONDS)
                    _flush_batch()  # flush remainder
                    print(f"[OK] Collected {following_count} following for {username}", flush=True)
                except instaloader.exceptions.PrivateProfileNotFollowedException:
                    _flush_batch()
                    print(f"[PRIVATE] Cannot access following of private profile {username}")
                except Exception as e:
                    _flush_batch()
                    print(f"[ERROR] Error collecting following for {username}: {e}")

            print(f"[STATS] Total new relationships collected: {collected_count}")

            # Update in-memory caches with newly collected data
            if self._usernames_cache is None:
                self._usernames_cache = []
            for u in new_usernames:
                if u not in self._usernames_cache:
                    self._usernames_cache.append(u)

            existing_keys = {(r['source'], r['target'], r['type']) for r in (self._relationships_cache or [])}
            if self._relationships_cache is None:
                self._relationships_cache = []
            for r in new_rels:
                key = (r['source'], r['target'], r['type'])
                if key not in existing_keys:
                    self._relationships_cache.append(r)
                    existing_keys.add(key)

            # Write backward-compat files
            self._save_usernames()
            self._save_relationships()

        except instaloader.exceptions.ProfileNotExistsException:
            print(f"[ERROR] Profile {username} does not exist")
            current_account_name = self.manager.current_account['name'] if self.manager.current_account else 'unknown'
            self.access_tracker.record_profile_access(username, current_account_name, {
                'can_access': False,
                'error': 'Profile does not exist',
            })
        except Exception as e:
            print(f"[ERROR] Error collecting relationships for {username}: {e}")
            current_account_name = self.manager.current_account['name'] if self.manager.current_account else 'unknown'
            self.access_tracker.record_profile_access(username, current_account_name, {
                'can_access': False,
                'error': str(e),
            })

    def run_batch(self, max_users=None):
        """Process pending usernames in chunked batches (500 at a time).

        Never loads all usernames into memory at once (RULE 9).
        Checks _SHUTDOWN at the top of every chunk iteration.
        """
        CHUNK = 500
        processed_total = 0
        offset = 0

        while True:
            if _SHUTDOWN.is_set():
                print(f"[STOPPED] Shutdown requested after {processed_total} users processed.")
                break

            # Fetch next chunk from DB — only pending/not-yet-processed usernames
            db = _get_db()
            try:
                rows = db.fetchall(
                    """SELECT username FROM usernames
                       WHERE (spider_status IS NULL OR spider_status = 'pending')
                       ORDER BY added_ts ASC
                       LIMIT ? OFFSET ?""",
                    (CHUNK, offset),
                )
            except Exception:
                # spider_status column may not exist yet; fall back to simple query
                rows = db.fetchall(
                    "SELECT username FROM usernames ORDER BY added_ts ASC LIMIT ? OFFSET ?",
                    (CHUNK, offset),
                )

            if not rows:
                break

            chunk_usernames = [r['username'] for r in rows]
            del rows  # release memory

            for username in chunk_usernames:
                if _SHUTDOWN.is_set():
                    break
                if max_users and processed_total >= max_users:
                    break
                try:
                    print(f"\n[SPIDER] Processing {username}...")
                    self.collect_for_user(username)
                    processed_total += 1
                    import sys; sys.stdout.flush()
                    if not _SHUTDOWN.is_set():
                        self.rate.user_delay(multiplier=2)
                except Exception as e:
                    print(f"[ERROR] Failed to process {username}: {e}")
                    continue

            if max_users and processed_total >= max_users:
                break
            if len(chunk_usernames) < CHUNK:
                break  # last chunk — done
            offset += CHUNK

        if processed_total == 0:
            print("[OK] All usernames have been processed")
        else:
            print(f"[OK] Batch processing complete — {processed_total} users processed")


