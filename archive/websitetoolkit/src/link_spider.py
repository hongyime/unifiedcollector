"""
Unified Website Toolkit - Link Spider
Web crawling engine for collecting links from websites with enhanced features
"""
import os
import asyncio
import aiohttp
import requests
import time
import json
import re
import logging
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse
from typing import Set, List, Dict, Optional, Tuple, Union, Any
from datetime import datetime
from collections import defaultdict

from config import (
    LINK_PATTERNS, DEFAULT_SETTINGS, save_config, get_config
)
from utils import (
    get_domain_name, normalize_url, extract_links_from_text,
    should_skip_url, is_same_domain, load_json_file, save_json_file,
    append_to_file, format_duration, ProgressTracker, async_sleep_with_jitter
)
from resilience import _SHUTDOWN, _interruptible_sleep

# Setup logger
from logger_config import setup_logger
logger = setup_logger(__name__)

# Optional async dependencies
try:
    from sitemap_parser import SitemapParser
    from pdf_processor import PDFProcessor
    from url_filter import URLFilter
    ENHANCEMENT_AVAILABLE = True
except ImportError:
    ENHANCEMENT_AVAILABLE = False


class LinkSpider:
    """Main link crawling engine with integrated enhanced features"""

    def __init__(self, website_name: str):
        self.website_name = website_name
        self.config = self._load_website_config()
        self.collected_links = defaultdict(set)  # categorized links
        self.visited_urls = set()
        self.failed_urls = set()
        self.crawl_state = {}
        self.discovered_websites = set()  # track discovered external websites
        self.discovered_paths = set()  # track all discovered paths
        self.discovered_subdomains = set()  # track all discovered subdomains
        
        # Initialize enhanced components
        self.url_filter = None
        self.sitemap_parser = None
        self.pdf_processor = None
        
        # Load URL filter if enabled
        if self.config.get('enable_url_filtering', True):
            try:
                from url_filter import URLFilter
                self.url_filter = URLFilter()
                logger.info(f"✓ URL filtering enabled for {website_name}")
            except ImportError:
                logger.warning(f"WARNING: URL filter not available for {website_name}")
        
        # Initialize sitemap parser if enabled
        if self.config.get('enable_sitemap_discovery', True):
            try:
                from sitemap_parser import SitemapParser
                self.sitemap_parser = SitemapParser(
                    timeout=self.config.get('sitemap_timeout', 30),
                    max_sitemaps=self.config.get('sitemap_max_sitemaps', 50)
                )
                logger.info(f"✓ Sitemap discovery enabled for {website_name}")
            except ImportError:
                logger.warning(f"WARNING: Sitemap parser not available for {website_name}")
        
        # Initialize PDF processor if enabled
        if self.config.get('enable_pdf_processing', True):
            try:
                from pdf_processor import PDFProcessor
                self.pdf_processor = PDFProcessor(
                    max_file_size=self.config.get('pdf_max_file_size', 50 * 1024 * 1024),
                    timeout=self.config.get('pdf_timeout', 60),
                    max_pages_per_pdf=self.config.get('pdf_max_pages_per_file', 100)
                )
                logger.info(f"✓ PDF processing enabled for {website_name}")
            except ImportError:
                logger.warning(f"WARNING: PDF processor not available for {website_name}")

        self.stats = {
            'total_pages_crawled': 0,
            'total_links_found': 0,
            'links_by_type': defaultdict(int),
            'total_failed_pages': 0,
            'start_time': None,
            'end_time': None,
            'discovered_websites': 0,
            'discovered_paths': 0,
            'discovered_subdomains': 0,
            'sitemap_urls_discovered': 0,
            'pdf_urls_found': 0,
            'pdf_files_processed': 0,
            'urls_filtered_out': 0,
            'sitemap_pages_processed': 0
        }
        self.progress_tracker = ProgressTracker(f"Link Crawling - {website_name}")
        self.background_tasks = []

    def _load_website_config(self) -> dict:
        for website in get_config().websites:
            if website.get('name') == self.website_name:
                return website
            if website.get('url') and get_domain_name(website['url']) == self.website_name:
                return website
        raise ValueError(f"Website '{self.website_name}' not found in configuration")

    def _get_data_path(self) -> str:
        """Get data storage path for this website"""
        from utils import get_safe_filename
        domain = get_domain_name(self.config['url'])
        safe_domain = get_safe_filename(domain)
        data_path = os.path.join('data', 'link_spider', safe_domain)
        os.makedirs(data_path, exist_ok=True)
        return data_path

    def _get_state_file_path(self) -> str:
        return os.path.join(self._get_data_path(), 'crawl_state.json')

    def _get_links_file_path(self, link_type: str = 'all') -> str:
        return os.path.join(self._get_data_path(), f'collected_links_{link_type}.txt')

    def _load_crawl_state(self):
        state_file = self._get_state_file_path()
        self.crawl_state = load_json_file(state_file, {
            'last_crawl_time': None,
            'total_pages_crawled': 0,
            'total_links_found': 0,
            'visited_urls': [],
            'failed_urls': [],
            'crawl_sessions': [],
            'discovered_paths': [],
            'discovered_subdomains': []
        })
        self.visited_urls = set(self.crawl_state.get('visited_urls', []))
        self.failed_urls = set(self.crawl_state.get('failed_urls', []))
        self.discovered_paths = set(self.crawl_state.get('discovered_paths', []))
        self.discovered_subdomains = set(self.crawl_state.get('discovered_subdomains', []))

    def _save_crawl_state(self):
        self.crawl_state['last_crawl_time'] = datetime.now().isoformat()
        self.crawl_state['total_pages_crawled'] = self.stats['total_pages_crawled']
        self.crawl_state['total_links_found'] = self.stats['total_links_found']
        self.crawl_state['visited_urls'] = list(self.visited_urls)
        self.crawl_state['failed_urls'] = list(self.failed_urls)
        self.crawl_state['discovered_paths'] = list(self.discovered_paths)
        self.crawl_state['discovered_subdomains'] = list(self.discovered_subdomains)
        session_info = {
            'timestamp': datetime.now().isoformat(),
            'pages_crawled': self.stats['total_pages_crawled'],
            'links_found': self.stats['total_links_found'],
            'links_by_type': dict(self.stats['links_by_type']),
            'failed_pages': self.stats['total_failed_pages'],
            'duration': time.time() - self.stats['start_time'] if self.stats['start_time'] else 0,
            'discovered_paths': len(self.discovered_paths),
            'discovered_subdomains': len(self.discovered_subdomains)
        }
        self.crawl_state['crawl_sessions'].append(session_info)
        if len(self.crawl_state['crawl_sessions']) > 10:
            self.crawl_state['crawl_sessions'] = self.crawl_state['crawl_sessions'][-10:]
        save_json_file(self._get_state_file_path(), self.crawl_state)

    def is_social_media_url(self, url: str) -> bool:
        """Check if URL is a social media link that should be completely skipped"""
        url_lower = url.lower()
        social_patterns = [
            r'(instagram\.com|twitter\.com|x\.com|facebook\.com|linkedin\.com|youtube\.com|tiktok\.com)',
            r'(reddit\.com|pinterest\.com|snapchat\.com|discord\.com|telegram\.org|whatsapp\.com)',
            r'(github\.com|gitlab\.com|bitbucket\.org)',
            r'(twitch\.tv|vimeo\.com|dailymotion\.com|rumble\.com)',
            r'(mastodon\.|social\.|micro\.blog)',
            r'(t\.co|bit\.ly|tinyurl\.com)',  # URL shorteners often used for social
            r'(share\.|social\.|feed\.)',  # Social sharing endpoints
        ]
        for pattern in social_patterns:
            if re.search(pattern, url_lower):
                return True
        return False

    def categorize_link(self, url: str) -> str:
        url_lower = url.lower()
        
        # Skip social media links entirely - they should not be processed at all
        if self.is_social_media_url(url):
            return 'social_media_skip'  # Special category to mark for skipping

        file_extensions = [
            r'\.(pdf|doc|docx|xls|xlsx|ppt|pptx)(\?|$)',
            r'\.(zip|rar|7z|tar|gz|bz2)(\?|$)',
            r'\.(mp4|avi|mkv|mov|wmv|flv|webm)(\?|$)',
            r'\.(mp3|wav|flac|aac|ogg|wma)(\?|$)',
            r'\.(exe|msi|dmg|deb|rpm|appimage)(\?|$)'
        ]
        for pattern in file_extensions:
            if re.search(pattern, url_lower):
                return 'file_downloads'

        if url_lower.startswith('mailto:'):
            return 'email_addresses'
        if url_lower.startswith('tel:'):
            return 'phone_numbers'

        base_domain = get_domain_name(self.config['url'])
        if is_same_domain(url, self.config['url']):
            return 'internal_links'
        else:
            return 'external_links'

    def extract_paths_and_subdomains(self, url: str):
        """Extract and store unique paths and subdomains from URLs"""
        try:
            parsed = urlparse(url)
            
            # Extract subdomain
            if parsed.netloc:
                subdomain = parsed.netloc.lower()
                # Only add if it's not the base domain and not empty
                base_domain = get_domain_name(self.config['url']).lower()
                if subdomain != base_domain and subdomain not in ['', 'www.' + base_domain]:
                    self.discovered_subdomains.add(subdomain)
            
            # Extract path (excluding query parameters and fragments)
            if parsed.path and parsed.path != '/' and parsed.path != '':
                # Clean and normalize the path
                path = parsed.path.strip()
                if path and len(path) > 1:  # Ignore root path "/"
                    # Remove trailing slash for consistency
                    if path.endswith('/'):
                        path = path[:-1]
                    self.discovered_paths.add(path)
                        
        except Exception as e:
            pass  # Silently ignore malformed URLs

    def extract_all_links(self, html_content: str, base_url: str) -> Dict[str, Set[str]]:
        categorized_links = defaultdict(set)
        social_media_skipped = 0
        
        try:
            text_links = extract_links_from_text(html_content, base_url)
            # Use lxml instead of html.parser for significant performance improvement
            soup = BeautifulSoup(html_content, 'lxml')

            for link in soup.find_all('a', href=True):
                href = link['href']
                full_url = urljoin(base_url, href)
                normalized_url = normalize_url(full_url)
                
                # Skip social media links completely
                if self.is_social_media_url(normalized_url):
                    social_media_skipped += 1
                    continue
                
                if not should_skip_url(normalized_url, self.visited_urls):
                    category = self.categorize_link(normalized_url)
                    if category != 'social_media_skip':  # Double check
                        categorized_links[category].add(normalized_url)
                        # Extract paths and subdomains
                        self.extract_paths_and_subdomains(normalized_url)

            for form in soup.find_all('form', action=True):
                action = form['action']
                full_url = urljoin(base_url, action)
                normalized_url = normalize_url(full_url)
                
                # Skip social media links completely
                if self.is_social_media_url(normalized_url):
                    social_media_skipped += 1
                    continue
                
                categorized_links['form_actions'].add(normalized_url)
                # Extract paths and subdomains
                self.extract_paths_and_subdomains(normalized_url)

            for iframe in soup.find_all('iframe', src=True):
                src = iframe['src']
                full_url = urljoin(base_url, src)
                normalized_url = normalize_url(full_url)
                
                # Skip social media links completely
                if self.is_social_media_url(normalized_url):
                    social_media_skipped += 1
                    continue
                
                categorized_links['embedded_content'].add(normalized_url)
                # Extract paths and subdomains
                self.extract_paths_and_subdomains(normalized_url)

            for script in soup.find_all('script', src=True):
                src = script['src']
                full_url = urljoin(base_url, src)
                normalized_url = normalize_url(full_url)
                
                # Skip social media links completely
                if self.is_social_media_url(normalized_url):
                    social_media_skipped += 1
                    continue
                
                categorized_links['script_sources'].add(normalized_url)
                # Extract paths and subdomains
                self.extract_paths_and_subdomains(normalized_url)

            for link_tag in soup.find_all('link', href=True):
                href = link_tag['href']
                full_url = urljoin(base_url, href)
                normalized_url = normalize_url(full_url)
                
                # Skip social media links completely
                if self.is_social_media_url(normalized_url):
                    social_media_skipped += 1
                    continue
                
                rel = link_tag.get('rel', [''])[0]
                if rel == 'stylesheet':
                    categorized_links['stylesheets'].add(normalized_url)
                else:
                    categorized_links['other_resources'].add(normalized_url)
                # Extract paths and subdomains
                self.extract_paths_and_subdomains(normalized_url)

            for url in text_links:
                # Skip social media links completely
                if self.is_social_media_url(url):
                    social_media_skipped += 1
                    continue
                
                category = self.categorize_link(url)
                if category != 'social_media_skip':  # Double check
                    categorized_links[category].add(url)
                    # Extract paths and subdomains
                    self.extract_paths_and_subdomains(url)

            # Log how many social media links were skipped
            if social_media_skipped > 0:
                logger.info(f"FILTER: Skipped {social_media_skipped} social media links")

            # Apply URL filtering if enabled
            if self.url_filter:
                filtered_links = {}
                total_blocked = 0
                
                for category, links in categorized_links.items():
                    if links:
                        allowed_urls, blocked_urls = self.url_filter.filter_urls(list(links))
                        filtered_links[category] = set(allowed_urls)
                        total_blocked += len(blocked_urls)
                    else:
                        filtered_links[category] = set()
                
                if total_blocked > 0:
                    self.stats['urls_filtered_out'] += total_blocked
                    logger.info(f"FILTER: Blocked {total_blocked} URLs on this page")
                
                categorized_links = filtered_links

            # Detect and categorize PDFs
            if self.pdf_processor:
                pdf_urls = set()
                for category, links in categorized_links.items():
                    if category != 'pdfs':  # Don't double-process
                        pdfs_in_category = {url for url in links if self.pdf_processor.is_pdf_url(url)}
                        if pdfs_in_category:
                            # Remove PDFs from original category and add to PDF category
                            categorized_links[category] -= pdfs_in_category
                            pdf_urls.update(pdfs_in_category)
                
                if pdf_urls:
                    categorized_links['pdfs'] = categorized_links.get('pdfs', set()) | pdf_urls
                    self.stats['pdf_urls_found'] += len(pdf_urls)
                    logger.info(f"PDF: Detected {len(pdf_urls)} PDF URLs on this page")

        except Exception as e:
            logger.warning(f"WARNING: Error extracting links: {e}")

        return categorized_links

    async def _process_sitemap_pdfs(self, pdf_urls: List[str]):
        """Process PDFs found in sitemaps concurrently"""
        if not self.pdf_processor:
            return
        
        try:
            # We use process_pdf_urls directly which is already optimized with Semaphore
            result = await self.pdf_processor.process_pdf_urls(pdf_urls, self.website_name)
            self.stats['pdf_files_processed'] += result.get('total_pdfs', 0)
            logger.info(f"PDF: Processed {result.get('total_pdfs', 0)} PDFs from sitemaps")
        except Exception as e:
            logger.error(f"ERROR: Sitemap PDF processing failed: {e}")

    async def crawl_website(self, max_depth: int = 3, max_pages: int = 100):
        """Enhanced crawling with sitemap discovery and URL filtering"""
        
        # Phase 1: Discover sitemaps first
        sitemap_urls = []
        if self.sitemap_parser:
            logger.info(f"🗺️  PHASE 1: Discovering sitemaps for {self.website_name}")
            try:
                sitemap_result = await self.sitemap_parser.discover_and_parse_all(self.config['url'])
                sitemap_urls = sitemap_result.get('urls', [])
                
                # Add sitemap URLs to our discovery
                if sitemap_urls:
                    logger.info(f"SITEMAP: Found {len(sitemap_urls)} URLs from sitemaps")
                    
                    # Filter sitemap URLs
                    if self.url_filter:
                        allowed_urls, blocked_urls = self.url_filter.filter_urls(sitemap_urls)
                        logger.info(f"FILTER: Allowed {len(allowed_urls)}, blocked {len(blocked_urls)} from sitemap")
                        sitemap_urls = allowed_urls
                        self.stats['urls_filtered_out'] += len(blocked_urls)
                    
                    # Add to internal links for crawling
                    self.collected_links['internal_links'].update(sitemap_urls)
                    self.stats['sitemap_urls_discovered'] = len(sitemap_urls)
                    
                # Process PDFs found in sitemaps
                sitemap_pdfs = sitemap_result.get('pdfs', [])
                if sitemap_pdfs and self.pdf_processor:
                    logger.info(f"PDF: Found {len(sitemap_pdfs)} PDFs in sitemaps")
                    self.stats['pdf_urls_found'] += len(sitemap_pdfs)
                    # Process PDFs in background
                    task = asyncio.create_task(self._process_sitemap_pdfs(sitemap_pdfs))
                    self.background_tasks.append(task)
                    
                # Add sitemap images
                sitemap_images = sitemap_result.get('images', [])
                if sitemap_images:
                    self.collected_links['images'].update(sitemap_images)
                
            except Exception as e:
                logger.warning(f"WARNING: Sitemap discovery failed: {e}")
        
        # Phase 2: Traditional crawling with enhancements
        logger.info(f"🕷️  PHASE 2: Traditional crawling for {self.website_name}")
        
        # Use existing crawling logic
        urls_to_visit = [(self.config['url'], 0)]  # (url, depth)
        
        # Add sitemap URLs to visit if any
        for s_url in sitemap_urls:
            urls_to_visit.append((s_url, 0))

        base_domain = get_domain_name(self.config['url'])
        pages_crawled = 0
        metadata_list = []

        # Optimize connection pooling for higher throughput
        connector = aiohttp.TCPConnector(limit=50, limit_per_host=10, enable_cleanup_closed=True)
        timeout = aiohttp.ClientTimeout(total=60, connect=15)

        async with aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        ) as session:
            
            # Using asyncio.gather to process multiple URLs concurrently at the same depth
            while urls_to_visit and pages_crawled < max_pages:
                # Check for shutdown
                if _SHUTDOWN.is_set():
                    logger.info("[STOPPED] Shutdown requested, stopping crawl")
                    break
                
                # Take up to the concurrent limit from the queue
                concurrent_limit = self.config.get('concurrent_pages_per_site', 5)
                batch = urls_to_visit[:concurrent_limit]
                urls_to_visit = urls_to_visit[concurrent_limit:]
                
                tasks = []
                for current_url, depth in batch:
                    if depth > max_depth:
                        continue
                    tasks.append(self.crawl_page(session, current_url, depth))
                
                if not tasks:
                    continue
                    
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                for i, result in enumerate(results):
                    # Check for shutdown in loop
                    if _SHUTDOWN.is_set():
                        logger.info("[STOPPED] Shutdown requested during crawl")
                        break
                    
                    current_url, depth = batch[i]
                    
                    if isinstance(result, Exception):
                        logger.error(f"ERROR: Exception while crawling {current_url}: {result}")
                        self.stats['total_failed_pages'] += 1
                        self.progress_tracker.update(1, False)
                        continue
                        
                    success, page_links, metadata = result

                    if success:
                        pages_crawled += 1
                        self.stats['total_pages_crawled'] += 1
                        metadata_list.append(metadata)

                        # Add links to collections
                        for category, links in page_links.items():
                            self.collected_links[category].update(links)
                            self.stats['links_by_type'][category] += len(links)

                        # Update progress
                        self.progress_tracker.update(1, True)

                        # If not at max depth, add internal links for further crawling
                        if depth < max_depth:
                            internal_links = page_links.get('internal_links', set())
                            for link in internal_links:
                                if (
                                    link not in self.visited_urls and
                                    get_domain_name(link) == base_domain and
                                    not should_skip_url(link, self.visited_urls)
                                ):
                                    urls_to_visit.append((link, depth + 1))

                    else:
                        self.stats['total_failed_pages'] += 1
                        self.progress_tracker.update(1, False)

                # Show progress periodically
                if pages_crawled % 10 == 0:
                    total_found = sum(len(links) for links in self.collected_links.values())
                    logger.info(f"STATS Crawled {pages_crawled} pages, found {total_found} total links, {len(self.discovered_paths)} paths, {len(self.discovered_subdomains)} subdomains")
                    import sys
                    sys.stdout.flush()

                # Add delay between requests
                delay = self.config.get('delay_between_requests', DEFAULT_SETTINGS['delay_between_requests'])
                if delay > 0:
                    await async_sleep_with_jitter(delay)

        # Wait for all background tasks to complete
        if self.background_tasks:
            logger.info(f"⏳ Waiting for {len(self.background_tasks)} background tasks to complete...")
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
            self.background_tasks = []

        # Save metadata
        metadata_file = os.path.join(self._get_data_path(), 'page_metadata.json')
        save_json_file(metadata_file, {'pages': metadata_list, 'crawl_summary': self.stats})

        # NEW CODE - Insert links into database
        from db_manager import get_db_manager, _connect

        db = get_db_manager()

        try:
            with _connect(db.db_path) as conn:
                # Get website_id from websites table
                row = conn.execute(
                    'SELECT id FROM websites WHERE name = ?',
                    (self.website_name,)
                ).fetchone()
                
                if not row:
                    logger.warning(f"[WARNING] Website '{self.website_name}' not found in database")
                else:
                    website_id = row[0]
                    
                    # Insert links in batches of 500 to avoid memory issues
                    for category, links in self.collected_links.items():
                        if not links or category == 'social_media_skip':
                            continue
                        
                        # Prepare batch data
                        batch = [
                            (website_id, url, category, datetime.now().isoformat())
                            for url in links
                        ]
                        
                        # Insert in chunks of 500
                        for i in range(0, len(batch), 500):
                            chunk = batch[i:i+500]
                            conn.executemany('''
                                INSERT OR IGNORE INTO links (website_id, url, link_type, discovered_date)
                                VALUES (?, ?, ?, ?)
                            ''', chunk)
                        
                        logger.info(f"[DB] Inserted {len(links)} {category} links for {self.website_name}")
                    
                    # Update total_links_found in websites table
                    total = sum(len(v) for v in self.collected_links.values() if v)
                    conn.execute('''
                        UPDATE websites 
                        SET total_links_found = ?, last_crawled = ?
                        WHERE id = ?
                    ''', (total, datetime.now().isoformat(), website_id))
                    
                    conn.commit()
                    logger.info(f"[DB] Updated website record: {total} total links")
                    
        except Exception as e:
            logger.error(f"[ERROR] Failed to insert links into database: {e}")

        return pages_crawled

    async def crawl_page(self, session: aiohttp.ClientSession, url: str, depth: int) -> Tuple[bool, Dict[str, Set[str]], Dict[str, str]]:
        """Crawl a single page and extract links"""
        if url in self.visited_urls:
            return False, {}, {}

        self.visited_urls.add(url)

        try:
            logger.info(f"SPIDER Crawling: {url}")

            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    logger.error(f"ERROR: Failed to fetch {url}: HTTP {response.status}")
                    self.failed_urls.add(url)
                    return False, {}, {}

                content_type = response.headers.get('Content-Type', '').lower()
                if 'text/html' not in content_type:
                    return False, {}, {}

                html_content = await response.text()

                # Extract links and metadata
                categorized_links = self.extract_all_links(html_content, url)
                metadata = self.extract_metadata(html_content)
                metadata['url'] = url
                metadata['crawl_time'] = datetime.now().isoformat()
                metadata['depth'] = depth

                # Discover new websites from external links
                self._discover_new_websites(categorized_links)

                # Count links found
                total_links = sum(len(links) for links in categorized_links.values())
                logger.info(f"LINK Found {total_links} links on {url}")

                return True, categorized_links, metadata

        except Exception as e:
            logger.error(f"ERROR: Error crawling {url}: {e}")
            self.failed_urls.add(url)
            return False, {}, {}

    def extract_metadata(self, html_content: str) -> Dict[str, str]:
        """Extract metadata from HTML"""
        metadata = {}
        try:
            soup = BeautifulSoup(html_content, 'lxml')
            title_tag = soup.find('title')
            if title_tag:
                metadata['title'] = title_tag.get_text().strip()

            meta_desc = soup.find('meta', attrs={'name': 'description'})
            if meta_desc:
                metadata['description'] = meta_desc.get('content', '').strip()

            og_title = soup.find('meta', property='og:title')
            if og_title:
                metadata['og_title'] = og_title.get('content', '').strip()
        except Exception as e:
            logger.warning(f"WARNING: Error extracting metadata: {e}")
        return metadata

    def _extract_domain_from_url(self, url: str) -> str:
        """Extract clean domain name from URL"""
        try:
            domain = get_domain_name(url)
            if domain.startswith('www.'):
                domain = domain[4:]
            return domain
        except Exception:
            return url

    def _extract_base_website_from_api_url(self, url: str) -> Optional[str]:
        """Extract base website URL from WordPress API or other API endpoints"""
        try:
            if '/wp-json/' in url:
                return url.split('/wp-json/')[0]
            if '/wp/v2/' in url:
                return url.split('/wp/v2/')[0]
            
            api_patterns = ['/api/v1/', '/api/v2/', '/api/', '/rest/']
            for pattern in api_patterns:
                if pattern in url:
                    return url.split(pattern)[0]
            return None
        except Exception:
            return None

    def _is_valid_website_url(self, url: str) -> bool:
        """Check if URL represents a valid website to add to config"""
        try:
            if not url.startswith(('http://', 'https://')):
                return False
            if self.is_social_media_url(url):
                return False
            
            base_url = self._extract_base_website_from_api_url(url)
            if base_url:
                url = base_url
            
            domain = get_domain_name(url)
            if not domain or len(domain) < 3:
                return False
            
            skip_patterns = ['localhost', '127.0.0.1', 'cdn.', 'static.', 'assets.']
            for pattern in skip_patterns:
                if pattern in domain.lower():
                    return False
            
            return True
        except Exception:
            return False

    def _discover_new_websites(self, categorized_links: Dict[str, Set[str]]):
        """Enhanced website discovery"""
        external_links = categorized_links.get('external_links', set())
        for url in external_links:
            if self.is_social_media_url(url):
                continue
            
            if self._is_valid_website_url(url):
                try:
                    parsed = urlparse(url)
                    final_url = f"{parsed.scheme}://{parsed.netloc}"
                    domain = self._extract_domain_from_url(final_url)
                    
                    if not self._is_website_in_config(domain, final_url):
                        self.discovered_websites.add(final_url)
                except Exception:
                    continue

    def _is_website_in_config(self, domain: str, url: str) -> bool:
        """Check if website already exists in configuration"""
        config_obj = get_config()
        for website in config_obj.websites:
            if isinstance(website, str):
                if self._urls_are_equivalent(website, url): return True
            elif isinstance(website, dict):
                if self._urls_are_equivalent(website.get('url', ''), url): return True
        return False

    def _urls_are_equivalent(self, url1: str, url2: str) -> bool:
        norm1 = self._normalize_url_for_comparison(url1)
        norm2 = self._normalize_url_for_comparison(url2)
        return norm1 == norm2

    def _normalize_url_for_comparison(self, url: str) -> str:
        try:
            normalized = url.lower().split('://')[-1]
            if normalized.startswith('www.'): normalized = normalized[4:]
            if normalized.endswith('/'): normalized = normalized[:-1]
            return normalized
        except Exception:
            return url.lower()

    def _add_discovered_websites_to_config(self, auto_enable: bool = False) -> int:
        """Add discovered websites to the configuration"""
        if not self.discovered_websites:
            return 0
        
        added_count = 0
        config_obj = get_config()
        for url in self.discovered_websites:
            domain = self._extract_domain_from_url(url)
            if not self._is_website_in_config(domain, url):
                config_obj.websites.append({
                    'name': domain,
                    'url': url,
                    'enabled': auto_enable,
                    'created_at': datetime.now().isoformat()
                })
                added_count += 1
        
        if added_count > 0:
            config_obj.save_config()
        return added_count

    def finalize_crawling(self, auto_add_websites: bool = True, auto_enable_websites: bool = False) -> Dict[str, Any]:
        """Finalize crawling results"""
        summary = {
            'total_links_found': sum(len(links) for links in self.collected_links.values()),
            'links_by_type': {k: len(v) for k, v in self.collected_links.items()},
            'discovered_websites_count': len(self.discovered_websites),
            'discovered_websites': list(self.discovered_websites),
            'discovered_paths_count': len(self.discovered_paths),
            'discovered_paths': list(self.discovered_paths),
            'discovered_subdomains_count': len(self.discovered_subdomains),
            'discovered_subdomains': list(self.discovered_subdomains),
            'websites_added_to_config': 0
        }
        
        if auto_add_websites:
            summary['websites_added_to_config'] = self._add_discovered_websites_to_config(auto_enable_websites)
            
        return summary

    def save_collected_links(self) -> None:
        """Save collected links to files"""
        data_path = self._get_data_path()
        all_links_file = os.path.join(data_path, 'collected_links_all.txt')
        with open(all_links_file, 'w', encoding='utf-8') as f:
            for category, links in self.collected_links.items():
                if category == 'social_media_skip': continue
                f.write(f"\n## {category.upper()}\n")
                for link in sorted(links):
                    f.write(f"{link}\n")

    def generate_report(self) -> str:
        """Generate crawling report string"""
        total_links = sum(len(links) for links in self.collected_links.values())
        return f"Spider Report: {self.website_name}\nLinks Found: {total_links}\nPages: {self.stats['total_pages_crawled']}"

    async def start_crawling(self):
        """Main entry point for starting a crawl"""
        self._load_crawl_state()
        self.stats['start_time'] = time.time()
        try:
            await self.crawl_website()
        finally:
            self.stats['end_time'] = time.time()
            self.save_collected_links()
            self._save_crawl_state()

    async def crawl_website_urls(self, urls: List[str], auto_add_websites: bool = True, auto_enable_websites: bool = False) -> Dict[str, Any]:
        """Wrapper for external calls to crawl multiple URLs"""
        if urls: self.config['url'] = urls[0]
        await self.crawl_website()
        self.save_collected_links()
        return self.finalize_crawling(auto_add_websites, auto_enable_websites)

def start_link_crawling(website_name: str):
    """Sync wrapper for link crawling"""
    spider = LinkSpider(website_name)
    asyncio.run(spider.start_crawling())
