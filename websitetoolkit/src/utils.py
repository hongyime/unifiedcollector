"""
Unified Website Toolkit Utilities
Shared utility functions for web scraping and link crawling
"""
import os
import re
import json
import hashlib
import time
import asyncio
from datetime import datetime
from typing import List, Dict, Set, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

def get_safe_filename(filename: str, max_length: int = 200) -> str:
    """Generate safe filename for filesystem"""
    # Remove or replace unsafe characters
    safe_chars = re.sub(r'[<>:"/\\|?*]', '_', filename)
    safe_chars = re.sub(r'[^\w\s\-_\.]', '', safe_chars)
    
    # Limit length
    if len(safe_chars) > max_length:
        name, ext = os.path.splitext(safe_chars)
        safe_chars = name[:max_length-len(ext)] + ext
    
    return safe_chars.strip()

def get_domain_name(url: str) -> str:
    """Extract clean domain name from URL"""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if not domain:
            return 'unknown_domain'
        # Remove www. prefix
        if domain.startswith('www.'):
            domain = domain[4:]
        return domain
    except Exception:
        return 'unknown_domain'

def generate_unique_filename(base_filename: str, directory: str) -> str:
    """Generate unique filename to prevent conflicts"""
    full_path = os.path.join(directory, base_filename)
    
    if not os.path.exists(full_path):
        return base_filename
    
    name, ext = os.path.splitext(base_filename)
    counter = 1
    
    while True:
        new_filename = f"{name}_dup{counter}{ext}"
        new_path = os.path.join(directory, new_filename)
        if not os.path.exists(new_path):
            return new_filename
        counter += 1

def calculate_file_hash(file_path: str) -> str:
    """Calculate SHA256 hash of file"""
    hasher = hashlib.sha256()
    try:
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception as e:
        print(f"WARNING: Error calculating hash for {file_path}: {e}")
        return ""

def calculate_content_hash(content: bytes) -> str:
    """Calculate SHA256 hash of content"""
    hasher = hashlib.sha256()
    hasher.update(content)
    return hasher.hexdigest()

def normalize_url(url: str, base_url: str = None) -> str:
    """Normalize URL format - improved version with fragment handling"""
    try:
        # Handle fragment-only URLs (preserve them as-is)
        if url.startswith('#'):
            return url
        
        # Handle special URL schemes (preserve as-is)
        if url.startswith(('mailto:', 'tel:', 'javascript:', 'data:', 'file:')):
            return url
        
        # Handle relative URLs
        if base_url and not url.startswith(('http://', 'https://', '//')):
            url = urljoin(base_url, url)
        
        # Parse URL
        parsed = urlparse(url)
        
        # Handle fragment-only URLs after parsing (edge case)
        if not parsed.netloc and not parsed.path and parsed.fragment:
            return f"#{parsed.fragment}"
        
        # Rebuild URL with normalized components
        normalized = urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            parsed.params,
            parsed.query,
            ""  # Remove fragment for normalization
        ))
        
        return normalized.rstrip('/')
        
    except Exception:
        return url

def is_valid_image_url(url: str) -> bool:
    """Check if URL points to a supported image format"""
    try:
        from config import SUPPORTED_IMAGE_EXTENSIONS, EXCLUDED_EXTENSIONS
    except ImportError:
        return True  # Skip validation if config not available
    
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        
        # Get file extension
        ext = path.split('.')[-1].split('?')[0]  # Remove query params
        
        return ext in SUPPORTED_IMAGE_EXTENSIONS and ext not in EXCLUDED_EXTENSIONS
    except Exception:
        pass
    
    return False

def is_valid_image_mime(mime_type: str) -> bool:
    """Check if MIME type is a supported image format"""
    try:
        from config import SUPPORTED_IMAGE_MIMES
        return mime_type.lower() in SUPPORTED_IMAGE_MIMES
    except ImportError:
        return mime_type.startswith('image/')

