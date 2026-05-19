import asyncio
import hashlib
import io
import logging
import os
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from src.core.base_collector import BaseCollector
from src.core.search_cache import SearchCache

logger = logging.getLogger(__name__)


class SearchCollector(BaseCollector):
    SOURCE_NAME = "search"

    def __init__(self):
        super().__init__()
        self._api_key = os.getenv("SEARCH_API_KEY", "") or os.getenv("SERPER_API_KEY", "")
        self._max_results = int(os.getenv("SEARCH_MAX_RESULTS", "50"))
        self._min_dim = int(os.getenv("SEARCH_MIN_DIMENSION", "200"))
        self._min_file_size = int(os.getenv("SEARCH_MIN_FILE_SIZE", "10240"))
        self._sem = asyncio.Semaphore(3)
        self._cache = SearchCache(
            cache_dir=Path("data") / "search_cache",
            ttl_hours=float(os.getenv("SEARCH_CACHE_TTL_HOURS", "24")),
        )
        self._use_ddg = self._check_ddg()
        self._spider_enabled = os.getenv("SEARCH_SPIDER_ENABLED", "true").lower() == "true"
        self._spider_max_pages = int(os.getenv("SEARCH_SPIDER_MAX_PAGES", "10"))

    @staticmethod
    def _check_ddg() -> bool:
        try:
            from duckduckgo_search import DDGS
            return True
        except ImportError:
            return False

    async def collect(self, targets: list[str]):
        for query in targets:
            if self._stop.is_set():
                break
            logger.info("Collecting search/%s", query)
            try:
                await self._waterfall_search(query)
                await self.checkpoint.save_progress(query)
            except Exception as e:
                logger.error("Failed search/%s: %s", query, e)
                await self.send_to_dlq(query, query, str(e))

    async def _waterfall_search(self, query: str):
        """Waterfall strategy: DDG first (free), then Serper (paid) if needed."""
        total = 0

        if self._use_ddg:
            total += await self._search_via_ddg(query)

        if total < self._max_results and self._api_key:
            total += await self._search_via_serper(query)

        if total == 0:
            total += await self._search_via_scrape(query)

        if self._spider_enabled and total > 0:
            spider_count = await self._spider_result_pages(query)
            total += spider_count

        logger.info("Search '%s': %d results collected", query, total)

    async def _search_via_ddg(self, query: str) -> int:
        cached = self._cache.get(query, engine="ddg")
        if cached:
            logger.debug("DDG cache hit for '%s'", query)
            return await self._process_image_results(cached, query, "ddg")

        try:
            from duckduckgo_search import DDGS
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(
                None,
                lambda: list(DDGS().images(query, max_results=self._max_results)),
            )
            image_urls = [r.get("image", r.get("url", "")) for r in results if r.get("image") or r.get("url")]
            self._cache.put(query, image_urls, engine="ddg")
            return await self._process_image_results(image_urls, query, "ddg")
        except Exception as e:
            logger.warning("DDG search failed: %s", e)
            return 0

    async def _search_via_serper(self, query: str) -> int:
        cached = self._cache.get(query, engine="serper")
        if cached:
            logger.debug("Serper cache hit for '%s'", query)
            return await self._process_image_results(cached, query, "serper")

        await self.wait_rate_limit("serper.dev")

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://google.serper.dev/images",
                headers={
                    "X-API-KEY": self._api_key,
                    "Content-Type": "application/json",
                },
                json={"q": query, "num": self._max_results},
            )
            resp.raise_for_status()
            data = resp.json()
            self.rate_limiter.record_success("serper.dev")

        images = data.get("images", [])
        image_urls = [img.get("imageUrl") or img.get("link") for img in images if img.get("imageUrl") or img.get("link")]
        self._cache.put(query, image_urls, engine="serper")
        return await self._process_image_results(image_urls, query, "serper")

    async def _search_via_scrape(self, query: str) -> int:
        await self.wait_rate_limit("google.com")

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(
                "https://www.google.com/search",
                params={"q": query, "tbm": "isch", "hl": "en"},
                headers={"User-Agent": self.user_agents.get_for_domain("google.com")},
            )
            resp.raise_for_status()
            html = resp.text

        urls = self._extract_image_urls(html)
        return await self._process_image_results(urls[:self._max_results], query, "scrape")

    async def _process_image_results(self, urls: list[str], query: str, engine: str) -> int:
        query_slug = hashlib.sha256(query.encode()).hexdigest()[:12]
        count = 0

        for url in urls:
            if self._stop.is_set():
                break
            if not url or not url.startswith("http"):
                continue

            cid = hashlib.sha256(url.encode()).hexdigest()[:16]
            if self.is_known(cid):
                continue

            await self.download_media({
                "entity_id": query_slug,
                "entity_name": query.replace(" ", "_")[:50],
                "content_type": "image",
                "content_id": cid,
                "url": url,
                "extension": self._ext_from_url(url),
                "source_url": url,
            })
            count += 1

        return count

    @staticmethod
    def _extract_image_urls(html: str) -> list[str]:
        urls: list[str] = []
        search_start = 0
        while True:
            idx = html.find('"https://', search_start)
            if idx == -1:
                break
            end = html.find('"', idx + 1)
            if end == -1:
                break
            url = html[idx + 1:end]
            if any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
                if "google.com" not in url and "gstatic.com" not in url:
                    urls.append(url)
            search_start = end + 1
        return list(dict.fromkeys(urls))

    @staticmethod
    def _ext_from_url(url: str) -> str:
        path = urlparse(url).path.lower()
        for ext in (".png", ".gif", ".webp", ".bmp", ".jpeg", ".jpg"):
            if path.endswith(ext):
                return ext.lstrip(".")
        return "jpg"

    async def _spider_result_pages(self, query: str) -> int:
        cached = self._cache.get(query, engine="ddg") or self._cache.get(query, engine="serper") or []
        page_urls = set()
        for url in cached:
            if not url or not url.startswith("http"):
                continue
            parsed = urlparse(url)
            base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            parent = base.rsplit("/", 1)[0] if "/" in parsed.path[1:] else base
            page_urls.add(parent)

        count = 0
        query_slug = hashlib.sha256(query.encode()).hexdigest()[:12]

        for page_url in list(page_urls)[:self._spider_max_pages]:
            if self._stop.is_set():
                break
            domain = urlparse(page_url).netloc
            await self.wait_rate_limit(domain)

            try:
                async with self._sem:
                    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                        resp = await client.get(
                            page_url,
                            headers={"User-Agent": self.user_agents.get_for_domain(domain)},
                        )
                if resp.status_code != 200:
                    continue
                ct = resp.headers.get("content-type", "")
                if "text/html" not in ct:
                    continue

                images = self._parse_page_images(resp.text, page_url)
                self.rate_limiter.record_success(domain)

                for img_url in images:
                    if self._stop.is_set():
                        break
                    cid = hashlib.sha256(img_url.encode()).hexdigest()[:16]
                    if self.is_known(cid):
                        continue
                    await self.download_media({
                        "entity_id": query_slug,
                        "entity_name": query.replace(" ", "_")[:50],
                        "content_type": "image",
                        "content_id": cid,
                        "url": img_url,
                        "extension": self._ext_from_url(img_url),
                        "source_url": page_url,
                    })
                    count += 1

            except Exception as e:
                self.rate_limiter.record_failure(domain)
                logger.debug("Spider page %s failed: %s", page_url, e)

        return count

    @staticmethod
    def _parse_page_images(html: str, page_url: str) -> list[str]:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return []
        soup = BeautifulSoup(html, "html.parser")
        images = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if src:
                full = urljoin(page_url, src)
                path = urlparse(full).path.lower()
                if any(path.endswith(e) for e in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
                    images.append(full)
        return list(dict.fromkeys(images))

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
            domain = urlparse(item["url"]).netloc
            await self.wait_rate_limit(domain)

            async with self._sem:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                    resp = await client.get(
                        item["url"],
                        headers={"User-Agent": self.user_agents.get_for_domain(domain)},
                    )
                    resp.raise_for_status()
                    data = resp.content

            if len(data) < self._min_file_size:
                return

            width, height = None, None
            try:
                from PIL import Image
                img = Image.open(io.BytesIO(data))
                width, height = img.size
                if width < self._min_dim or height < self._min_dim:
                    return
            except Exception:
                pass

            sha = self.sha256_bytes(data)
            self.save_file(data, filename)
            self.rate_limiter.record_success(domain)
            self.circuit_breaker.record_success()

            await self.insert_media_item(
                entity_id=item["entity_id"],
                entity_name=item["entity_name"],
                content_type=item["content_type"],
                content_id=cid,
                filename=filename,
                file_path=str(dest),
                file_size=len(data),
                width=width,
                height=height,
                sha256=sha,
                source_url=item.get("source_url"),
            )
        except Exception as e:
            self.rate_limiter.record_failure(item.get("entity_id", "search"))
            self.circuit_breaker.record_failure()
            logger.error("Download failed %s: %s", cid, e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))
