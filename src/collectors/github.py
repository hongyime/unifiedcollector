import asyncio
import hashlib
import json
import logging
import os
import tempfile
from collections import deque
from pathlib import Path
from datetime import datetime, timezone

import httpx

from src.core.base_collector import BaseCollector
from src.core.profile_photo_tracker import ProfilePhotoTracker
from src.core.file_naming import sanitize_name

logger = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
PER_PAGE = 100


class GithubCollector(BaseCollector):
    SOURCE_NAME = "github"

    def __init__(self):
        super().__init__()
        self._pats = self._load_pats()
        self._pat_idx = 0
        self._sem = asyncio.Semaphore(int(os.getenv("GITHUB_MAX_CONCURRENT", "5")))
        self._batch_sem = asyncio.Semaphore(10)
        self._spider_depth = int(os.getenv("GITHUB_SPIDER_DEPTH", "4"))
        self._spider_batch_size = int(os.getenv("GITHUB_SPIDER_BATCH_SIZE", "20"))
        self._spider_user_delay = float(os.getenv("GITHUB_SPIDER_USER_DELAY", "2.0"))
        self._api_delay = float(os.getenv("GITHUB_API_DELAY", "0.1"))
        self._download_delay = float(os.getenv("GITHUB_DOWNLOAD_DELAY", "0.5"))
        self._avatar_size = int(os.getenv("GITHUB_AVATAR_SIZE", "460"))
        self._photo_tracker = ProfilePhotoTracker(
            blob_max_size_mb=int(os.getenv("GITHUB_PROFILE_PHOTO_BLOB_MAX_SIZE_MB", "5000"))
        )
        self._blob_enabled = os.getenv("GITHUB_PROFILE_PHOTO_BLOB_ENABLED", "false").lower() == "true"
        self._spider_visited: set[str] = set()
        self._db_avatar_ids: set[int] = set()

    def set_pool(self, pool):
        super().set_pool(pool)
        self._photo_tracker.set_pool(pool)

    def _load_pats(self) -> list[str]:
        raw = os.getenv("GITHUB_TOKEN", "")
        return [t.strip() for t in raw.split(",") if t.strip()]

    def _headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": self.user_agents.get_for_domain("github.com"),
        }
        if self._pats:
            h["Authorization"] = f"token {self._pats[self._pat_idx]}"
        return h

    def _rotate_pat(self):
        if len(self._pats) > 1:
            self._pat_idx = (self._pat_idx + 1) % len(self._pats)
            logger.info("Rotated to PAT index %d", self._pat_idx)

    @property
    def account_media_dir(self) -> Path:
        # isolation by PAT index
        path = self.media_dir / f"token_{self._pat_idx + 1}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def _api_get(self, client: httpx.AsyncClient, url: str) -> httpx.Response | None:
        async with self._sem:
            await asyncio.sleep(self._api_delay)
            resp = await client.get(url, headers=self._headers())
            remaining = int(resp.headers.get("X-RateLimit-Remaining", "999"))
            if remaining < 10 and self._pats: self._rotate_pat()
            if resp.status_code == 403 and remaining == 0:
                reset_at = int(resp.headers.get("X-RateLimit-Reset", "0"))
                import time
                wait = max(0, reset_at - int(time.time())) + 5
                logger.warning("GitHub rate limit hit, waiting %ds", wait)
                await asyncio.sleep(min(wait, 300))
                self._rotate_pat()
                return None
            if resp.status_code == 404: return None
            resp.raise_for_status()
            return resp

    async def _paginate(self, client: httpx.AsyncClient, url: str, max_items: int | None = None) -> list[dict]:
        results = []
        page = 1
        while True:
            if self._stop.is_set(): break
            if max_items is not None and len(results) >= max_items: break
            sep = "&" if "?" in url else "?"
            page_url = f"{url}{sep}per_page={PER_PAGE}&page={page}"
            resp = await self._api_get(client, page_url)
            if resp is None: break
            batch = resp.json()
            if not batch: break
            results.extend(batch)
            if len(batch) < PER_PAGE: break
            page += 1
        return results[:max_items] if max_items is not None else results

    async def collect(self, targets: list[str]):
        # Parallel target iteration via per-target semaphore.
        # GITHUB_TARGET_CONCURRENCY controls how many users/repos are processed simultaneously.
        # Defaults to 4 (heavy users have ~150 repos each; 4 in flight = ~4x throughput without
        # blowing the 5000/hr API budget — _api_get's _sem still rate-limits raw calls).
        target_concurrency = int(os.getenv("GITHUB_TARGET_CONCURRENCY", "4"))
        target_sem = asyncio.Semaphore(target_concurrency)

        async def _process_one(client: httpx.AsyncClient, target: str):
            async with target_sem:
                if self._stop.is_set():
                    return
                logger.info("Collecting github/%s", target)
                try:
                    if "/" in target:
                        await self._collect_repo(client, target)
                    else:
                        await self._collect_user(client, target)
                        if self._spider_depth > 0:
                            await self._spider_social_graph(client, target)
                except Exception as e:
                    logger.error("Failed github/%s: %s", target, e, exc_info=True)
                    await self.send_to_dlq(target, target, str(e))

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            await asyncio.gather(*[_process_one(client, t) for t in targets], return_exceptions=True)

        if os.getenv("GITHUB_SPIDER_ENABLED", "true").lower() == "true":
            await self._process_spider_queue()

    async def _process_spider_queue(self):
        """Drain N pending entries from github_spider_queue per tick.

        Pops up to ``_spider_batch_size`` rows ordered by priority (depth) ASC,
        marks them ``processing``, fetches the user (and their repos/commits via
        the existing _collect_user logic), enqueues their direct followers/following
        at priority+1 if still under ``_spider_depth``, then marks ``done``/``failed``.

        Sleeps ``_spider_user_delay`` seconds between users to be respectful of
        GitHub's 5000/hr authenticated rate limit.
        """
        # Parallelized drain. GITHUB_SPIDER_CONCURRENCY workers race on the queue
        # (FOR UPDATE SKIP LOCKED ensures no double-processing). Each worker pops
        # one row, processes it, marks done/failed, sleeps _spider_user_delay, repeats.
        # Total per-tick budget: _spider_batch_size rows divided across workers.
        spider_concurrency = int(os.getenv("GITHUB_SPIDER_CONCURRENCY", "4"))
        processed_counter = {"n": 0}

        async def _drain_worker(worker_id: int, client: httpx.AsyncClient):
            while processed_counter["n"] < self._spider_batch_size and not self._stop.is_set():
                async with self.pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        UPDATE github_spider_queue
                        SET status = 'processing'
                        WHERE id = (
                            SELECT id FROM github_spider_queue
                            WHERE status = 'pending' AND priority <= $1
                            ORDER BY priority ASC, collected_at ASC
                            FOR UPDATE SKIP LOCKED
                            LIMIT 1
                        )
                        RETURNING id, target_type, target_identifier, priority
                        """,
                        self._spider_depth,
                    )
                if not row:
                    logger.info("Spider drain worker=%d: queue empty or depth-exhausted", worker_id)
                    return

                # Reserve a slot in the per-tick budget atomically.
                processed_counter["n"] += 1
                slot = processed_counter["n"]

                qid = row["id"]
                ttype = row["target_type"]
                tid = row["target_identifier"]
                depth = row["priority"] or 1
                logger.info("Spider drain w=%d: processing %s/%s (depth=%d, %d/%d)",
                            worker_id, ttype, tid, depth, slot, self._spider_batch_size)
                try:
                    if ttype == "user":
                        await self._collect_user(client, tid)
                        if depth < self._spider_depth:
                            await self._enqueue_neighbors(client, tid, depth + 1)
                    else:
                        await self._collect_repo(client, tid)
                    async with self.pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE github_spider_queue SET status='done' WHERE id=$1",
                            qid,
                        )
                except Exception as e:
                    logger.warning("Spider drain w=%d failed for %s/%s: %s", worker_id, ttype, tid, e)
                    async with self.pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE github_spider_queue SET status='failed' WHERE id=$1",
                            qid,
                        )
                await asyncio.sleep(self._spider_user_delay)

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            await asyncio.gather(
                *[_drain_worker(i, client) for i in range(spider_concurrency)],
                return_exceptions=True,
            )
            logger.info("Spider drain tick complete: processed=%d (workers=%d)",
                        processed_counter["n"], spider_concurrency)

    async def _enqueue_neighbors(self, client: httpx.AsyncClient, username: str, depth: int):
        """Fetch followers + following of ``username`` and enqueue them at the given depth.

        depth here is encoded into the queue's ``priority`` column (lower = nearer to seed).
        """
        if depth > self._spider_depth:
            return
        added = 0
        for endpoint in ("followers", "following"):
            users = await self._paginate(client, f"{API_BASE}/users/{username}/{endpoint}")
            for u in users:
                login = u.get("login", "")
                if not login:
                    continue
                try:
                    async with self.pool.acquire() as conn:
                        result = await conn.execute(
                            "INSERT INTO github_spider_queue (target_type, target_identifier, status, priority, collected_at) "
                            "VALUES ('user', $1, 'pending', $2, NOW()) "
                            "ON CONFLICT (target_type, target_identifier) DO NOTHING",
                            login,
                            depth,
                        )
                        if result and result.endswith(" 1"):
                            added += 1
                except Exception as e:
                    logger.debug("spider neighbor enqueue failed for %s: %s", login, e)
        logger.info("Spider neighbors of %s: enqueued %d new at depth %d", username, added, depth)

    async def _collect_user(self, client: httpx.AsyncClient, username: str):
        resp = await self._api_get(client, f"{API_BASE}/users/{username}")
        if resp is None: return
        user = resp.json()
        uid = str(user["id"])
        login = user["login"]

        await self._upsert_user(user)

        if user.get("avatar_url"):
            avatar_url = user["avatar_url"]
            if self._avatar_size:
                sep = "&" if "?" in avatar_url else "?"
                avatar_url = f"{avatar_url}{sep}s={self._avatar_size}"
            
            dest_dir = self.account_media_dir / "profiles"
            dest_dir.mkdir(parents=True, exist_ok=True)
            changed, path = await self._photo_tracker.check_and_download(avatar_url, uid, "github", dest_dir)
            if changed and path:
                await self.insert_media_item(
                    entity_id=uid, entity_name=login, content_type="profile_photo", content_id=f"avatar_{uid}",
                    filename=path.name, file_path=str(path), file_size=path.stat().st_size,
                    sha256=self.sha256_bytes(path.read_bytes()), metadata={"raw": user}
                )

        repos = await self._paginate(client, f"{API_BASE}/users/{username}/repos")
        for repo in repos:
            if self._stop.is_set(): break
            await self._upsert_repo(repo)
            await self._collect_repo_content(client, repo["full_name"], uid, login)

        await self.checkpoint.save_progress(username)

    async def _collect_repo(self, client: httpx.AsyncClient, full_name: str):
        resp = await self._api_get(client, f"{API_BASE}/repos/{full_name}")
        if resp is None: return
        repo = resp.json()
        await self._upsert_repo(repo)
        await self._collect_repo_content(client, full_name, str(repo["owner"]["id"]), repo["owner"]["login"])
        await self.checkpoint.save_progress(full_name)

    async def _collect_repo_content(self, client: httpx.AsyncClient, full_name: str, uid: str, login: str):
        max_commits = int(os.getenv("GITHUB_MAX_COMMITS_PER_REPO", "200"))
        max_issues = int(os.getenv("GITHUB_MAX_ISSUES_PER_REPO", "100"))
        max_contributors = int(os.getenv("GITHUB_MAX_CONTRIBUTORS_PER_REPO", "25"))

        # 1. README
        readme_resp = await self._api_get(client, f"{API_BASE}/repos/{full_name}/readme")
        if readme_resp:
            readme = readme_resp.json()
            import base64
            content = base64.b64decode(readme.get("content", "")).decode("utf-8", "ignore")
            await self._upsert_readme(readme.get("repository_id") or 0, content, readme.get("sha"), readme.get("size"))

        # 2. Commits (capped)
        commits = await self._paginate(client, f"{API_BASE}/repos/{full_name}/commits", max_items=max_commits)
        for c in commits:
            await self._upsert_commit(0, c)

        # 3. Issues (capped)
        issues = await self._paginate(client, f"{API_BASE}/repos/{full_name}/issues", max_items=max_issues)
        for i in issues:
            await self._upsert_issue(0, i)

        # 4. Releases/Assets
        releases = await self._paginate(client, f"{API_BASE}/repos/{full_name}/releases")
        for release in releases:
            for asset in release.get("assets", []):
                if self.is_known(str(asset["id"])): continue
                await self.download_media({
                    "entity_id": uid, "entity_name": login, "content_type": "release", "content_id": str(asset["id"]),
                    "url": asset["browser_download_url"], "extension": Path(asset["name"]).suffix.lstrip(".") or "bin",
                    "source_url": asset["browser_download_url"], "raw": asset
                })

        # 5. Contributors → spider queue (the ACTUAL community of a repo, distinct from owner's followers)
        if max_contributors > 0 and self._spider_depth > 0:
            try:
                contributors = await self._paginate(client, f"{API_BASE}/repos/{full_name}/contributors", max_items=max_contributors)
                added = 0
                for c in contributors:
                    contrib_login = (c or {}).get("login")
                    if not contrib_login or (c or {}).get("type") == "Bot":
                        continue
                    try:
                        async with self.pool.acquire() as conn:
                            result = await conn.execute(
                                "INSERT INTO github_spider_queue (target_type, target_identifier, status, priority, collected_at) "
                                "VALUES ('user', $1, 'pending', $2, NOW()) "
                                "ON CONFLICT (target_type, target_identifier) DO NOTHING",
                                contrib_login, 2,
                            )
                            if result and result.endswith(" 1"):
                                added += 1
                    except Exception as e:
                        logger.debug("contributor enqueue failed for %s: %s", contrib_login, e)
                if added:
                    logger.info("Spider contributors of %s: enqueued %d new", full_name, added)
            except Exception as e:
                logger.debug("contributor fetch failed for %s: %s", full_name, e)

    async def _upsert_user(self, user_data: dict):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO github_users (
                    platform_user_id, login, name, company, blog, location,
                    email, bio, public_repos_count, followers_count,
                    following_count, platform_created_at, collected_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NOW())
                ON CONFLICT (platform_user_id) DO UPDATE SET
                    login = EXCLUDED.login, name = EXCLUDED.name, bio = EXCLUDED.bio,
                    public_repos_count = EXCLUDED.public_repos_count, collected_at = NOW()
            """, user_data.get("id"), user_data.get("login"), user_data.get("name"), user_data.get("company"), user_data.get("blog"), user_data.get("location"), user_data.get("email"), user_data.get("bio"), user_data.get("public_repos"), user_data.get("followers"), user_data.get("following"), datetime.fromisoformat(user_data.get("created_at").replace("Z", "")) if user_data.get("created_at") else None)

    async def _upsert_repo(self, repo_data: dict):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO github_repos (
                    platform_repo_id, name, full_name, description, homepage,
                    language, stargazers_count, watchers_count, forks_count,
                    open_issues_count, topics, license, platform_created_at,
                    platform_updated_at, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                ON CONFLICT (platform_repo_id) DO UPDATE SET
                    stargazers_count = EXCLUDED.stargazers_count, forks_count = EXCLUDED.forks_count,
                    platform_updated_at = EXCLUDED.platform_updated_at, metadata = EXCLUDED.metadata
            """, repo_data.get("id"), repo_data.get("name"), repo_data.get("full_name"), repo_data.get("description"), repo_data.get("homepage"), repo_data.get("language"), repo_data.get("stargazers_count"), repo_data.get("watchers_count"), repo_data.get("forks_count"), repo_data.get("open_issues_count"), repo_data.get("topics"), repo_data.get("license", {}).get("name") if repo_data.get("license") else None, datetime.fromisoformat(repo_data.get("created_at").replace("Z", "")) if repo_data.get("created_at") else None, datetime.fromisoformat(repo_data.get("updated_at").replace("Z", "")) if repo_data.get("updated_at") else None, json.dumps(repo_data, default=str))

    async def _upsert_readme(self, repo_id: int, content: str, sha: str, size: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id FROM github_repos WHERE platform_repo_id = $1", repo_id)
            if row: await conn.execute("INSERT INTO github_readmes (repo_id, content, sha, size, collected_at) VALUES ($1, $2, $3, $4, NOW())", row['id'], content, sha, size)

    async def _upsert_commit(self, repo_id: int, commit: dict):
        # Hardened: GitHub returns null for "commit", "commit.author", and top-level "author"
        # when commits are imported, signed without GH account, or authored by deleted users.
        # dict.get(k, default) returns the *value* when key exists, even if value is None,
        # so we must coalesce None → {} explicitly.
        c = commit.get("commit") or {}
        author = c.get("author") or {}
        gh_author = commit.get("author") or {}
        date_str = author.get("date")
        commit_date = None
        if date_str:
            try:
                commit_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                commit_date = None
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO github_commits (sha, author_name, author_email, author_login, message, date, collected_at)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                ON CONFLICT (sha) DO NOTHING
            """, commit.get("sha"), author.get("name"), author.get("email"), gh_author.get("login"), c.get("message"), commit_date)

    async def _upsert_issue(self, repo_id: int, issue: dict):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO github_issues (platform_issue_id, number, title, body, state, is_pull_request, labels, comments_count, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (platform_issue_id) DO UPDATE SET state = EXCLUDED.state, updated_at = EXCLUDED.updated_at
            """, issue.get("id"), issue.get("number"), issue.get("title"), issue.get("body"), issue.get("state"), "pull_request" in issue, [l.get("name") for l in issue.get("labels", [])], issue.get("comments"), datetime.fromisoformat(issue.get("created_at").replace("Z", "")) if issue.get("created_at") else None, datetime.fromisoformat(issue.get("updated_at").replace("Z", "")) if issue.get("updated_at") else None)

    async def download_media(self, item: dict):
        cid = item["content_id"]
        if self.is_known(cid): return
        filename = self.build_filename(item["entity_id"], item["entity_name"], item["content_type"], cid, extension=item.get("extension", "jpg"))
        dest_dir = self.account_media_dir / item["content_type"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        try:
            await asyncio.sleep(self._download_delay)
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(item["url"])
                resp.raise_for_status()
                data = resp.content
            sha = self.sha256_bytes(data)
            fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(data); f.flush(); os.fsync(f.fileno())
            os.replace(tmp_path, dest)
            metadata = {"entity_id": item["entity_id"], "entity_name": item["entity_name"], "content_type": item["content_type"], "content_id": cid, "collected_at": datetime.now(timezone.utc).isoformat(), "raw": item.get("raw", {})}
            self.save_json(metadata, dest_dir / f"{Path(filename).stem}_metadata.json")
            await self.insert_media_item(entity_id=item["entity_id"], entity_name=item["entity_name"], content_type=item["content_type"], content_id=cid, filename=filename, file_path=str(dest), file_size=len(data), sha256=sha, metadata=metadata)
            self._known_ids.add(cid)
        except Exception as e:
            logger.error("Download failed %s: %s", cid, e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))

    async def _spider_social_graph(self, client: httpx.AsyncClient, seed_username: str):
        """Seed the spider queue with the seed user's direct followers/following.

        Deeper traversal is performed by ``_process_spider_queue`` which pops
        users from the queue in batches and enqueues *their* neighbors at depth+1.
        This avoids unbounded in-memory BFS that previously starved the queue worker.
        """
        await self._enqueue_neighbors(client, seed_username, depth=1)

    async def cleanup(self):
        pass
