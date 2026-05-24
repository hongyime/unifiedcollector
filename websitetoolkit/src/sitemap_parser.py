"""
Unified Website Toolkit - Sitemap Parser
XML Sitemap detection and parsing for efficient website traversal
"""
import xml.etree.ElementTree as ET
import asyncio
import requests
import logging
from typing import List, Dict, Set, Optional, Tuple, Any
from urllib.parse import urljoin, urlparse
from datetime import datetime
import gzip
import io
import re

# Setup logger
logger = logging.getLogger(__name__)

try:
    import defusedxml.ElementTree as DET
    XML_PARSER = DET
except ImportError as e:
    raise ImportError(
        "defusedxml is required to prevent XXE vulnerabilities in sitemap parsing. "
        "Install it with: pip install defusedxml"
    ) from e

# Optional async dependencies
try:
    import aiohttp
    ASYNC_AVAILABLE = True
except ImportError:
    ASYNC_AVAILABLE = False
    aiohttp = None

from utils import create_session_with_retries, get_domain_name


class SitemapParser:
    """XML Sitemap parser for efficient website discovery"""
    
    def __init__(self, timeout: int = 30, max_sitemaps: int = 50):
        """Initialize sitemap parser
        
        Args:
            timeout: Request timeout in seconds
            max_sitemaps: Maximum number of sitemaps to process
        """
        self.timeout = timeout
        self.max_sitemaps = max_sitemaps
        self.session = create_session_with_retries()
            
        self.processed_sitemaps = set()
        self.discovered_urls = set()
        self.sitemap_stats = {
            'total_sitemaps_found': 0,
            'total_sitemaps_processed': 0,
            'total_urls_discovered': 0,
            'image_urls_found': 0,
            'pdf_urls_found': 0,
            'failed_sitemaps': 0,
            'start_time': None,
            'end_time': None
        }
    
    async def discover_sitemaps(self, base_url: str) -> List[str]:
        """Discover sitemaps for a given website
        
        Returns:
            List of sitemap URLs found
        """
        self.sitemap_stats['start_time'] = datetime.now().isoformat()
        sitemap_urls = set()
        
        try:
            parsed_url = urlparse(base_url)
            base_domain = f"{parsed_url.scheme}://{parsed_url.netloc}"
            
            # Common sitemap locations
            common_paths = [
                '/sitemap.xml',
                '/sitemap_index.xml',
                '/sitemaps.xml',
                '/sitemap/sitemap.xml',
                '/wp-sitemap.xml',
                '/sitemap-index.xml',
                '/robots.txt'  # Check robots.txt for sitemap references
            ]
            
            logger.info(f"🗺️  SITEMAP: Discovering sitemaps for {get_domain_name(base_url)}")
            
            # Check common sitemap locations
            for path in common_paths:
                sitemap_url = urljoin(base_domain, path)
                
                if path == '/robots.txt':
                    # Parse robots.txt for sitemap references
                    robot_sitemaps = await self._parse_robots_txt(sitemap_url)
                    sitemap_urls.update(robot_sitemaps)
                else:
                    # Check if sitemap exists
                    if await self._check_sitemap_exists(sitemap_url):
                        sitemap_urls.add(sitemap_url)
            
            self.sitemap_stats['total_sitemaps_found'] = len(sitemap_urls)
            logger.info(f"SITEMAP: Found {len(sitemap_urls)} sitemaps")
            
            return list(sitemap_urls)
            
        except Exception as e:
            logger.warning(f"WARNING: Error discovering sitemaps for {base_url}: {e}")
            return []
    
    async def _check_sitemap_exists(self, sitemap_url: str) -> bool:
        """Check if a sitemap URL exists and is valid"""
        try:
            if self.session and not self.session.closed:
                async with self.session.head(sitemap_url, timeout=self.timeout) as response:
                    if response.status == 200:
                        content_type = response.headers.get('content-type', '').lower()
                        return any(ct in content_type for ct in ['xml', 'text/plain'])
            else:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                    async with session.head(sitemap_url) as response:
                        if response.status == 200:
                            content_type = response.headers.get('content-type', '').lower()
                            return any(ct in content_type for ct in ['xml', 'text/plain'])
            return False
        except Exception:
            return False
    
    async def _parse_robots_txt(self, robots_url: str) -> List[str]:
        """Parse robots.txt file for sitemap references"""
        sitemaps = []
        try:
            if self.session and not self.session.closed:
                async with self.session.get(robots_url, timeout=self.timeout) as response:
                    if response.status == 200:
                        content = await response.text()
                        
                        # Find sitemap entries
                        sitemap_pattern = re.compile(r'Sitemap:\s*(https?://\S+)', re.IGNORECASE)
                        matches = sitemap_pattern.findall(content)
                        sitemaps.extend(matches)
            else:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                    async with session.get(robots_url) as response:
                        if response.status == 200:
                            content = await response.text()
                            
                            # Find sitemap entries
                            sitemap_pattern = re.compile(r'Sitemap:\s*(https?://\S+)', re.IGNORECASE)
                            matches = sitemap_pattern.findall(content)
                            sitemaps.extend(matches)
            
        except Exception as e:
            logger.warning(f"WARNING: Error parsing robots.txt at {robots_url}: {e}")
        
        return sitemaps
    
    async def parse_sitemap(self, sitemap_url: str) -> Dict[str, Any]:
        """Parse a single sitemap and extract URLs
        
        Returns:
            Dictionary containing parsed URLs and metadata
        """
        if sitemap_url in self.processed_sitemaps:
            return {'urls': [], 'images': [], 'pdfs': [], 'nested_sitemaps': []}
        
        self.processed_sitemaps.add(sitemap_url)
        
        try:
            logger.info(f"SITEMAP: Parsing {sitemap_url}")
            
            # Download sitemap content
            content = await self._download_sitemap(sitemap_url)
            if not content:
                self.sitemap_stats['failed_sitemaps'] += 1
                return {'urls': [], 'images': [], 'pdfs': [], 'nested_sitemaps': []}
            
            # Parse XML
            parsed_data = self._parse_xml_content(content)
            self.sitemap_stats['total_sitemaps_processed'] += 1
            
            # Update statistics
            self.sitemap_stats['total_urls_discovered'] += len(parsed_data['urls'])
            self.sitemap_stats['image_urls_found'] += len(parsed_data['images'])
            self.sitemap_stats['pdf_urls_found'] += len(parsed_data['pdfs'])
            
            return parsed_data
            
        except Exception as e:
            logger.error(f"ERROR: Failed to parse sitemap {sitemap_url}: {e}")
            self.sitemap_stats['failed_sitemaps'] += 1
            return {'urls': [], 'images': [], 'pdfs': [], 'nested_sitemaps': []}
    
    async def _download_sitemap(self, sitemap_url: str) -> Optional[bytes]:
        """Download sitemap content with support for gzip compression"""
        try:
            if self.session and not self.session.closed:
                async with self.session.get(sitemap_url, timeout=self.timeout) as response:
                    if response.status != 200:
                        return None
                    
                    content = await response.read()
            else:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                    async with session.get(sitemap_url) as response:
                        if response.status != 200:
                            return None
                        
                        content = await response.read()
            
            # Check if content is gzipped
            if sitemap_url.endswith('.gz') or content.startswith(b'\x1f\x8b'):
                try:
                    content = gzip.decompress(content)
                except Exception:
                    pass  # If decompression fails, use original content
            
            return content
        
        except Exception as e:
            logger.warning(f"WARNING: Error downloading sitemap {sitemap_url}: {e}")
            return None
    
    def _parse_xml_content(self, content: bytes) -> Dict[str, Any]:
        """Parse XML sitemap content and extract URLs"""
        urls = []
        images = []
        pdfs = []
        nested_sitemaps = []
        
        try:
            # Parse XML safely
            root = XML_PARSER.fromstring(content)
            
            # Handle different namespaces
            namespaces = {
                'sitemap': 'http://www.sitemaps.org/schemas/sitemap/0.9',
                'image': 'http://www.google.com/schemas/sitemap-image/1.1',
                'news': 'http://www.google.com/schemas/sitemap-news/0.9',
                'video': 'http://www.google.com/schemas/sitemap-video/1.1'
            }
            
            # Check if this is a sitemap index
            if root.tag.endswith('sitemapindex'):
                # Parse sitemap index
                for sitemap_elem in root.findall('.//sitemap:sitemap', namespaces):
                    loc_elem = sitemap_elem.find('sitemap:loc', namespaces)
                    if loc_elem is not None and loc_elem.text:
                        nested_sitemaps.append(loc_elem.text.strip())
            
            # Parse regular sitemap URLs
            for url_elem in root.findall('.//sitemap:url', namespaces):
                loc_elem = url_elem.find('sitemap:loc', namespaces)
                if loc_elem is not None and loc_elem.text:
                    url = loc_elem.text.strip()
                    
                    # Categorize URLs
                    if self._is_image_url(url):
                        images.append(url)
                    elif self._is_pdf_url(url):
                        pdfs.append(url)
                    else:
                        urls.append(url)
                    
                    # Check for image elements within this URL
                    for image_elem in url_elem.findall('.//image:image', namespaces):
                        image_loc = image_elem.find('image:loc', namespaces)
                        if image_loc is not None and image_loc.text:
                            images.append(image_loc.text.strip())
            
            # Also try to parse without namespaces (some sitemaps don't use them properly)
            if not urls and not images and not nested_sitemaps:
                for url_elem in root.findall('.//url'):
                    loc_elem = url_elem.find('loc')
                    if loc_elem is not None and loc_elem.text:
                        url = loc_elem.text.strip()
                        
                        if self._is_image_url(url):
                            images.append(url)
                        elif self._is_pdf_url(url):
                            pdfs.append(url)
                        else:
                            urls.append(url)
                
                # Check for sitemap index without namespace
                for sitemap_elem in root.findall('.//sitemap'):
                    loc_elem = sitemap_elem.find('loc')
                    if loc_elem is not None and loc_elem.text:
                        nested_sitemaps.append(loc_elem.text.strip())
        
        except ET.ParseError as e:
            logger.warning(f"WARNING: XML parsing error: {e}")
        except Exception as e:
            logger.warning(f"WARNING: Error parsing sitemap content: {e}")
        
        return {
            'urls': urls,
            'images': images,
            'pdfs': pdfs,
            'nested_sitemaps': nested_sitemaps
        }
    
    def _is_image_url(self, url: str) -> bool:
        """Check if URL points to an image"""
        image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp'}
        return any(url.lower().endswith(ext) for ext in image_extensions)
    
    def _is_pdf_url(self, url: str) -> bool:
        """Check if URL points to a PDF"""
        return url.lower().endswith('.pdf')
    
    async def parse_all_sitemaps(self, sitemap_urls: List[str], recursive: bool = True) -> Dict[str, Any]:
        """Parse multiple sitemaps recursively
        
        Args:
            sitemap_urls: List of sitemap URLs to parse
            recursive: Whether to follow nested sitemaps
            
        Returns:
            Combined results from all sitemaps
        """
        all_urls = set()
        all_images = set()
        all_pdfs = set()
        pending_sitemaps = set(sitemap_urls)
        
        while pending_sitemaps and len(self.processed_sitemaps) < self.max_sitemaps:
            current_batch = list(pending_sitemaps)[:10]  # Process in batches
            pending_sitemaps -= set(current_batch)
            
            # Parse current batch
            tasks = [self.parse_sitemap(url) for url in current_batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for result in results:
                if isinstance(result, dict):
                    all_urls.update(result['urls'])
                    all_images.update(result['images'])
                    all_pdfs.update(result['pdfs'])
                    
                    # Add nested sitemaps if recursive parsing is enabled
                    if recursive:
                        for nested_sitemap in result['nested_sitemaps']:
                            if nested_sitemap not in self.processed_sitemaps:
                                pending_sitemaps.add(nested_sitemap)
        
        self.sitemap_stats['end_time'] = datetime.now().isoformat()
        
        return {
            'urls': list(all_urls),
            'images': list(all_images),
            'pdfs': list(all_pdfs),
            'stats': self.sitemap_stats.copy()
        }
    
    async def discover_and_parse_all(self, base_url: str) -> Dict[str, Any]:
        """Complete sitemap discovery and parsing workflow
        
        Args:
            base_url: Base URL of the website
            
        Returns:
            Combined results from sitemap discovery and parsing
        """
        try:
            # Discover sitemaps
            sitemap_urls = await self.discover_sitemaps(base_url)
            
            if not sitemap_urls:
                logger.info(f"SITEMAP: No sitemaps found for {get_domain_name(base_url)}")
                return {
                    'urls': [],
                    'images': [],
                    'pdfs': [],
                    'stats': self.sitemap_stats.copy()
                }
            
            # Parse all discovered sitemaps
            result = await self.parse_all_sitemaps(sitemap_urls, recursive=True)
            
            logger.info(f"SITEMAP: Completed parsing - {len(result['urls'])} URLs, "
                  f"{len(result['images'])} images, {len(result['pdfs'])} PDFs")
            
            return result
            
        except Exception as e:
            logger.error(f"ERROR: Sitemap discovery and parsing failed for {base_url}: {e}")
            return {
                'urls': [],
                'images': [],
                'pdfs': [],
                'stats': self.sitemap_stats.copy()
            }
    
    def get_stats(self) -> Dict[str, Any]:
        """Get sitemap parsing statistics"""
        return self.sitemap_stats.copy()
    
    def reset_stats(self):
        """Reset sitemap parsing statistics"""
        self.processed_sitemaps.clear()
        self.discovered_urls.clear()
        self.sitemap_stats = {
            'total_sitemaps_found': 0,
            'total_sitemaps_processed': 0,
            'total_urls_discovered': 0,
            'image_urls_found': 0,
            'pdf_urls_found': 0,
            'failed_sitemaps': 0,
            'start_time': None,
            'end_time': None
        }