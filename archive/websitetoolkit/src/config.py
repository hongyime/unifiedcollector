"""
Unified Website Toolkit Configuration
Manages settings, file paths, and website configurations
"""
import os
import json
import logging
from typing import List, Dict, Any, Optional, Tuple
from db_manager import get_db_manager

logger = logging.getLogger(__name__)

# Base directories — anchored to project root (one level above src/)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, 'data')
DOWNLOADS_DIR = os.path.join(BASE_DIR, 'downloads')

# Ensure directories exist
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Data files - All tracking and progress files go in DATA_DIR
SCRAPED_HASHES_FILE = os.path.join(DATA_DIR, 'scraped_photos_hashes.json')
CRAWLED_LINKS_FILE = os.path.join(DATA_DIR, 'crawled_links.txt')
SPIDER_PROGRESS_FILE = os.path.join(DATA_DIR, 'spider_progress.json')
PHOTO_SCRAPER_STATE_FILE = os.path.join(DATA_DIR, 'photo_scraper_state.json')
WEBSITES_CONFIG_FILE = os.path.join(DATA_DIR, 'websites_config.json')

# Log files - All logs go in DATA_DIR
ERROR_LOG_FILE = os.path.join(DATA_DIR, 'errors.log')
SCRAPING_LOG_FILE = os.path.join(DATA_DIR, 'scraping.log')

# Progress and state files - All in DATA_DIR
SCRAPING_PROGRESS_FILE = os.path.join(DATA_DIR, 'scraping_progress.json')
DOWNLOAD_PROGRESS_FILE = os.path.join(DATA_DIR, 'download_progress.json')

# Output files - All data exports go in DATA_DIR
LINKS_EXPORT_FILE = os.path.join(DATA_DIR, 'extracted_links.txt')
STATISTICS_FILE = os.path.join(DATA_DIR, 'statistics.json')

# Supported image extensions (matching Telegram toolkit)
SUPPORTED_IMAGE_EXTENSIONS = {
    'jpeg', 'jpg', 'png', 'webp', 'gif'
}

# Excluded extensions (matching Telegram toolkit)
EXCLUDED_EXTENSIONS = {
    'webm'  # Explicitly excluded like in Telegram toolkit
}

# Image MIME types
SUPPORTED_IMAGE_MIMES = {
    'image/jpeg', 'image/jpg', 'image/png',
    'image/webp', 'image/gif'
}

# Link extraction patterns
LINK_PATTERNS = {
    'http_urls': r'https?://[^\s<>"]+',
    'email': r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
    'phone': r'(?:\+?1[-.\s]?)?\(?[0-9]{3}\)?[-.\s]?[0-9]{3}[-.\s]?[0-9]{4}',
    'social_media': r'(?:https?://)?(?:www\.)?(?:facebook|twitter|instagram|linkedin|youtube|tiktok)\.com/[^\s<>"]+',
    'file_urls': r'https?://[^\s<>"]+\.(?:pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar|tar|gz)(?:\?[^\s<>"]*)?'
}

# Default scraping settings
DEFAULT_SETTINGS = {
    'max_depth': 3,
    'max_images': 1000,
    'max_pages': 100,
    'max_image_size': 10 * 1024 * 1024,  # 10MB
    'max_image_width': 2048,
    'max_image_height': 2048,
    'delay_between_requests': 1.0,
    'timeout': 30,
    'max_retries': 3,
    'concurrent_websites': 5,
    'concurrent_pages_per_site': 3,
    'respect_robots_txt': True,
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'max_file_size_mb': 50,
    'save_progress_interval': 1,
    
    # New: URL filtering settings
    'enable_url_filtering': True,
    'url_filter_config_file': None,  # Uses default if None
    
    # New: Sitemap discovery settings
    'enable_sitemap_discovery': True,
    'sitemap_max_depth': True,  # Follow nested sitemaps
    'sitemap_max_sitemaps': 50,
    'sitemap_timeout': 30,
    
    # New: PDF processing settings
    'enable_pdf_processing': True,
    'pdf_max_file_size': 50 * 1024 * 1024,  # 50MB
    'pdf_max_pages_per_file': 100,
    'pdf_timeout': 60,
    'pdf_conversion_dpi': 300,
    
    # Enhanced subdomain and path tracking
    'track_subdomains': True,
    'track_all_paths': True,
    'max_subdomains_per_domain': 1000,
    'max_paths_per_domain': 10000,
}

