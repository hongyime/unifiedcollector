"""
Unified Website Toolkit - Photo Scraper
Advanced image scraping with support for various image sources
"""
import os
import re
import asyncio
import aiohttp
import hashlib
import io
import ssl
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup, Tag, NavigableString
from typing import List, Dict, Set, Optional, Tuple, Union, Any
from PIL import Image
import time
from logger_config import setup_logger

logger = setup_logger(__name__)

# Resolve project root (one level above src/)
TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOWNLOADS = TOOLKIT_ROOT / "downloads"
DEFAULT_DATA = TOOLKIT_ROOT / "data"

try:
    from config import get_config, DOWNLOADS_DIR, DATA_DIR
except ImportError:
    DOWNLOADS_DIR = str(DEFAULT_DOWNLOADS)
    DATA_DIR = str(DEFAULT_DATA)

    def get_config():
        return type('Config', (), {'get_setting': lambda self, key, default: default})()


class ProgressTracker:
    """Simple progress tracker for scraping operations"""

    def __init__(self, operation_name: str):
        self.operation_name = operation_name
        self.start_time = time.time()
        self.items_processed = 0

    def update(self, items_processed: int, total_items: Optional[int] = None):
        """Update progress"""
        self.items_processed = items_processed
        elapsed = time.time() - self.start_time
        if total_items:
            percent = (items_processed / total_items) * 100
            logger.info(f"PROCESSING {self.operation_name}: {items_processed}/{total_items} ({percent:.1f}%) - {elapsed:.1f}s")
        else:
            logger.info(f"PROCESSING {self.operation_name}: {items_processed} items - {elapsed:.1f}s")


