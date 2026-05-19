import asyncio
import gzip
import hashlib
import io
import logging
import os
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from src.core.base_collector import BaseCollector
from src.core.url_filter import URLFilter
from src.core.pdf_processor import PDFProcessor

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 10 * 1024 * 1024
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif"}

SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/wp-sitemap.xml",
    "/sitemap/sitemap.xml",
    "/sitemaps/sitemap.xml",
    "/sitemap1.xml",
]


class WebsiteCollector(BaseCollector):
    SOURCE_NAME = "website"

    def __init__(self):
        super().__init__()
        self._max_depth = int(os.getenv("WEBSITE_MAX_DEPTH", "3"))
        self._max_pages = int(os.getenv("WEBSITE_MAX_PAGES", "500"))
        self._timeout = httpx.Timeout(
            float(os.getenv("WEBSITE_TIMEOUT", "30")),
            connect=10,
        )
        self._max_concurrent = int(os.getenv("WEBSITE_MAX_CONCURRENT_TASKS", "5"))
        self._sem = asyncio.Semaphore(self._max_concurrent)
        self._min_width = int(os.getenv("WEBSITE_MIN_WIDTH", "100"))
        self._min_height = int(os.getenv("WEBSITE_MIN_HEIGHT", "100"))
        self._min_file_size = int(os.getenv("WEBSITE_MIN_FILE_SIZE", "5120"))
        self._url_filter = URLFilter.from_env("WEBSITE_URL_ALLOW", "WEBSITE_URL_BLOCK")
        self._pdf_processor = PDFProcessor()
        self._pdf_max_pages = int(os.getenv("WEBSITE_PDF_MAX_PAGES", "100"))

    async def collect(self, targets: list[str]):
        for url in targets:
            if self._stop.is_set():
                break
            logger.info("Collecting website/%s", url)
            try:
                await self._crawl_site(url)
                await self.checkpoint.save_progress(url)
            except Exception as e:
                logger.error("Failed website/%s: %s", url, e)
                await self.send_to_dlq(url, url, str(e))

    async def _crawl_site(self, start_url: str):
        domain = urlparse(start_url).netloc
        visited: set[str] = set()
        queue: list[tuple[str, int]] = [(start_url, 0)]

        sitemap_urls = await self._discover_sitemap(start_url)
        for su in sitemap_urls:
            if su not in visited:
                queue.append((su, 1))

        async with httpx.AsyncClient(
            timeout=self._timeout, follow_redirects=True,
            headers={"User-Agent": self.user_agents.get_for_domain(domain)},
        ) as client:
            while queue and len(visited) < self._max_pages:
                if self._stop.is_set():
                    break

                url, depth = queue.pop(0)
                if url in visited:
                    continue
                visited.add(url)

                await self.wait_rate_limit(domain)

                allowed, reason = self._url_filter.is_allowed(url)
                if not allowed:
                    logger.debug("URL filtered: %s (%s)", url, reason)
                    continue

                try:
                    async with self._sem:
                        resp = await client.get(url)
                    if resp.status_code != 200:
                        continue
                    ct = resp.headers.get("content-type", "")

                    if PDFProcessor.is_pdf_content_type(ct):
                        await self._handle_pdf(resp.content, url, domain)
                        continue

                    if "text/html" not in ct:
                        continue
                    html = resp.text
                except Exception as e:
                    logger.debug("Failed to fetch %s: %s", url, e)
                    self.rate_limiter.record_failure(domain)
                    continue

                self.rate_limiter.record_success(domain)
                images, links = self._parse_page(html, url)

                for img_url in images:
                    if self._stop.is_set():
                        break
                    cid = hashlib.sha256(img_url.encode()).hexdigest()[:16]
                    if not self.is_known(cid):
                        await self.download_media({
                            "entity_id": domain,
                            "entity_name": domain,
                            "content_type": "image",
                            "content_id": cid,
                            "url": img_url,
                            "extension": self._ext_from_url(img_url),
                            "source_url": url,
                        })

                if depth < self._max_depth:
                    for link in links:
                        if link not in visited and urlparse(link).netloc == domain:
                            ok, _ = self._url_filter.is_allowed(link)
                            if ok:
                                queue.append((link, depth + 1))

    async def _discover_sitemap(self, base_url: str) -> list[str]:
        urls: list[str] = []

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                headers={"User-Agent": self.user_agents.get_for_domain(urlparse(base_url).netloc)},
            ) as client:
                robots_url = urljoin(base_url, "/robots.txt")
                resp = await client.get(robots_url)
                if resp.status_code == 200:
                    for line in resp.text.splitlines():
                        if line.lower().startswith("sitemap:"):
                            sm_url = line.split(":", 1)[1].strip()
                            urls.extend(await self._parse_sitemap(client, sm_url))

                for path in SITEMAP_PATHS:
                    sm_url = urljoin(base_url, path)
                    if sm_url not in urls:
                        found = await self._parse_sitemap(client, sm_url)
                        urls.extend(found)
        except Exception:
            pass
        return list(set(urls))

    async def _parse_sitemap(self, client: httpx.AsyncClient, url: str,
                             depth: int = 0, max_depth: int = 3) -> list[str]:
        if depth > max_depth:
            return []

        urls: list[str] = []
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return urls

            content = resp.content
            if content[:2] == b"\x1f\x8b":
                content = gzip.decompress(content)

            import defusedxml.ElementTree as ET
            root = ET.fromstring(content)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

            for sitemap_loc in root.findall(".//sm:sitemap/sm:loc", ns):
                if sitemap_loc.text:
                    sub_urls = await self._parse_sitemap(client, sitemap_loc.text.strip(), depth + 1, max_depth)
                    urls.extend(sub_urls)

            for loc in root.findall(".//sm:url/sm:loc", ns):
                if loc.text:
                    urls.append(loc.text.strip())
        except Exception:
            pass
        return urls

    def _parse_page(self, html: str, page_url: str) -> tuple[list[str], list[str]]:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        images: list[str] = []
        links: list[str] = []

        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            if src:
                full = urljoin(page_url, src)
                if self._is_image_url(full):
                    images.append(full)

        for source in soup.find_all("source"):
            srcset = source.get("srcset")
            if srcset:
                for part in srcset.split(","):
                    url_part = part.strip().split()[0]
                    full = urljoin(page_url, url_part)
                    if self._is_image_url(full):
                        images.append(full)

        for meta in soup.find_all("meta"):
            prop = meta.get("property", "")
            if prop in ("og:image", "twitter:image"):
                content = meta.get("content", "")
                if content:
                    images.append(urljoin(page_url, content))

        bg_pattern = re.compile(r'url\(["\']?(https?://[^"\')\s]+)["\']?\)')
        for style in soup.find_all("style"):
            if style.string:
                for match in bg_pattern.finditer(style.string):
                    images.append(match.group(1))
        for tag in soup.find_all(style=True):
            for match in bg_pattern.finditer(tag["style"]):
                images.append(match.group(1))

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith(("javascript:", "mailto:", "tel:", "#")):
                continue
            links.append(urljoin(page_url, href))

        return list(set(images)), list(set(links))

    def _is_image_url(self, url: str) -> bool:
        path = urlparse(url).path.lower()
        return any(path.endswith(ext) for ext in IMAGE_EXTS)

    def _ext_from_url(self, url: str) -> str:
        path = urlparse(url).path.lower()
        for ext in IMAGE_EXTS:
            if path.endswith(ext):
                return ext.lstrip(".")
        return "jpg"

    async def _handle_pdf(self, pdf_data: bytes, source_url: str, domain: str):
        if not self._pdf_processor.available:
            logger.debug("PyMuPDF not available, skipping PDF: %s", source_url)
            return

        try:
            pages = await self._pdf_processor.convert_to_images(
                pdf_data, max_pages=self._pdf_max_pages,
            )
            pdf_hash = hashlib.sha256(source_url.encode()).hexdigest()[:12]

            for img_data, page_num in pages:
                if self._stop.is_set():
                    break
                cid = f"pdf_{pdf_hash}_p{page_num}"
                if self.is_known(cid):
                    continue
                await self.download_media({
                    "entity_id": domain,
                    "entity_name": domain,
                    "content_type": "pdf_page",
                    "content_id": cid,
                    "data": img_data,
                    "extension": "png",
                    "source_url": source_url,
                })
            logger.info("Extracted %d pages from PDF: %s", len(pages), source_url)
        except Exception as e:
            logger.debug("PDF processing failed for %s: %s", source_url, e)

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
            if "data" in item:
                data = item["data"]
                sha = self.sha256_bytes(data)
                self.save_file(data, filename)
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
                )
                return

            domain = urlparse(item["url"]).netloc
            async with httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=True,
                headers={"User-Agent": self.user_agents.get_for_domain(domain)},
            ) as client:
                resp = await client.get(item["url"])
                resp.raise_for_status()
                data = resp.content

            if len(data) < self._min_file_size:
                return
            if len(data) > MAX_FILE_SIZE:
                return

            width, height = None, None
            try:
                from PIL import Image
                img = Image.open(io.BytesIO(data))
                width, height = img.size
                if width < self._min_width or height < self._min_height:
                    return
            except Exception:
                pass

            sha = self.sha256_bytes(data)
            self.save_file(data, filename)
            self.rate_limiter.record_success(item["entity_id"])
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
            self.rate_limiter.record_failure(item["entity_id"])
            self.circuit_breaker.record_failure()
            logger.error("Download failed %s: %s", item["url"], e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))