def extract_links_from_text(text: str, base_url: str = None) -> Set[str]:
    """Extract links from text using regex patterns - improved version"""
    links = set()
    
    # Comprehensive URL patterns for different types of links
    url_patterns = [
        # HTTP/HTTPS URLs
        r'https?://[^\s<>"\'\(\)\[\]{}]+',
        # URLs without protocol (www.example.com)
        r'www\.[^\s<>"\'\(\)\[\]{}]+',
        # Domain-only URLs (example.com)
        r'\b[a-zA-Z0-9][a-zA-Z0-9-]{1,61}[a-zA-Z0-9]?\.[a-zA-Z]{2,}(?:/[^\s<>"\'\(\)\[\]{}]*)?',
        # Email addresses
        r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
        # Phone numbers (various formats)
        r'(?:\+?1[-.\s]?)?\(?[0-9]{3}\)?[-.\s]?[0-9]{3}[-.\s]?[0-9]{4}',
        # API endpoints
        r'https?://[^\s<>"\'\(\)\[\]{}]+/api/[^\s<>"\'\(\)\[\]{}]+',
        r'https?://[^\s<>"\'\(\)\[\]{}]+/wp-json/[^\s<>"\'\(\)\[\]{}]+',
        r'https?://[^\s<>"\'\(\)\[\]{}]+/wp/v2/[^\s<>"\'\(\)\[\]{}]+'
    ]
    
    # Try to get patterns from config, fallback to defaults
    try:
        from config import LINK_PATTERNS
        if isinstance(LINK_PATTERNS, dict):
            # If LINK_PATTERNS is a dict, extract the values
            url_patterns.extend(LINK_PATTERNS.values())
        elif isinstance(LINK_PATTERNS, list):
            url_patterns.extend(LINK_PATTERNS)
    except ImportError:
        pass  # Use our enhanced default patterns
    
    for pattern in url_patterns:
        try:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for match in matches:
                if isinstance(match, tuple):
                    match = match[0]  # Take first group if tuple
                
                # Clean up the URL
                cleaned_url = match.strip('.,;!?"\'()[]{}')
                
                # Skip if it's just a domain without protocol and base_url is provided
                if base_url and not cleaned_url.startswith(('http://', 'https://', '//', 'mailto:', 'tel:')):
                    # Check if it looks like a domain
                    if '.' in cleaned_url and not '/' in cleaned_url:
                        cleaned_url = f"https://{cleaned_url}"
                
                try:
                    normalized = normalize_url(cleaned_url, base_url)
                    if normalized and len(normalized) > 4:  # Minimum valid URL length
                        links.add(normalized)
                except Exception:
                    continue
        except Exception:
            continue
    
    return links

def create_session_with_retries(retries: int = 3, backoff_factor: float = 0.3) -> requests.Session:
    """Create requests session with retry logic"""
    session = requests.Session()
    
    retry_strategy = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"]
    )
    
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    return session

def load_json_file(file_path: str, default=None):
    """Load JSON file with error handling"""
    if not os.path.exists(file_path):
        return default if default is not None else {}
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception) as e:
        print(f"WARNING: Error loading {file_path}: {e}")
        return default if default is not None else {}

def save_json_file(file_path: str, data: dict) -> bool:
    """Save data to JSON file"""
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"ERROR: Error saving {file_path}: {e}")
        return False

def append_to_file(file_path: str, content: str) -> bool:
    """Append content to file"""
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'a', encoding='utf-8') as f:
            f.write(content)
        return True
    except Exception as e:
        print(f"ERROR: Error appending to {file_path}: {e}")
        return False

def format_file_size(size_bytes: int) -> str:
    """Format file size in human readable format"""
    if size_bytes == 0:
        return "0 B"
    
    size_names = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while size_bytes >= 1024 and i < len(size_names) - 1:
        size_bytes /= 1024.0
        i += 1
    
    return f"{size_bytes:.1f} {size_names[i]}"

