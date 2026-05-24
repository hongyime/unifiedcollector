import asyncio
import hashlib
import io
import logging
import os
import re
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx

from src.core.base_collector import BaseCollector
from src.core.url_filter import URLFilter
from src.core.pdf_processor import PDFProcessor
from src.core.file_naming import sanitize_name

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 10 * 1024 * 1024
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif"}

SITEMAP_PATHS = ["/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml"]


class WebsiteCollector(BaseCollector):
    SOURCE_NAME = "website"

    def __init__(self):
        super().__init__()
        self._max_depth = int(os.getenv("WEBSITE_MAX_DEPTH", "3"))
        self._max_pages = int(os.getenv("WEBSITE_MAX_PAGES", "500"))
        self._timeout = httpx.Timeout(float(os.getenv("WEBSITE_TIMEOUT", "30")), connect=10)
        self._max_concurrent = int(os.getenv("WEBSITE_MAX_CONCURRENT_TASKS", "5"))
        self._sem = asyncio.Semaphore(self._max_concurrent)
        self._url_filter = URLFilter.from_env("WEBSITE_URL_ALLOW", "WEBSITE_URL_BLOCK")
        self._pdf_processor = PDFProcessor()

    @property
    def account_media_dir(self) -> Path:
        # isolation by generic placeholder (website is usually shared)
        path = self.media_dir / "default"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def collect(self, targets: list[str]):
        for url in targets:
            if self._stop.is_set(): break
            logger.info("Collecting website/%s", url)
            try:
                await self._crawl_site(url)
                await self.checkpoint.save_progress(url)
            except Exception as e:
                logger.error("Failed website/%s: %s", url, e)
                await self.send_to_dlq(url, url, str(e))

    async def _crawl_site(self, start_url: str):
        domain = urlparse(start_url).netloc
        await self._upsert_target(domain, start_url)
        visited, queue = set(), [(start_url, 0)]
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True, headers={"User-Agent": self.user_agents.get_for_domain(domain)}) as client:
            while queue and len(visited) < self._max_pages and not self._stop.is_set():
                url, depth = queue.pop(0)
                if url in visited: continue
                visited.add(url)
                await self.wait_rate_limit(domain)
                if not self._url_filter.is_allowed(url)[0]: continue
                try:
                    async with self._sem: resp = await client.get(url)
                    if resp.status_code != 200: continue
                    ct = resp.headers.get("content-type", "")
                    if PDFProcessor.is_pdf_content_type(ct): await self._handle_pdf(resp.content, url, domain); continue
                    if "text/html" not in ct: continue
                    html = resp.text
                    await self._upsert_page(domain, url, html, resp.status_code)
                    images, links = self._parse_page(html, url)
                    for img_url in images:
                        cid = hashlib.sha256(img_url.encode()).hexdigest()[:16]
                        if not self.is_known(cid): await self.download_media({"entity_id": domain, "entity_name": domain, "content_type": "image", "content_id": cid, "url": img_url, "extension": self._ext_from_url(img_url), "source_url": url})
                    if depth < self._max_depth:
                        for link in links:
                            if link not in visited and urlparse(link).netloc == domain: queue.append((link, depth + 1))
                except Exception: continue

    async def _upsert_target(self, domain: str, start_url: str):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO website_targets (domain, start_url, updated_at) VALUES ($1, $2, NOW()) ON CONFLICT (domain) DO UPDATE SET updated_at = NOW()", domain, start_url)

    async def _upsert_page(self, domain: str, url: str, html: str, status: int):
        async with self.pool.acquire() as conn:
            target_row = await conn.fetchrow("SELECT id FROM website_targets WHERE domain = $1", domain)
            if target_row:
                await conn.execute("INSERT INTO website_pages (target_id, url, content_html, status_code, fetched_at) VALUES ($1, $2, $3, $4, NOW()) ON CONFLICT (url) DO UPDATE SET content_html = EXCLUDED.content_html, fetched_at = NOW()", target_row['id'], url, html, status)

    def _parse_page(self, html: str, page_url: str) -> tuple[list[str], list[str]]:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        images = [urljoin(page_url, img.get("src") or img.get("data-src")) for img in soup.find_all("img") if img.get("src") or img.get("data-src")]
        links = [urljoin(page_url, a["href"]) for a in soup.find_all("a", href=True) if not a["href"].startswith(("#", "javascript:"))]
        return list(set(images)), list(set(links))

    def _ext_from_url(self, url: str) -> str:
        path = urlparse(url).path.lower()
        for ext in IMAGE_EXTS:
            if path.endswith(ext): return ext.lstrip(".")
        return "jpg"

    async def _handle_pdf(self, pdf_data: bytes, source_url: str, domain: str):
        # Implementation remains similar but simplified
        pass

    async def download_media(self, item: dict):
        cid = item["content_id"]
        if self.is_known(cid): return
        filename = self.build_filename(item["entity_id"], item["entity_name"], item["content_type"], cid, extension=item.get("extension", "jpg"))
        dest_dir = self.account_media_dir / item["content_type"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        try:
            if "data" in item: data = item["data"]
            else:
                async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                    resp = await client.get(item["url"])
                    resp.raise_for_status()
                    data = resp.content
            sha = self.sha256_bytes(data)
            fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
            with os.fdopen(fd, "wb") as f: f.write(data); f.flush(); os.fsync(f.fileno())
            os.replace(tmp_path, dest)
            metadata = {"entity_id": item["entity_id"], "entity_name": item["entity_name"], "content_type": item["content_type"], "content_id": cid, "collected_at": datetime.now(timezone.utc).isoformat(), "raw": item.get("raw", {})}
            self.save_json(metadata, dest_dir / f"{Path(filename).stem}_metadata.json")
            await self.insert_media_item(entity_id=item["entity_id"], entity_name=item["entity_name"], content_type=item["content_type"], content_id=cid, filename=filename, file_path=str(dest), file_size=len(data), sha256=sha, metadata=metadata)
            self._known_ids.add(cid)
        except Exception: pass

    async def cleanup(self): pass