class PhotoScraper:
    """Advanced photo scraper with improved error handling"""

    def __init__(self, website_name: str, custom_download_dir: Optional[str] = None, website_url: Optional[str] = None):
        self.website_name = website_name
        self.website_url = website_url
        self.config = get_config()
        self.downloaded_images = set()
        self.found_images = []
        self.custom_download_dir = custom_download_dir
        self.stats = {
            'total_images_found': 0,
            'total_images_downloaded': 0,
            'total_size_downloaded': 0,
            'start_time': None,
            'end_time': None,
            'errors': 0
        }
        self.progress_tracker = ProgressTracker(f"Photo Scraping - {website_name}")

        # Image configuration
        self.supported_formats = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff'}
        try:
            self.max_image_size = self.config.get_setting('max_image_size', 10 * 1024 * 1024)  # 10MB default
            self.min_image_size = self.config.get_setting('min_image_size', 1024)  # 1KB minimum
        except (AttributeError, KeyError):
            self.max_image_size = 10 * 1024 * 1024
            self.min_image_size = 1024

        # Track processed photos to avoid duplicates
        self.downloaded_images = set()
        self.load_processed_hashes()

        # HTTP configuration
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        }
        
        # Auto-add website to config if custom path provided and URL available
        if self.custom_download_dir and self.website_url:
            self._ensure_website_in_config()

    def load_processed_hashes(self):
        """Load previously processed photo hashes from DB"""
        try:
            from db_manager import get_db_manager
            db = get_db_manager()
            self.downloaded_images = set(db.get_all_hashes('photo'))
        except Exception as e:
            logger.warning(f"WARNING: Error loading photo hashes from DB: {e}")
            self.downloaded_images = set()

    def save_hash(self, hash_id: str):
        """Save photo hash to DB and local set"""
        try:
            from db_manager import get_db_manager
            db = get_db_manager()
            db.add_hash(hash_id, 'photo', datetime.now().isoformat())
            self.downloaded_images.add(hash_id)
        except Exception as e:
            logger.warning(f"WARNING: Error saving photo hash to DB: {e}")

    @staticmethod
    def _safe_get_attr(element: Union[Tag, NavigableString, None], attr: str, default: str = '') -> str:
        """Safely get attribute from BeautifulSoup element"""
        if element is None:
            return default
        if isinstance(element, Tag) and hasattr(element, 'get'):
            result = element.get(attr, default)
            return str(result) if result is not None else default
        return default

    @staticmethod
    def _safe_get_text(element: Union[Tag, NavigableString, None], strip: bool = True, default: str = '') -> str:
        """Safely get text from BeautifulSoup element"""
        if element is None:
            return default
        try:
            if hasattr(element, 'get_text'):
                return element.get_text(strip=strip) or default
            elif hasattr(element, 'string'):
                text = element.string
                return text.strip() if strip and text else (text or default)
            else:
                return str(element).strip() if strip else str(element)
        except Exception:
            return default

    @staticmethod
    def _is_tag(element: Union[Tag, NavigableString, None]) -> bool:
        """Check if element is a BeautifulSoup Tag"""
        return isinstance(element, Tag)

    @staticmethod
    def _safe_find_all(element: Union[Tag, NavigableString, None], *args, **kwargs) -> List[Tag]:
        """Safely call find_all on element"""
        if element is None or not isinstance(element, Tag):
            return []
        try:
            return element.find_all(*args, **kwargs)
        except Exception:
            return []

    def _ensure_website_in_config(self):
        """Ensure website is added to config when custom download path is used"""
        try:
            import json
            from config import get_websites, save_config
            
            websites = get_websites()
            
            # Check if website already exists
            for website in websites:
                if website.get('url') == self.website_url or website.get('name') == self.website_name:
                    return  # Already exists, no need to add
            
            # Add new website entry
            new_website = {
                'name': self.website_name,
                'url': self.website_url,
                'enabled': True,
                'added_by': 'photo_scraper',
                'added_at': datetime.now().isoformat(),
                'custom_download_path': self.custom_download_dir
            }
            
            websites.append(new_website)
            
            # Save updated config
            config = {
                'websites': websites,
                'settings': {}
            }
            save_config(config)
            
            logger.info(f"✅ AUTO-ADDED: {self.website_name} to website config")
            
        except Exception as e:
            logger.warning(f"⚠️  Warning: Could not auto-add website to config: {e}")

    def extract_images_from_page(self, html_content: str, base_url: str) -> List[Dict[str, str]]:
        """Extract all image URLs from HTML content with comprehensive error handling"""
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        images = []

        # Method 1: Regular img tags
        img_tags = soup.find_all('img')
        for img in img_tags:
            try:
                if not self._is_tag(img):
                    continue

                src = self._safe_get_attr(img, 'src')
                data_src = self._safe_get_attr(img, 'data-src')
                data_lazy_src = self._safe_get_attr(img, 'data-lazy-src')
                src = src or data_src or data_lazy_src

                if src:
                    full_url = urljoin(base_url, src)
                    images.append({
                        'url': full_url,
                        'alt': self._safe_get_attr(img, 'alt'),
                        'source': 'img_tag'
                    })

                srcset = self._safe_get_attr(img, 'srcset')
                if srcset:
                    try:
                        for src_item in srcset.split(','):
                            src_url = src_item.strip().split(' ')[0]
                            if src_url:
                                full_url = urljoin(base_url, src_url)
                                images.append({
                                    'url': full_url,
                                    'alt': self._safe_get_attr(img, 'alt'),
                                    'source': 'srcset'
                                })
                    except Exception:
                        continue

            except Exception:
                continue

        # Method 2: CSS background images in style tags
        style_tags = soup.find_all('style')
        for style in style_tags:
            try:
                if not self._is_tag(style):
                    continue

                style_content = self._safe_get_text(style, strip=False)
                if style_content:
                    bg_images = re.findall(r'background-image:\s*url\(["\']?([^"\'()]+)["\']?\)', style_content, re.IGNORECASE)
                    for bg_url in bg_images:
                        full_url = urljoin(base_url, bg_url)
                        images.append({
                            'url': full_url,
                            'alt': '',
                            'source': 'css_background'
                        })
            except Exception:
                continue

        # Method 3: Inline style attributes
        elements_with_style = soup.find_all(attrs={'style': True})
        for element in elements_with_style:
            try:
                if not self._is_tag(element):
                    continue

                style_content = self._safe_get_attr(element, 'style')
                if style_content:
                    bg_images = re.findall(r'background-image:\s*url\(["\']?([^"\'()]+)["\']?\)', style_content, re.IGNORECASE)
                    for bg_url in bg_images:
                        full_url = urljoin(base_url, bg_url)
                        images.append({
                            'url': full_url,
                            'alt': '',
                            'source': 'inline_style'
                        })
            except Exception:
                continue

        # Method 4: Picture elements and source tags
        picture_tags = soup.find_all('picture')
        for picture in picture_tags:
            try:
                if not self._is_tag(picture):
                    continue

                sources = self._safe_find_all(picture, 'source')
                for source in sources:
                    srcset = self._safe_get_attr(source, 'srcset')
                    if srcset:
                        try:
                            for src_item in srcset.split(','):
                                src_url = src_item.strip().split(' ')[0]
                                if src_url:
                                    full_url = urljoin(base_url, src_url)
                                    images.append({
                                        'url': full_url,
                                        'alt': '',
                                        'source': 'picture_element'
                                    })
                        except Exception:
                            continue
            except Exception:
                continue

        # Method 5: Links to image files
        link_tags = soup.find_all('a', href=True)
        for link in link_tags:
            try:
                if not self._is_tag(link):
                    continue

                href = self._safe_get_attr(link, 'href')
                if href:
                    full_url = urljoin(base_url, href)
                    if self._is_image_url(full_url):
                        images.append({
                            'url': full_url,
                            'alt': self._safe_get_text(link),
                            'source': 'link_to_image'
                        })
            except Exception:
                continue

        return images

    def _is_image_url(self, url: str) -> bool:
        """Check if URL points to an image file"""
        try:
            parsed = urlparse(url)
            path = parsed.path.lower()
            return any(path.endswith(ext) for ext in self.supported_formats)
        except Exception:
            return False

    async def download_image(self, session: aiohttp.ClientSession, image_info: Dict[str, str]) -> Tuple[bool, str, int]:
        """Download a single image with comprehensive error handling"""
        url = image_info['url']

        try:
            # Fast in-memory check (same process, same run)
            url_hash = hashlib.md5(url.encode()).hexdigest()
            if url_hash in self.downloaded_images:
                return False, "Already downloaded", 0

            # Atomic DB claim — blocks the TOCTOU race across processes/threads.
            # INSERT OR IGNORE returns changes()=0 if another process already owns it.
            from db_manager import get_db_manager
            if not get_db_manager().claim_hash_atomic(url_hash, 'photo'):
                self.downloaded_images.add(url_hash)
                return False, "Already downloaded", 0

            # Create filename
            parsed_url = urlparse(url)
            filename = os.path.basename(parsed_url.path) or f"image_{url_hash}"

            # Ensure proper extension
            if not any(filename.lower().endswith(ext) for ext in self.supported_formats):
                filename += '.jpg'  # Default extension

            # Create safe filename
            safe_filename = re.sub(r'[<>:"/\\\\|?*]', '_', filename)

            # Create download directory - use custom path if provided
            from utils import get_safe_filename
            safe_website_name = get_safe_filename(self.website_name)
            
            if self.custom_download_dir:
                download_dir = os.path.join(self.custom_download_dir, safe_website_name, 'images')
            else:
                download_dir = os.path.join(DOWNLOADS_DIR, safe_website_name, 'images')
            
            os.makedirs(download_dir, exist_ok=True)

            file_path = os.path.join(download_dir, safe_filename)

            # Skip if file already exists
            if os.path.exists(file_path):
                return False, "File already exists", 0

            # Download image with better error handling
            async with session.get(url) as response:
                if response.status != 200:
                    return False, f"HTTP {response.status}", 0

                # Check content type
                content_type = response.headers.get('content-type', '').lower()
                if not content_type.startswith('image/'):
                    return False, f"ERROR: Invalid content type: {content_type}", 0

                # Check content size before downloading
                content_length = response.headers.get('content-length')
                if content_length and self.max_image_size and int(content_length) > self.max_image_size:
                    return False, f"WARNING: Image too large based on header ({content_length} bytes > {self.max_image_size} bytes)", 0

                # Download and stream to file
                import aiofiles
                downloaded_size = 0
                async with aiofiles.open(file_path, 'wb') as f:
                    async for chunk in response.content.iter_chunked(8192):
                        downloaded_size += len(chunk)
                        if self.max_image_size and downloaded_size > self.max_image_size:
                            break
                        await f.write(chunk)

                if self.max_image_size and downloaded_size > self.max_image_size:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    return False, f"WARNING: Image too large ({downloaded_size} bytes > {self.max_image_size} bytes)", 0

                if downloaded_size < self.min_image_size:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    return False, f"WARNING: Image too small ({downloaded_size} bytes < {self.min_image_size} bytes)", 0

                # Validate image format
                try:
                    image = Image.open(file_path)

                    # Check image dimensions (minimum size)
                    width, height = image.size
                    min_dimension = 32  # Minimum 32x32 pixels
                    if width < min_dimension or height < min_dimension:
                        image.close()
                        if os.path.exists(file_path):
                            os.remove(file_path)
                        return False, f"WARNING: Image dimensions too small ({width}x{height} < {min_dimension}x{min_dimension})", 0

                    # Verify image can be processed
                    image.verify()
                    # Some versions of PIL require reopening to verify/read again, but verify() doesn't need to stay open
                    # We just close it
                except Exception as e:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    return False, f"WARNING: Invalid image format: {str(e)}", 0

                # DB claim already written above; just sync the in-memory cache
                self.downloaded_images.add(url_hash)

                return True, file_path, downloaded_size

        except aiohttp.ClientSSLError as e:
            return False, f"SSL SSL Certificate error: {str(e)}", 0
        except aiohttp.ClientConnectorError as e:
            return False, f"CONNECTION Connection error: {str(e)}", 0
        except asyncio.TimeoutError:
            return False, f"TIMEOUT Download timeout", 0
        except Exception as e:
            return False, f"WARNING: Download error: {str(e)}", 0

    async def scrape_website_images(self, urls: List[str]) -> Dict[str, Any]:
        """Scrape images from multiple URLs"""
        logger.info(f"PHOTO Starting photo scraping for {self.website_name}")
        logger.info(f"LINK Processing {len(urls)} URLs")

        self.stats['start_time'] = datetime.now().isoformat()
        all_images = []

        try:
            # Create SSL context with standard verification by default
            ssl_context = ssl.create_default_context()

            # Create connector with SSL context and timeout
            connector = aiohttp.TCPConnector(
                ssl=ssl_context,
                limit=10,
                limit_per_host=5,
                ttl_dns_cache=300,
                use_dns_cache=True,
            )

            # Create session with timeout
            timeout = aiohttp.ClientTimeout(total=30, connect=10)

            async with aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers=self.headers
            ) as session:
                for i, url in enumerate(urls, 1):
                    logger.info(f"PROCESSING Processing URL {i}/{len(urls)}: {url}")

                    try:
                        # Fetch page content with better error handling
                        try:
                            async with session.get(url) as response:
                                if response.status == 200:
                                    html_content = await response.text()

                                    # Extract images
                                    page_images = self.extract_images_from_page(html_content, url)
                                    logger.info(f"IMAGES Found {len(page_images)} images on page")

                                    all_images.extend(page_images)

                                    # Update progress
                                    self.progress_tracker.update(i, len(urls))
                                else:
                                    logger.error(f"ERROR: HTTP {response.status} for {url}")
                                    self.stats['errors'] += 1

                        except aiohttp.ClientSSLError as e:
                            logger.info(f"SSL SSL Certificate issue for {url}: {str(e)}")
                            logger.warning(f"WARNING: Trying with SSL verification disabled...")
                            try:
                                async with session.get(url, ssl=False) as response:
                                    if response.status == 200:
                                        html_content = await response.text()
                                        page_images = self.extract_images_from_page(html_content, url)
                                        logger.info(f"IMAGES Found {len(page_images)} images on page (without SSL)")
                                        all_images.extend(page_images)
                                        self.progress_tracker.update(i, len(urls))
                                    else:
                                        logger.error(f"ERROR: HTTP {response.status} for {url} (without SSL)")
                                        self.stats['errors'] += 1
                            except Exception as e2:
                                logger.error(f"ERROR: Failed even without SSL: {e2}")
                                self.stats['errors'] += 1
                            continue

                        except aiohttp.ClientConnectorError as e:
                            logger.info(f"CONNECTION Connection failed for {url}: {str(e)}")
                            self.stats['errors'] += 1
                            continue

                        except asyncio.TimeoutError:
                            logger.info(f"TIMEOUT Timeout accessing {url}")
                            self.stats['errors'] += 1
                            continue

                        except Exception as e:
                            logger.error(f"ERROR: Error processing {url}: {e}")
                            self.stats['errors'] += 1

                    except Exception as e:
                        logger.error(f"ERROR: Unexpected error processing {url}: {e}")
                        self.stats['errors'] += 1

                    # Rate limiting
                    try:
                        delay = self.config.get_setting('delay_between_requests', 1.0)
                    except AttributeError:
                        delay = 1.0
                    if delay and isinstance(delay, (int, float)) and i < len(urls):
                        await asyncio.sleep(float(delay))

                # Remove duplicates
                unique_images = []
                seen_urls = set()
                for img in all_images:
                    if img['url'] not in seen_urls:
                        unique_images.append(img)
                        seen_urls.add(img['url'])

                self.stats['total_images_found'] = len(unique_images)
                logger.info(f"LIST Found {len(unique_images)} unique images total")

                # Download images within the same session
                if unique_images:
                    logger.info(" Starting image downloads...")

                    # Process downloads with limited concurrency
                    semaphore = asyncio.Semaphore(5)  # Max 5 concurrent downloads

                    async def bounded_download(img_info):
                        async with semaphore:
                            return await self.download_image(session, img_info)

                    results = await asyncio.gather(*[bounded_download(img) for img in unique_images], return_exceptions=True)

                    # Process results
                    successful_downloads = 0
                    total_size = 0

                    for i, result in enumerate(results):
                        try:
                            if isinstance(result, Exception):
                                logger.error(f"ERROR: Download failed: {result}")
                                self.stats['errors'] += 1
                            else:
                                success, message, size = result
                                if success:
                                    successful_downloads += 1
                                    total_size += size
                                    logger.info(f"SUCCESS: Downloaded: {message} ({size} bytes)")
                                else:
                                    logger.warning(f"WARNING: Skipped: {message}")
                        except Exception as e:
                            logger.error(f"ERROR: Error processing result {i}: {e}")
                            self.stats['errors'] += 1

                    self.stats['total_images_downloaded'] = successful_downloads
                    self.stats['total_size_downloaded'] = total_size

        except Exception as e:
            logger.error(f"ERROR: Fatal error during scraping: {e}")

        finally:
            self.stats['end_time'] = datetime.now().isoformat()
            await self._save_scraping_log()
            self._update_site_stats_in_db()

        # Return summary
        return {
            'website_name': self.website_name,
            'total_images_found': self.stats['total_images_found'],
            'total_images_downloaded': self.stats['total_images_downloaded'],
            'total_size_downloaded': self.stats['total_size_downloaded'],
            'errors': self.stats['errors'],
            'duration_seconds': self._calculate_duration()
        }

    def _calculate_duration(self) -> Optional[float]:
        """Calculate scraping duration"""
        try:
            if self.stats['start_time'] and self.stats['end_time']:
                start = datetime.fromisoformat(self.stats['start_time'])
                end = datetime.fromisoformat(self.stats['end_time'])
                return (end - start).total_seconds()
        except Exception:
            pass
        return None

    def _update_site_stats_in_db(self):
        """Increment total_photos_downloaded and set last_scraped on the websites row."""
        downloaded = self.stats.get('total_images_downloaded', 0)
        if downloaded == 0:
            return
        try:
            from db_manager import get_db_manager, _connect
            db = get_db_manager()
            with _connect(db.db_path) as conn:
                conn.execute(
                    """UPDATE websites
                       SET total_photos_downloaded = total_photos_downloaded + ?,
                           last_scraped = ?
                       WHERE name = ?""",
                    (downloaded, datetime.now().isoformat(), self.website_name),
                )
        except Exception as e:
            logger.warning(f"WARNING: Could not update photo stats in DB: {e}")

    async def _save_scraping_log(self):
        """Save scraping statistics to log file"""
        try:
            from utils import get_safe_filename
            safe_website_name = get_safe_filename(self.website_name)
            log_dir = os.path.join(DATA_DIR, 'photo_scraping_logs')
            os.makedirs(log_dir, exist_ok=True)

            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            log_file = os.path.join(log_dir, f"photo_scraping_{safe_website_name}_{timestamp}.json")

            import json
            with open(log_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'website_name': self.website_name,
                    'stats': self.stats,
                    'timestamp': datetime.now().isoformat()
                }, f, indent=2)

            logger.info(f"STATS Scraping log saved: {log_file}")

        except Exception as e:
            logger.error(f"ERROR: Error saving log: {e}")


# Example usage
async def main():
    """Test the photo scraper"""
    scraper = PhotoScraper("test_website")

    # Test URLs
    test_urls = [
        "https://example.com",  # Replace with actual test URLs
    ]

    summary = await scraper.scrape_website_images(test_urls)

    logger.info("\nCOMPLETE Photo scraping completed!")
    logger.info(f"STATS Website: {summary['website_name']}")
    logger.info(f"IMAGES Images found: {summary['total_images_found']}")
    logger.info(f" Images downloaded: {summary['total_images_downloaded']}")
    logger.info(f"SAVED Total size: {summary['total_size_downloaded']} bytes")
    logger.error(f"ERROR: Errors: {summary['errors']}")

    if __name__ == "__main__":
        import io  # Add missing import
        asyncio.run(main())