import asyncio
import hashlib
import io
import json
import logging
import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx

from src.core.base_collector import BaseCollector
from src.core.search_cache import SearchCache
from src.core.file_naming import sanitize_name

logger = logging.getLogger(__name__)


class SearchCollector(BaseCollector):
    SOURCE_NAME = "search"

    def __init__(self):
        super().__init__()
        self._max_results = int(os.getenv("SEARCH_MAX_RESULTS", "50"))
        self._min_dim = int(os.getenv("SEARCH_MIN_DIMENSION", "200"))
        self._min_file_size = int(os.getenv("SEARCH_MIN_FILE_SIZE", "10240"))
        # Tor SOCKS5 proxy (default: sidecar container `tor` in docker-compose).
        # Set SEARCH_TOR_PROXY="" to disable; set to e.g. "socks5://127.0.0.1:9050"
        # for host-side runs. Empty = direct (clearnet) traffic.
        self._tor_proxy = os.getenv("SEARCH_TOR_PROXY", "socks5://tor:9050").strip()
        self._sem = asyncio.Semaphore(3)
        self._cache = SearchCache(cache_dir=Path("data") / "search_cache", ttl_hours=float(os.getenv("SEARCH_CACHE_TTL_HOURS", "24")))
        self._use_ddg = self._check_ddg()

    @staticmethod
    def _check_ddg() -> bool:
        try:
            from duckduckgo_search import DDGS
            return True
        except ImportError: return False

    @property
    def account_media_dir(self) -> Path:
        path = self.media_dir / "default"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def collect(self, targets: list[str]):
        for query in targets:
            if self._stop.is_set(): break
            logger.info("Collecting search/%s", query)
            try:
                await self._upsert_query(query)
                await self._waterfall_search(query)
                await self.checkpoint.save_progress(query)
            except Exception as e:
                logger.error("Failed search/%s: %s", query, e)
                await self.send_to_dlq(query, query, str(e))

    async def _upsert_query(self, query: str):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO search_queries (query, engine, updated_at) VALUES ($1, $2, NOW()) ON CONFLICT (query, engine) DO UPDATE SET updated_at = NOW()", query, "waterfall")

    async def _upsert_result(self, query: str, res: dict):
        async with self.pool.acquire() as conn:
            q_row = await conn.fetchrow("SELECT id FROM search_queries WHERE query = $1", query)
            if q_row:
                await conn.execute("INSERT INTO search_results (query_id, url, title, snippet, rank) VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING", q_row['id'], res['url'], res.get('title'), res.get('snippet'), res.get('rank'))

    async def _waterfall_search(self, query: str):
        total = 0
        if self._use_ddg:
            total += await self._search_via_ddg(query)
        logger.info("Search '%s': %d results collected (tor=%s)", query, total, bool(self._tor_proxy))

    async def _search_via_ddg(self, query: str) -> int:
        """DuckDuckGo image search, optionally tunneled through Tor SOCKS5.

        duckduckgo_search >=4 accepts a `proxy` arg that is forwarded to httpx.
        Falls back to direct connection if the Tor sidecar is unreachable.
        """
        try:
            from duckduckgo_search import DDGS
            loop = asyncio.get_event_loop()

            def _do_search(use_proxy: bool):
                kwargs = {}
                if use_proxy and self._tor_proxy:
                    kwargs["proxy"] = self._tor_proxy
                with DDGS(**kwargs) as ddgs:
                    return list(ddgs.images(query, max_results=self._max_results))

            results = []
            if self._tor_proxy:
                try:
                    results = await loop.run_in_executor(None, lambda: _do_search(True))
                except Exception as e:
                    logger.warning("DDG via Tor failed (%s); falling back to direct", e)
                    results = []
            if not results:
                results = await loop.run_in_executor(None, lambda: _do_search(False))

            for i, r in enumerate(results):
                res = {"url": r.get("image"), "title": r.get("title"), "rank": i+1}
                if not res["url"]:
                    continue
                await self._upsert_result(query, res)
                await self._process_res(query, res)
            return len(results)
        except Exception as e:
            logger.warning("DDG search failed for '%s': %s", query, e)
            return 0

    async def _process_res(self, query: str, res: dict):
        q_slug = hashlib.sha256(query.encode()).hexdigest()[:12]
        url = res["url"]
        cid = hashlib.sha256(url.encode()).hexdigest()[:16]
        if not self.is_known(cid):
            await self.download_media({"entity_id": q_slug, "entity_name": query[:50], "content_type": "image", "content_id": cid, "url": url, "extension": "jpg", "source_url": url, "raw": res})

    async def download_media(self, item: dict):
        cid = item["content_id"]
        if self.is_known(cid): return
        filename = self.build_filename(item["entity_id"], item["entity_name"], item["content_type"], cid, extension=item.get("extension", "jpg"))
        dest_dir = self.account_media_dir / item["content_type"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        tmp_path = None
        try:
            # follow_redirects=True is intentional for image URLs — but the
            # source URL is search-result-controlled, so an attacker who can
            # influence search results could redirect to internal IPs.
            # Acceptable here because the result is binary content we hash and
            # store, not interpreted; SSRF impact is bounded to "we fetched a
            # weird URL". If this surface widens, add an allow-list.
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, max_redirects=5) as client:
                resp = await client.get(item["url"]); resp.raise_for_status(); data = resp.content
            sha = self.sha256_bytes(data)
            fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
            with os.fdopen(fd, "wb") as f: f.write(data); f.flush(); os.fsync(f.fileno())
            os.replace(tmp_path, dest)
            tmp_path = None  # ownership transferred via os.replace
            metadata = {"entity_id": item["entity_id"], "entity_name": item["entity_name"], "content_type": item["content_type"], "content_id": cid, "collected_at": datetime.now(timezone.utc).isoformat(), "raw": item.get("raw", {})}
            self.save_json(metadata, dest_dir / f"{Path(filename).stem}_metadata.json")
            await self.insert_media_item(entity_id=item["entity_id"], entity_name=item["entity_name"], content_type=item["content_type"], content_id=cid, filename=filename, file_path=str(dest), file_size=len(data), sha256=sha, metadata=metadata)
            self._known_ids.add(cid)
        except Exception:
            # Don't swallow silently — log with stack so downloader failures
            # are visible. Also clean up any leftover tempfile.
            logger.exception("search download_media failed for cid=%s url=%s", cid, item.get("url", ""))
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    async def cleanup(self): pass
