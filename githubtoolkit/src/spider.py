"""Social graph spider - collects followers/following relationships."""
import asyncio
from typing import Optional
import aiosqlite

from src.config import Config
from src.github_client import GitHubAPIClient
from src.profile_photo_tracker import ProfilePhotoTracker
from src.database import (
    upsert_user, add_user_if_not_exists, get_pending_spider_users,
    update_spider_status, add_edge
)

_DB_TIMEOUT = 30  # seconds to wait on SQLite write lock before failing


class SocialGraphSpider:
    """Spiders GitHub social graph starting from seed user."""

    def __init__(self, db_path, pat: Optional[str] = None, pats=None):
        self.db_path = db_path
        self._pats = list(pats) if pats else ([pat] if pat else [])
        self.pat = self._pats[0] if self._pats else None
        self.max_users = Config.GITHUB_MAX_USERS
        self.users_discovered = 0
        self.users_spidered = 0
        self.edges_created = 0
        self.users_skipped = 0
        self._stop_requested = False

    def _client(self):
        return GitHubAPIClient(pats=self._pats)

    def _db(self):
        """Open a DB connection with busy-wait timeout."""
        return aiosqlite.connect(self.db_path, timeout=_DB_TIMEOUT)

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------

    async def seed_from_user(self, username: str):
        """Seed spider from a GitHub username."""
        print(f"🌱 Seeding spider from: {username}")
        async with self._client() as client:
            async with self._db() as db:
                user_data = await client.get_user(username)
                if not user_data:
                    print(f"❌ User not found: {username}")
                    return
                await upsert_user(db, user_data)
                await db.execute(
                    "UPDATE users SET spider_status='pending', hop_count=0 WHERE username=?",
                    (username,))
                await db.commit()
                print(f"✅ Seed user: {username}  "
                      f"followers={user_data.get('followers',0):,}  "
                      f"following={user_data.get('following',0):,}")

    async def seed_from_id(self, user_id: int):
        """Seed spider from a GitHub numeric user ID."""
        print(f"🌱 Resolving user ID: {user_id}")
        async with self._client() as client:
            user_data = await client.get_user_by_id(user_id)
            if not user_data:
                print(f"❌ User ID not found: {user_id}")
                return
        username = user_data.get('login')
        print(f"   Resolved to: {username}")
        await self.seed_from_user(username)

    async def seed_from_self(self):
        """Seed spider from the authenticated user's own account."""
        if not self.pat:
            print("❌ PAT required for self-seed")
            return
        print("🌱 Seeding from authenticated account...")
        async with self._client() as client:
            user_data = await client.get_authenticated_user()
            if not user_data:
                print("❌ Could not fetch authenticated user — check PAT scopes")
                return
            username = user_data.get('login')
            print(f"   Authenticated as: {username}")

            async with self._db() as db:
                await upsert_user(db, user_data)
                await db.execute(
                    "UPDATE users SET spider_status='pending', hop_count=0 WHERE username=?",
                    (username,))

                print("   Fetching own followers...")
                followers = await client.get_all_followers(username)
                for f in followers:
                    login = f.get('login')
                    if login:
                        await add_user_if_not_exists(db, login, hop_count=1)
                        await add_edge(db, login, username, 'follows')
                        self.edges_created += 1

                print("   Fetching own following...")
                following = await client.get_all_following(username)
                for f in following:
                    login = f.get('login')
                    if login:
                        await add_user_if_not_exists(db, login, hop_count=1)
                        await add_edge(db, username, login, 'follows')
                        self.edges_created += 1

                await db.commit()

            print(f"✅ Self-seed done: {len(followers):,} followers, {len(following):,} following queued")

    # ------------------------------------------------------------------
    # Spidering
    # ------------------------------------------------------------------

    async def spider_batch(self, batch_size: int = 10):
        """Spider a batch of pending users. Commits after each user."""
        async with self._client() as client:
            async with ProfilePhotoTracker(self.db_path) as tracker:
                async with self._db() as db:
                    pending = await get_pending_spider_users(db, limit=batch_size)
                    if not pending:
                        print("✅ No pending users to spider")
                        return

                    print(f"🕷️  Spidering {len(pending)} users...")
                    for username in pending:
                        if self._stop_requested:
                            break

                        cursor = await db.execute("SELECT COUNT(*) FROM users")
                        if (await cursor.fetchone())[0] >= self.max_users:
                            print(f"⚠️  Max users limit reached ({self.max_users:,})")
                            self._stop_requested = True
                            break

                        await self._spider_user(client, db, username, tracker)
                        # Commit per-user: releases write lock so ProfilePhotoTracker
                        # can open its own connection without hitting SQLITE_BUSY.
                        await db.commit()
                        self.users_spidered += 1

                    print(f"✅ Batch: spidered={self.users_spidered} "
                          f"discovered={self.users_discovered} "
                          f"edges={self.edges_created} "
                          f"skipped={self.users_skipped}")

    async def _spider_user(self, client: GitHubAPIClient, db: aiosqlite.Connection,
                           username: str, tracker: Optional[ProfilePhotoTracker] = None):
        """Spider one user's followers and following."""
        print(f"   Spidering: {username}")
        await update_spider_status(db, username, 'in_progress')

        cursor = await db.execute("SELECT hop_count FROM users WHERE username=?", (username,))
        row = await cursor.fetchone()
        current_hop = row[0] if row else 0
        next_hop = current_hop + 1

        if next_hop > Config.DEFAULT_SPIDER_DEPTH:
            await update_spider_status(db, username, 'completed')
            return

        try:
            user_data = await client.get_user(username)
            if not user_data:
                await update_spider_status(db, username, 'completed')
                return

            await upsert_user(db, user_data)

            # Commit the upsert before tracker opens its own DB connection.
            # Without this, the open write transaction holds the lock and
            # track_photo_change() fails with "database is locked".
            await db.commit()

            if tracker and user_data.get('avatar_url') and user_data.get('id'):
                await tracker.track_photo_change(
                    username, user_data['id'], user_data['avatar_url'])

            followers = await client.get_all_followers(username)
            for f in followers:
                login = f.get('login')
                if login:
                    if await add_user_if_not_exists(db, login, next_hop):
                        self.users_discovered += 1
                    await add_edge(db, login, username, 'follows')
                    self.edges_created += 1

            following = await client.get_all_following(username)
            for f in following:
                login = f.get('login')
                if login:
                    if await add_user_if_not_exists(db, login, next_hop):
                        self.users_discovered += 1
                    await add_edge(db, username, login, 'follows')
                    self.edges_created += 1

            await update_spider_status(db, username, 'completed')
            print(f"      ✓ followers={len(followers)} following={len(following)}")

        except asyncio.CancelledError:
            # Ctrl+C — mark back to pending so restart can resume
            await update_spider_status(db, username, 'pending')
            await db.commit()
            raise
        except Exception as e:
            print(f"      ✗ Error spidering {username}: {e}")
            await update_spider_status(db, username, 'pending')

    async def spider_all(self):
        """Spider all pending users until queue empty or limit reached."""
        print(f"🕷️  Unlimited spider (max {self.max_users:,} users)")
        print("   Press Ctrl+C to stop cleanly.\n")
        try:
            while not self._stop_requested:
                async with self._db() as db:
                    pending = await get_pending_spider_users(db, limit=1)
                    if not pending:
                        print("✅ Spider queue empty!")
                        break
                    cursor = await db.execute("SELECT COUNT(*) FROM users")
                    if (await cursor.fetchone())[0] >= self.max_users:
                        print(f"⚠️  Max users limit reached ({self.max_users:,})")
                        break
                await self.spider_batch(batch_size=10)

        except (KeyboardInterrupt, asyncio.CancelledError):
            print(f"\n⚠️  Spider stopped by user. Progress is saved — restart to resume.")

        print(f"🏁 Spider done: spidered={self.users_spidered} "
              f"discovered={self.users_discovered} edges={self.edges_created}")