def format_duration(seconds: float) -> str:
    """Format duration in human readable format"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        remaining_seconds = int(seconds % 60)
        return f"{minutes}m {remaining_seconds}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes}m"

def is_same_domain(url1: str, url2: str) -> bool:
    """Check if two URLs belong to the same domain"""
    try:
        domain1 = get_domain_name(url1)
        domain2 = get_domain_name(url2)
        return domain1 == domain2
    except Exception:
        return False

def should_skip_url(url: str, visited_urls: Set[str]) -> bool:
    """Check if URL should be skipped during crawling - improved version"""
    try:
        # Skip if already visited
        if url in visited_urls:
            return True
        
        # Skip special URL schemes that shouldn't be crawled
        if url.startswith(('mailto:', 'tel:', 'javascript:', 'data:', 'file:')):
            return True
        
        # Skip fragment-only URLs (just anchors on the same page)
        if url.startswith('#'):
            return True
        
        # Parse URL for further checks
        parsed = urlparse(url)
        
        # Skip empty or invalid URLs
        if not parsed.netloc and not parsed.path:
            return True
        
        # Use the new URL filter system
        try:
            from url_filter import is_url_blocked
            result = is_url_blocked(url)
            if isinstance(result, tuple):
                blocked, reason = result
                if blocked:
                    return True
            elif result:  # If it's just a boolean
                return True
        except ImportError:
            # Fallback to original logic if url_filter not available
            
            # Skip certain file types
            path = parsed.path.lower()
            if path.endswith(('.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx')):
                return True
            
            # Skip certain URL patterns (admin areas, etc.)
            skip_patterns = [
                r'/wp-admin/',
                r'/admin/',
                r'/login',
                r'/logout',
                r'/register',
                r'\.zip$',
                r'\.exe$',
                r'\.dmg$',
            ]
            
            url_lower = url.lower()
            for pattern in skip_patterns:
                if re.search(pattern, url_lower):
                    return True
        
        return False
        
    except Exception:
        return True

def validate_website_url(url: str) -> Tuple[bool, str]:
    """Validate URL format. No live HTTP request — avoids Cloudflare blocks and offline failures.
    Connectivity is checked at crawl time by the spider."""
    if not url:
        return False, "Empty URL"
    if not url.startswith(('http://', 'https://')):
        url = f"https://{url}"
    parsed = urlparse(url)
    if not parsed.netloc or '.' not in parsed.netloc:
        return False, "Invalid URL format"
    return True, "URL format valid"

class ProgressTracker:
    """Track progress of operations"""
    
    def __init__(self, operation_name: str):
        self.operation_name = operation_name
        self.start_time = time.time()
        self.processed_items = 0
        self.total_items = None
        self.errors = 0

    def update(self, processed: int, success: bool = True):
        """Update progress counters"""
        self.processed_items += processed
        if not success:
            self.errors += 1

    def add_error(self):
        """Add error to counter"""
        self.errors += 1

    def set_total(self, total: int):
        """Set total number of items to process"""
        self.total_items = total

    def get_progress_percentage(self) -> float:
        """Get progress percentage"""
        if self.total_items and self.total_items > 0:
            return min(100.0, (self.processed_items / self.total_items) * 100)
        return 0.0

    def get_rate(self) -> float:
        """Get processing rate (items per second)"""
        elapsed = time.time() - self.start_time
        if elapsed > 0:
            return self.processed_items / elapsed
        return 0.0

    def get_eta(self) -> Optional[float]:
        """Get estimated time to completion (seconds)"""
        if not self.total_items or self.total_items <= 0:
            return None
        
        rate = self.get_rate()
        if rate <= 0:
            return None
        
        remaining = self.total_items - self.processed_items
        return remaining / rate

    def get_summary(self) -> str:
        """Get progress summary string"""
        elapsed = time.time() - self.start_time
        rate = self.get_rate()
        
        if self.total_items:
            progress = self.get_progress_percentage()
            eta = self.get_eta()
            eta_str = f", ETA: {format_duration(eta)}" if eta else ""
            return f"{self.operation_name}: {self.processed_items}/{self.total_items} ({progress:.1f}%) - {rate:.1f}/s{eta_str}"
        else:
            return f"{self.operation_name}: {self.processed_items} items - {rate:.1f}/s - {format_duration(elapsed)}"

async def async_sleep_with_jitter(base_delay: float, jitter_factor: float = 0.1):
    """Sleep with random jitter to avoid thundering herd"""
    import random
    jitter = random.uniform(-jitter_factor, jitter_factor) * base_delay
    delay = max(0.1, base_delay + jitter)
    await asyncio.sleep(delay)