class Config:
    def __init__(self):
        self.settings = DEFAULT_SETTINGS.copy()
        self.websites = []
        self._ensure_directories()
        self._load_config()

    def _ensure_directories(self):
        for directory in [DATA_DIR, DOWNLOADS_DIR]:
            os.makedirs(directory, exist_ok=True)

    def _load_config(self):
        """Load configuration from DB"""
        try:
            db = get_db_manager()
            settings = db.get_settings()
            if settings:
                self.settings.update(settings)
            else:
                db.save_settings(self.settings)
                
            websites = db.get_websites()
            if websites:
                self.websites = websites
            else:
                self._create_default_config()
        except Exception as e:
            logger.error("Error loading config from DB: %s", e)
            self._create_default_config()

    def _create_default_config(self):
        """Create default configuration file/db entry"""
        default_website = {
            'name': 'example_site',
            'url': 'https://example.com',
            'enabled': False,
            'max_depth': 2,
            'custom_headers': {},
            'authentication': {},
            'notes': 'Example website configuration',
            'enable_sitemap_discovery': True,
            'enable_pdf_processing': True,
            'enable_url_filtering': True,
            'custom_url_filters': [],
            'pdf_settings': {
                'max_file_size': None,
                'max_pages': None,
                'convert_to_images': True
            },
            'sitemap_settings': {
                'follow_nested': True,
                'max_sitemaps': None
            }
        }
        self.websites = [default_website]
        try:
            db = get_db_manager()
            db.save_settings(self.settings)
            db.save_websites(self.websites)
            print("SUCCESS: Created default config in DB")
        except Exception as e:
            logger.error("Error creating default config: %s", e)

    def save_config(self):
        """Save current configuration to DB"""
        try:
            db = get_db_manager()
            db.save_settings(self.settings)
            db.save_websites(self.websites)
            return True
        except Exception as e:
            logger.error("Error saving config to DB: %s", e)
            return False

    def _normalize_url_for_comparison(self, url: str) -> str:
        """Normalize URL for consistent comparison"""
        try:
            # Remove protocol and trailing slash for comparison
            normalized = url.lower()
            if normalized.startswith('https://'):
                normalized = normalized[8:]
            elif normalized.startswith('http://'):
                normalized = normalized[7:]
            
            # Remove trailing slash
            if normalized.endswith('/'):
                normalized = normalized[:-1]
            
            # Remove www prefix for comparison
            if normalized.startswith('www.'):
                normalized = normalized[4:]
                
            return normalized
        except Exception:
            return url.lower()

    def _urls_are_equivalent(self, url1: str, url2: str) -> bool:
        """Check if two URLs are equivalent (same website)"""
        try:
            norm1 = self._normalize_url_for_comparison(url1)
            norm2 = self._normalize_url_for_comparison(url2)
            return norm1 == norm2
        except Exception:
            return url1.lower() == url2.lower()

    def _is_duplicate_website(self, name: str, url: str) -> Tuple[bool, str]:
        normalized_name = name.lower()
        for site in self.websites:
            if site.get('name', '').lower() == normalized_name:
                return True, f"Website name '{name}' already exists"
            site_url = site.get('url', '')
            if site_url and self._urls_are_equivalent(site_url, url):
                return True, f"URL {url} already exists as {site_url}"
            if site_url:
                existing_domain = site_url.split('://')[-1].split('/')[0].lower()
                if existing_domain.startswith('www.'):
                    existing_domain = existing_domain[4:]
                test_domain = url.split('://')[-1].split('/')[0].lower()
                if test_domain.startswith('www.'):
                    test_domain = test_domain[4:]
                if existing_domain == test_domain:
                    return True, f"Domain already exists: {existing_domain} (existing: {site_url}, new: {url})"
        return False, ""

    def add_website(self, name_or_url: str, url: Optional[str] = None, max_depth: Optional[int] = None, simple: bool = False, **kwargs) -> bool:
        if simple or (url is None and name_or_url.startswith(('http://', 'https://'))):
            actual_url = name_or_url if url is None else url
            domain_name = actual_url.split('://')[-1].split('/')[0]
            if domain_name.startswith('www.'):
                domain_name = domain_name[4:]

            is_duplicate, reason = self._is_duplicate_website(domain_name, actual_url)
            if is_duplicate:
                print(f"WARNING: Cannot add website - {reason}")
                return False

            self.websites.append({
                'name': domain_name,
                'url': actual_url,
                'enabled': True,
                'max_depth': self.settings['max_depth'],
                'custom_headers': {},
                'authentication': {},
                'notes': '',
                'created_at': None,
            })
            print(f"SUCCESS: Added website: {actual_url}")
            return self.save_config()
        else:
            name = name_or_url
            actual_url = url or name_or_url

            # Check for duplicates using robust method
            is_duplicate, reason = self._is_duplicate_website(name, actual_url)
            if is_duplicate:
                print(f"WARNING: Cannot add website - {reason}")
                return False

            website_config = {
                'name': name,
                'url': actual_url,
                'enabled': True,
                'max_depth': max_depth or self.settings['max_depth'],
                'custom_headers': kwargs.get('custom_headers', {}),
                'authentication': kwargs.get('authentication', {}),
                'notes': kwargs.get('notes', ''),
                'created_at': kwargs.get('created_at', None)
            }

            self.websites.append(website_config)
            print(f"SUCCESS: Added website: {name} -> {actual_url}")
            return self.save_config()

    def remove_website(self, name_or_url: str) -> bool:
        original_count = len(self.websites)
        self.websites = [
            site for site in self.websites
            if not (site.get('name') == name_or_url or site.get('url') == name_or_url)
        ]
        if len(self.websites) < original_count:
            return self.save_config()
        return False

    def get_enabled_websites(self) -> List[Dict[str, Any]]:
        return [site for site in self.websites if site.get('enabled', True)]

    def update_setting(self, key: str, value: Any) -> bool:
        if key in self.settings:
            self.settings[key] = value
            return self.save_config()
        return False

    def get_setting(self, key: str, default=None):
        return self.settings.get(key, default)

    def get_website_config(self, name_or_url: str) -> Dict[str, Any]:
        for site in self.websites:
            if site.get('name') == name_or_url or site.get('url') == name_or_url:
                return site
        return {}

    def toggle_website(self, name_or_url: str) -> bool:
        for site in self.websites:
            if site.get('name') == name_or_url or site.get('url') == name_or_url:
                site['enabled'] = not site.get('enabled', True)
                return self.save_config()
        return False

# Global configuration instance
config = Config()

def get_config() -> Config:
    return config

def get_setting(key: str, default=None):
    return config.get_setting(key, default)

def get_websites() -> List[Dict[str, Any]]:
    return list(config.websites)

def get_enabled_websites() -> List[Dict[str, Any]]:
    return config.get_enabled_websites()

def save_config() -> bool:
    return config.save_config()


