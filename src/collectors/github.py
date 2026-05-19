import asyncio
import hashlib
import json
import logging
import os
from collections import deque
from pathlib import Path

import httpx

from src.core.base_collector import BaseCollector
from src.core.profile_photo_tracker import ProfilePhotoTracker

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

    async def _api_get(self, client: httpx.AsyncClient, url: str) -> httpx.Response | None:
        async with self._sem:
            await asyncio.sleep(self._api_delay)
            resp = await client.get(url, headers=self._headers())

            remaining = int(resp.headers.get("X-RateLimit-Remaining", "999"))
            if remaining < 10 and self._pats:
                self._rotate_pat()

            if resp.status_code == 403 and remaining == 0:
                reset_at = int(resp.headers.get("X-RateLimit-Reset", "0"))
                import time
                wait = max(0, reset_at - int(time.time())) + 5
                logger.warning("GitHub rate limit hit, waiting %ds", wait)
                await asyncio.sleep(min(wait, 300))
                self._rotate_pat()
                return None

            if resp.status_code == 404:
                return None

            resp.raise_for_status()
            return resp

    async def _paginate(self, client: httpx.AsyncClient, url: str) -> list[dict]:
        results = []
        page = 1
        while True:
            if self._stop.is_set():
                break
            sep = "&" if "?" in url else "?"
            page_url = f"{url}{sep}per_page={PER_PAGE}&page={page}"
            resp = await self._api_get(client, page_url)
            if resp is None:
                break
            batch = resp.json()
            if not batch:
                break
            results.extend(batch)
            if len(batch) < PER_PAGE:
                break
            page += 1
        return results

    async def collect(self, targets: list[str]):
        async with httpx.AsyncClient(timeout=30) as client:
            for target in targets:
                if self._stop.is_set():
                    break
                logger.info("Collecting github/%s", target)
                try:
                    if "/" in target:
                        await self._collect_repo(client, target)
                    else:
                        await self._collect_user(client, target)
                        if self._spider_depth > 0:
                            await self._spider_social_graph(client, target)
                except Exception as e:
                    logger.error("Failed github/%s: %s", target, e)
                    await self.send_to_dlq(target, target, str(e))

    async def _collect_user(self, client: httpx.AsyncClient, username: str):
        resp = await self._api_get(client, f"{API_BASE}/users/{username}")
        if resp is None:
            return
        user = resp.json()
        uid = str(user["id"])
        login = user["login"]

        if user.get("avatar_url"):
            avatar_url = user["avatar_url"]
            if self._avatar_size:
                sep = "&" if "?" in avatar_url else "?"
                avatar_url = f"{avatar_url}{sep}s={self._avatar_size}"

            changed, path = await self._photo_tracker.check_and_download(
                avatar_url, uid, "github", self.media_dir / "profiles",
            )
            if changed and path:
                data = path.read_bytes()
                await self.insert_media_item(
                    entity_id=uid,
                    entity_name=login,
                    content_type="profile_photo",
                    content_id=f"avatar_{uid}",
                    filename=path.name,
                    file_path=str(path),
                    file_size=len(data),
                    sha256=self.sha256_bytes(data),
                )
            elif not changed:
                cid = f"avatar_{uid}"
                if not self.is_known(cid):
                    await self.download_media({
                        "entity_id": uid,
                        "entity_name": login,
                        "content_type": "avatar",
                        "content_id": cid,
                        "url": avatar_url,
                        "extension": "jpg",
                    })

        repos = await self._paginate(client, f"{API_BASE}/users/{username}/repos")
        for repo in repos:
            if self._stop.is_set():
                break
            await self._collect_repo_releases(client, repo["full_name"], uid, login)

        await self.checkpoint.save_progress(username)

    async def _spider_social_graph(self, client: httpx.AsyncClient, seed_username: str):
        """BFS spider across followers/following to discover users."""
        queue: deque[tuple[str, int]] = deque([(seed_username, 0)])
        self._spider_visited.add(seed_username.lower())

        while queue and not self._stop.is_set():
            username, depth = queue.popleft()
            if depth >= self._spider_depth:
                continue

            for endpoint in ("followers", "following"):
                if self._stop.is_set():
                    break
                users = await self._paginate(
                    client, f"{API_BASE}/users/{username}/{endpoint}"
                )
                for u in users:
                    if self._stop.is_set():
                        break
                    login = u.get("login", "")
                    if login.lower() in self._spider_visited:
                        continue
                    self._spider_visited.add(login.lower())

                    uid = str(u.get("id", ""))
                    avatar_url = u.get("avatar_url", "")

                    edge_type = "follows" if endpoint == "followers" else "following"
                    await self._persist_edge(username, login, edge_type)

                    if avatar_url and uid:
                        cid = f"avatar_{uid}"
                        if not self.is_known(cid):
                            if self._avatar_size:
                                sep = "&" if "?" in avatar_url else "?"
                                avatar_url = f"{avatar_url}{sep}s={self._avatar_size}"
                            await self.download_media({
                                "entity_id": uid,
                                "entity_name": login,
                                "content_type": "avatar",
                                "content_id": cid,
                                "url": avatar_url,
                                "extension": "jpg",
                            })

                    if depth + 1 < self._spider_depth:
                        queue.append((login, depth + 1))

            logger.info("Spider depth %d: visited %d users", depth, len(self._spider_visited))

    async def _collect_repo(self, client: httpx.AsyncClient, full_name: str):
        resp = await self._api_get(client, f"{API_BASE}/repos/{full_name}")
        if resp is None:
            return
        repo = resp.json()
        owner = repo["owner"]
        uid = str(owner["id"])

        if owner.get("avatar_url"):
            cid = f"avatar_{uid}"
            if not self.is_known(cid):
                await self.download_media({
                    "entity_id": uid,
                    "entity_name": owner["login"],
                    "content_type": "avatar",
                    "content_id": cid,
                    "url": owner["avatar_url"],
                    "extension": "jpg",
                })

        await self._collect_repo_releases(client, full_name, uid, owner["login"])
        await self.checkpoint.save_progress(full_name)

    async def _collect_repo_releases(self, client: httpx.AsyncClient,
                                     full_name: str, uid: str, login: str):
        releases = await self._paginate(client, f"{API_BASE}/repos/{full_name}/releases")
        for release in releases:
            if self._stop.is_set():
                break
            for asset in release.get("assets", []):
                cid = str(asset["id"])
                if self.is_known(cid):
                    continue
                await self.download_media({
                    "entity_id": uid,
                    "entity_name": login,
                    "content_type": "release",
                    "content_id": cid,
                    "url": asset["browser_download_url"],
                    "extension": Path(asset["name"]).suffix.lstrip(".") or "bin",
                    "source_url": asset["browser_download_url"],
                    "metadata": json.dumps({
                        "repo": full_name,
                        "release": release["tag_name"],
                        "asset_name": asset["name"],
                        "size": asset["size"],
                    }),
                })

    async def download_media(self, item: dict):
        cid = item["content_id"]
        if self.is_known(cid):
            return

        filename = self.build_filename(
            entity_id=item["entity_id"],
            entity_name=item["entity_name"],
            content_type=item["content_type"],
            content_id=cid,
            extension=item.get("extension", "jpg"),
        )

        dest = self.media_dir / filename
        if dest.exists():
            return

        try:
            await asyncio.sleep(self._download_delay)
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(item["url"])
                resp.raise_for_status()
                data = resp.content

            sha = self.sha256_bytes(data)
            self.save_file(data, filename)
            self.rate_limiter.record_success("github.com")
            self.circuit_breaker.record_success()

            await self.insert_media_item(
                entity_id=item["entity_id"],
                entity_name=item["entity_name"],
                content_type=item["content_type"],
                content_id=cid,
                filename=filename,
                file_path=str(dest),
                file_size=len(data),
                sha256=sha,
                source_url=item.get("source_url"),
                metadata=item.get("metadata"),
            )
        except Exception as e:
            self.rate_limiter.record_failure("github.com")
            self.circuit_breaker.record_failure()
            logger.error("Download failed %s: %s", cid, e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))

    async def _persist_edge(self, source_user: str, target_user: str, edge_type: str = "follows"):
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO graph_edges (source_user, target_user, edge_type)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (source_user, target_user, edge_type) DO NOTHING
                    """,
                    source_user.lower(), target_user.lower(), edge_type,
                )
        except Exception as e:
            logger.debug("Edge persist failed: %s", e)

    async def _store_photo_blob(self, uid: str, username: str, avatar_url: str, data: bytes):
        if not self._blob_enabled or not self._pool:
            return
        try:
            md5 = hashlib.md5(data).hexdigest()
            phash_str = None
            try:
                import imagehash
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(data))
                phash_str = str(imagehash.phash(img))
            except Exception:
                pass

            async with self._pool.acquire() as conn:
                db_size = await conn.fetchval(
                    "SELECT COALESCE(SUM(octet_length(avatar_blob)), 0) FROM profile_photo_history"
                )
                max_bytes = self._photo_tracker._blob_max_size_mb * 1024 * 1024
                blob = data if db_size < max_bytes else None

                await conn.execute(
                    """
                    INSERT INTO profile_photo_history
                        (user_id, username, avatar_url, avatar_md5, avatar_phash, avatar_blob)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    uid, username, avatar_url, md5, phash_str, blob,
                )
        except Exception as e:
            logger.debug("Photo blob storage failed: %s", e)

    async def batch_download_avatars(self, start_id: int, end_id: int):
        async with httpx.AsyncClient(timeout=30) as client:
            batch_count = 0
            synced = 0
            downloaded = 0

            if self._pool:
                async with self._pool.acquire() as conn:
                    rows = await conn.fetch("SELECT user_id FROM avatar_downloads")
                    self._db_avatar_ids = {r["user_id"] for r in rows}

            for uid in range(start_id, end_id + 1):
                if self._stop.is_set():
                    break

                cid = f"avatar_{uid}"
                on_disk = self.is_known(cid)
                in_db = uid in self._db_avatar_ids

                if on_disk and in_db:
                    continue

                if on_disk and not in_db:
                    await self._lazy_sync_avatar(uid)
                    synced += 1
                    continue

                async with self._batch_sem:
                    resp = await self._api_get(client, f"{API_BASE}/user/{uid}")
                    if resp is None:
                        continue
                    user = resp.json()
                    avatar_url = user.get("avatar_url", "")
                    if not avatar_url:
                        continue

                    if self._avatar_size:
                        sep = "&" if "?" in avatar_url else "?"
                        avatar_url = f"{avatar_url}{sep}s={self._avatar_size}"

                    await self.download_media({
                        "entity_id": str(uid),
                        "entity_name": user.get("login", str(uid)),
                        "content_type": "avatar",
                        "content_id": cid,
                        "url": avatar_url,
                        "extension": "jpg",
                    })
                    downloaded += 1

                batch_count += 1
                if batch_count % 100 == 0:
                    logger.info("Batch avatars: processed %d (downloaded=%d, synced=%d)", batch_count, downloaded, synced)

            logger.info("Batch complete: %d total (downloaded=%d, synced=%d)", batch_count, downloaded, synced)

    async def _lazy_sync_avatar(self, user_id: int):
        if not self._pool:
            return
        cid = f"avatar_{user_id}"
        filename_pattern = f"*_{cid}.*"
        matches = list(self.media_dir.glob(filename_pattern))
        if not matches:
            return
        path = matches[0]
        data = path.read_bytes()
        md5 = hashlib.md5(data).hexdigest()

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO avatar_downloads (user_id, md5_hash, file_path, file_size)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    user_id, md5, str(path), len(data),
                )
            self._db_avatar_ids.add(user_id)
        except Exception as e:
            logger.debug("Lazy sync failed for %d: %s", user_id, e)
