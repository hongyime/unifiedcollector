"""Contribution spider — repos, co-contributors, fork edges."""
import asyncio
from typing import Optional, List
import aiosqlite

from src.config import Config
from src.github_client import GitHubAPIClient
from src.database import (
    upsert_user, upsert_repository, add_contribution_edge,
    add_user_if_not_exists
)


class ContributionSpider:
    """Fetches repos and co-contributor relationships for known users."""

    def __init__(self, db_path=Config.DB_PATH, pats: Optional[List[str]] = None):
        self.db_path = db_path
        self._pats = pats or []
        self.repos_added = 0
        self.edges_added = 0
        self.users_added = 0

    def _client(self):
        return GitHubAPIClient(pats=self._pats)

    async def spider_user_repos(self, username: str):
        """Fetch all repos for a user and add co-contributor edges."""
        print(f"📦 Spidering repos for: {username}")
        async with self._client() as client:
            async with aiosqlite.connect(self.db_path, timeout=30) as db:
                repos = await client.get_all_user_repos(username)
                print(f"   {len(repos)} repos found")

                for repo in repos:
                    await upsert_repository(db, repo)
                    self.repos_added += 1

                    if repo.get('fork'):
                        # Forked repo — add edge to original
                        parent = repo.get('parent', {})
                        if parent:
                            parent_owner = parent.get('owner', {}).get('login')
                            if parent_owner and parent_owner != username:
                                await add_user_if_not_exists(db, parent_owner)
                                await add_contribution_edge(
                                    db, username, parent_owner, 'forked',
                                    repo.get('full_name'))
                                self.edges_added += 1

                    # Co-contributors
                    owner = repo.get('owner', {}).get('login', username)
                    repo_name = repo.get('name', '')
                    contributors = await client.get_repo_contributors(owner, repo_name)
                    await asyncio.sleep(Config.API_REQUEST_DELAY)

                    for contrib in contributors:
                        login = contrib.get('login')
                        if login and login != username:
                            await add_user_if_not_exists(db, login)
                            # Bidirectional co_contributor edges
                            await add_contribution_edge(
                                db, username, login, 'co_contributor',
                                repo.get('full_name'))
                            await add_contribution_edge(
                                db, login, username, 'co_contributor',
                                repo.get('full_name'))
                            self.edges_added += 2

                await db.commit()

        print(f"   ✅ repos={self.repos_added} edges={self.edges_added}")

    async def spider_all_users_repos(self, batch_size: int = 20):
        """Spider repos for all users with spider_status='completed'."""
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            cursor = await db.execute("""
                SELECT username FROM users
                WHERE spider_status='completed' AND user_id IS NOT NULL
                ORDER BY followers_count DESC
                LIMIT ?
            """, (batch_size,))
            users = [r[0] for r in await cursor.fetchall()]

        print(f"📦 Spidering repos for {len(users)} users...")
        for i, username in enumerate(users):
            await self.spider_user_repos(username)
            if (i + 1) % 10 == 0:
                print(f"   Progress: {i+1}/{len(users)}")

        print(f"✅ Done: repos={self.repos_added} edges={self.edges_added}")

    async def spider_repo_contributors(self, owner: str, repo_name: str):
        """Add co-contributor edges for all contributors on one repo."""
        full_name = f"{owner}/{repo_name}"
        print(f"👥 Fetching contributors for: {full_name}")
        async with self._client() as client:
            contributors = await client.get_repo_contributors(owner, repo_name)

        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            logins = []
            for c in contributors:
                login = c.get('login')
                if login:
                    await add_user_if_not_exists(db, login)
                    logins.append(login)

            # All pairs — O(n²) edges
            for i, a in enumerate(logins):
                for b in logins[i+1:]:
                    await add_contribution_edge(db, a, b, 'co_contributor', full_name)
                    await add_contribution_edge(db, b, a, 'co_contributor', full_name)
                    self.edges_added += 2

            await db.commit()

        print(f"   ✅ {len(logins)} contributors, {self.edges_added} edges added")
