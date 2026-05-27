#!/usr/bin/env python3
"""
Unified Search Toolkit v2
Consolidated multi-engine search, image downloading, and file extraction tool.

Modes:
  1) Search & Extract  — Multi-engine search → download images/PDFs → convert to JPG
  2) Bing Image Downloader — Search Bing Images with format/quality filters
  3) Dork Runner — Run Google dorks across multiple engines, save URL lists

Enhanced Features:
  - Tor proxy support for avoiding rate limits
  - SQLite state persistence with JSON backup
  - Smart rate limiting with domain throttling
  - Search result caching to reduce API costs
  - Resume from checkpoint functionality
"""

import os
import sys
import time
import random
import json
import io
import re
import hashlib
import traceback
import argparse
from pathlib import Path
from urllib.parse import urlparse, urljoin, quote_plus
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Set

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from PIL import Image
import fitz  # PyMuPDF
from colorama import init, Fore, Style

# Import enhanced modules
from .tor_manager import TorManager
from .state_manager import StateManager
from .rate_limiter import AdaptiveRateLimiter
from .search_cache import SearchCache

# Initialize colorama for colored terminal output
init(autoreset=True)

# ============================================
# Enhanced Components (Global State)
# ============================================
_tor_manager: Optional[TorManager] = None
_state_manager: Optional[StateManager] = None
_rate_limiter: Optional[AdaptiveRateLimiter] = None
_search_cache: Optional[SearchCache] = None
_cli_args = None

def init_enhanced_components(args):
    """Initialize enhanced components based on CLI args."""
    global _tor_manager, _state_manager, _rate_limiter, _search_cache, _cli_args
    _cli_args = args
    
    # State directory
    project_root = Path(__file__).resolve().parent.parent
    state_dir = Path(args.state_dir) if args.state_dir else project_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    
    # Initialize state manager
    _state_manager = StateManager(state_dir)
    
    # Initialize Tor manager if requested
    if args.use_tor:
        print(f"{Fore.CYAN}🔒 Initializing Tor proxy...{Style.RESET_ALL}")
        _tor_manager = TorManager()
        if _tor_manager.start(wait_for_bootstrap=True):
            print(f"{Fore.GREEN}✅ Tor proxy ready.{Style.RESET_ALL}")
        else:
            print(f"{Fore.RED}⚠️  Failed to start Tor. Continuing without proxy.{Style.RESET_ALL}")
            _tor_manager = None
    
    # Initialize rate limiter
    _rate_limiter = AdaptiveRateLimiter(
        base_delay=args.rate_limit_delay,
        tor_manager=_tor_manager
    )
    
    # Initialize search cache
    if not args.no_cache:
        cache_dir = state_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        _search_cache = SearchCache(cache_dir, ttl_hours=args.cache_ttl)
        expired_entries = _search_cache.cleanup_expired()
        print(f"{Fore.GREEN}✅ Search cache enabled (TTL: {args.cache_ttl}h){Style.RESET_ALL}")
        if expired_entries:
            print(f"{Fore.CYAN}🧹 Removed {expired_entries} expired cache entries{Style.RESET_ALL}")
    else:
        _search_cache = None
        print(f"{Fore.YELLOW}⚠️  Search cache disabled{Style.RESET_ALL}")
    
    return _state_manager, _rate_limiter, _search_cache, _tor_manager

def cleanup_enhanced_components():
    """Clean up enhanced components on exit."""
    global _tor_manager, _state_manager
    
    if _tor_manager and _tor_manager.is_running:
        print(f"{Fore.CYAN}🛑 Stopping Tor daemon...{Style.RESET_ALL}")
        _tor_manager.stop()
    
    if _state_manager:
        print(f"{Fore.CYAN}💾 Backing up state...{Style.RESET_ALL}")
        _state_manager.backup_to_json()

# ============================================
# Unified Path Manager (optional dependency)
# ============================================
try:
    from .download_path_manager import prompt_for_download_path
except ImportError:
    def prompt_for_download_path(context=None, out_path=None):
        return str(Path(__file__).resolve().parent.parent / "downloads")

# Increase the size limit for large images
Image.MAX_IMAGE_PIXELS = None

# ============================================
# Global Configuration
# ============================================
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
MIN_ACCEPTABLE_RESULTS = 20
MAX_DOWNLOAD_THREADS = 5
MAX_RETRIES = 3

# Spider / quality gate settings
CONTENT_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif', '.jfif', '.pdf'}
SKIP_EXTENSIONS = {'.svg', '.webp', '.ico', '.cur', '.gif'}
ICON_KEYWORDS = {'icon', 'logo', 'favicon', 'sprite', 'thumb', 'avatar', 'badge',
                 'button', 'arrow', 'spacer', 'pixel', 'tracking', 'analytics'}
MIN_IMAGE_DIMENSION = 200
MIN_FILE_SIZE_BYTES = 10240  # 10 KB

# User-Agent pool for rotation
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) Gecko/20100101 Firefox/123.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
    'Mozilla/5.0 (X11; Linux x86_64; rv:123.0) Gecko/20100101 Firefox/123.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 OPR/105.0.0.0',
]

# ============================================
# Robust Session with Retry & UA Rotation
# ============================================
def _build_session():
    """Build a requests session with connection pooling, retries, and UA rotation."""
    session = requests.Session()
    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=2,  # 2s, 4s, 8s
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST"],
    )
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=20,
        pool_maxsize=20,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.max_redirects = 5
    session.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    })
    return session


SESSION = _build_session()


def rotate_ua():
    """Rotate the session user-agent to a random one."""
    SESSION.headers["User-Agent"] = random.choice(USER_AGENTS)


def robust_get(url, timeout=15, stream=False):
    """GET with UA rotation and graceful error handling. Returns response or None."""
    rotate_ua()
    try:
        response = SESSION.get(url, timeout=timeout, stream=stream)
        if response.status_code == 200:
            return response
        return None
    except requests.exceptions.Timeout:
        print(f"{Fore.RED}  ⏱ Timeout: {url[:80]}...")
        return None
    except requests.exceptions.TooManyRedirects:
        print(f"{Fore.RED}  🔄 Too many redirects: {url[:80]}...")
        return None
    except requests.exceptions.ConnectionError:
        print(f"{Fore.RED}  🔌 Connection error: {url[:80]}...")
        return None
    except Exception:
        return None


# ============================================
# Hash-based Deduplication
# ============================================
class DeduplicationTracker:
    """Track content hashes to avoid saving duplicate files."""

    def __init__(self):
        self._seen_hashes = set()
        self.duplicates_skipped = 0

    def is_duplicate(self, data: bytes) -> bool:
        """Return True if we've already seen this exact content."""
        content_hash = hashlib.sha256(data).hexdigest()
        if content_hash in self._seen_hashes:
            self.duplicates_skipped += 1
            return True
        self._seen_hashes.add(content_hash)
        return False


# Global dedup tracker — reset per pipeline run
_dedup = DeduplicationTracker()


# ============================================
# Progress Tracker
# ============================================
class ProgressTracker:
    """Track download stats across a pipeline run."""

    def __init__(self):
        self.downloaded = 0
        self.failed = 0
        self.skipped = 0
        self.start_time = time.time()

    def success(self):
        self.downloaded += 1

    def fail(self):
        self.failed += 1

    def skip(self):
        self.skipped += 1

    @property
    def elapsed(self):
        return time.time() - self.start_time

    def status_line(self, current, total):
        elapsed = self.elapsed
        rate = self.downloaded / elapsed if elapsed > 0 else 0
        eta = (total - current) / rate if rate > 0 else 0
        return (
            f"[{Fore.GREEN}{self.downloaded} saved{Style.RESET_ALL} | "
            f"{Fore.YELLOW}{self.skipped} skipped{Style.RESET_ALL} | "
            f"{Fore.RED}{self.failed} failed{Style.RESET_ALL}] "
            f"{Fore.CYAN}{elapsed:.0f}s elapsed"
            f"{f' ~{eta:.0f}s remaining' if rate > 0 else ''}{Style.RESET_ALL}"
        )

    def final_report(self):
        print(f"\n{Fore.CYAN}{'─' * 50}")
        print(f"{Fore.CYAN}📊 Pipeline Stats")
        print(f"{Fore.CYAN}{'─' * 50}")
        print(f"  {Fore.GREEN}✅ Downloaded:  {self.downloaded}")
        print(f"  {Fore.YELLOW}⏭️  Skipped:     {self.skipped}")
        print(f"  {Fore.RED}❌ Failed:      {self.failed}")
        print(f"  {Fore.MAGENTA}🔄 Dedup saved: {_dedup.duplicates_skipped}")
        print(f"  {Fore.CYAN}⏱  Time:        {self.elapsed:.1f}s")
        print(f"{Fore.CYAN}{'─' * 50}")


# ============================================
# Default Dorks for Dork Runner (Mode 3)
# ============================================
DEFAULT_DORKS = [
    'site:credsverse.com "SMU .Hack"',
    'site:sites.google.com "moe.edu.sg"',
    'site:online.fliphtml5.com "secondary school"',
    'secondary school yearbook',
    'jc yearbook',
    'site:.edu.sg inurl:wp-content/uploads',
    'site:.moe.edu.sg filetype:jpg',
    'site:.edu.sg filetype:png "graduation"',
    'intitle:"Yearbook" filetype:pdf',
    'site:online.fliphtml5.com "Secondary School"',
    'site:fliphtml5.com "Yearbook"',
    'site:issuu.com "Secondary School" "Yearbook"',
    'site:sharepoint.com "Yearbook"',
    'site:drive.google.com "Secondary School" "Yearbook"',
    'site:drive.google.com filetype:pdf "Class of"',
    'site:docs.google.com "Yearbook"',
    'site:*.moe.edu.sg filetype:pdf',
    'site:.moe.edu.sg filetype:pdf "yearbook"',
    'site:.edu.sg filetype:pdf "yearbook"',
    'site:.edu.sg "Secondary School" filetype:pdf',
    'filetype:pdf "Graduation Ceremony"',
    'filetype:pdf "Speech Day" "Secondary School"',
    'filetype:pdf "Secondary School" "Yearbook"',
    'filetype:pdf "Junior College" "Yearbook"',
    'filetype:pdf "Class of 2024" "Secondary"',
    'filetype:pdf site:moe.edu.sg',
    'filetype:pdf "SMU .Hack"',
]


# ============================================
# Utility Helpers
# ============================================
def clean_filename(name):
    """Remove characters that are invalid in file/folder names."""
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def ensure_dir(path):
    """Create directory if it doesn't exist, return the path."""
    os.makedirs(path, exist_ok=True)
    return path


# ============================================
# Search Engines (shared across all modes)
# ============================================
def search_duckduckgo(query, max_results=50):
    """Search using DuckDuckGo — no API key needed."""
    # Check cache first
    if _search_cache:
        cached = _search_cache.get(query, "duckduckgo")
        if cached:
            print(f"{Fore.GREEN}[DDG] Cache hit! Returning {len(cached)} cached results{Style.RESET_ALL}")
            return cached
    
    try:
        import warnings
        warnings.filterwarnings("ignore", message=".*Impersonate.*")
        warnings.filterwarnings("ignore", message=".*renamed.*")

        from ddgs import DDGS

        print(f"{Fore.CYAN}[DDG] Searching: {query}")
        results = set()
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                # Handle both old ('link') and new ('href') API formats
                url = r.get('href') or r.get('link') or r.get('url', '')
                if url:
                    results.add(url)
                # Rate limiting
                if _rate_limiter:
                    _rate_limiter.wait("https://duckduckgo.com")
                time.sleep(0.1)
        print(f"{Fore.GREEN}[DDG] Found {len(results)} results{Style.RESET_ALL}")
        
        # Cache results
        if _search_cache and results:
            _search_cache.set(query, "duckduckgo", results)
        
        return results
    except Exception as e:
        print(f"{Fore.RED}[DDG] Error: {e}{Style.RESET_ALL}")
        if _rate_limiter:
            _rate_limiter.record_failure("https://duckduckgo.com", 500)
        return set()


def search_bing(query, num_pages=5):
    """Search using Bing web search with proper URL encoding."""
    # Check cache first
    if _search_cache:
        cached = _search_cache.get(query, "bing")
        if cached:
            print(f"{Fore.GREEN}[Bing] Cache hit! Returning {len(cached)} cached results{Style.RESET_ALL}")
            return cached
    
    try:
        print(f"{Fore.CYAN}[Bing] Searching: {query}")
        results = set()
        encoded_query = quote_plus(query)
        for page in range(0, num_pages * 10, 10):
            url = f'https://www.bing.com/search?q={encoded_query}&first={page}'
            
            # Rate limiting
            if _rate_limiter:
                _rate_limiter.wait("https://www.bing.com")
            
            response = robust_get(url, timeout=10)
            if response:
                if _rate_limiter:
                    _rate_limiter.record_success("https://www.bing.com")
                soup = BeautifulSoup(response.text, 'html.parser')
                for result in soup.select('li.b_algo h2 a'):
                    link = result.get('href')
                    if link and link.startswith('http'):
                        results.add(link)
            else:
                if _rate_limiter:
                    _rate_limiter.record_failure("https://www.bing.com", response.status_code if response else 0)
            time.sleep(random.uniform(1, 2.5))
        print(f"{Fore.GREEN}[Bing] Found {len(results)} results{Style.RESET_ALL}")
        
        # Cache results
        if _search_cache and results:
            _search_cache.set(query, "bing", results)
        
        return results
    except Exception as e:
        print(f"{Fore.RED}[Bing] Error: {e}{Style.RESET_ALL}")
        if _rate_limiter:
            _rate_limiter.record_failure("https://www.bing.com", 500)
        return set()


def search_serper(query, api_key):
    """Search using Serper.dev API (Google results) with regional params."""
    # Check cache first
    if _search_cache:
        cached = _search_cache.get(query, "serper")
        if cached:
            print(f"{Fore.GREEN}[Serper] Cache hit! Returning {len(cached)} cached results{Style.RESET_ALL}")
            return cached
    
    if not api_key:
        return set()
    try:
        print(f"{Fore.CYAN}[Serper API] Searching: {query}")
        results = set()
        url = "https://google.serper.dev/search"
        headers = {'X-API-KEY': api_key, 'Content-Type': 'application/json'}
        
        # Rate limiting
        if _rate_limiter:
            _rate_limiter.wait("https://google.serper.dev")
        
        for page in range(3):
            payload = json.dumps({
                "q": query,
                "page": page + 1,
                "num": 100,
                "gl": "sg",
                "hl": "en",
            })
            rotate_ua()
            response = requests.post(url, headers=headers, data=payload, timeout=15)
            if response.status_code == 401:
                print(f"{Fore.RED}[Serper] API key invalid or unauthorized.{Style.RESET_ALL}")
                return set()
            if response.status_code == 429:
                print(f"{Fore.YELLOW}[Serper] API quota exceeded.{Style.RESET_ALL}")
                return set()
            if response.status_code == 200:
                if _rate_limiter:
                    _rate_limiter.record_success("https://google.serper.dev")
                data = response.json()
                if 'organic' in data:
                    for item in data['organic']:
                        if 'link' in item:
                            results.add(item['link'])
                else:
                    break
            else:
                if _rate_limiter:
                    _rate_limiter.record_failure("https://google.serper.dev", response.status_code)
                break
        print(f"{Fore.GREEN}[Serper API] Found {len(results)} results{Style.RESET_ALL}")
        
        # Cache results
        if _search_cache and results:
            _search_cache.set(query, "serper", results)
        
        return results
    except Exception as e:
        print(f"{Fore.RED}[Serper API] Error: {e}{Style.RESET_ALL}")
        if _rate_limiter:
            _rate_limiter.record_failure("https://google.serper.dev", 500)
        if hasattr(e, 'response') and e.response is not None:
            print(f"{Fore.RED}[Serper API] Details: {e.response.text}")
        return set()


def search_chrome(query, num_pages=5):
    """Search using undetected ChromeDriver — most reliable but slowest (final fallback)."""
    try:
        import undetected_chromedriver as uc
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        print(f"{Fore.CYAN}[Chrome] Searching: {query} (Final Fallback)")
        results = set()

        options = uc.ChromeOptions()
        options.add_argument('--headless=new')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--no-sandbox')
        options.add_argument(f'--user-agent={random.choice(USER_AGENTS)}')

        driver = uc.Chrome(options=options)

        try:
            driver.get('https://www.google.com')
            time.sleep(random.uniform(2, 4))

            try:
                search_box = driver.find_element(By.NAME, 'q')
            except Exception:
                print(f"{Fore.RED}[Chrome] Could not find search box. You might be blocked.")
                return results

            for char in query:
                search_box.send_keys(char)
                time.sleep(random.uniform(0.05, 0.2))

            time.sleep(random.uniform(0.5, 1.5))
            search_box.send_keys(Keys.RETURN)
            time.sleep(random.uniform(4, 7))

            for page in range(num_pages):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
                time.sleep(random.uniform(1, 2))

                links = driver.find_elements(By.CSS_SELECTOR, 'a[jsname="UWckNb"]')
                for link in links:
                    try:
                        href = link.get_attribute('href')
                        if href and href.startswith('http'):
                            results.add(href)
                    except Exception:
                        continue

                try:
                    next_button = driver.find_element(By.ID, 'pnnext')
                    next_button.click()
                    time.sleep(random.uniform(5, 8))
                except Exception:
                    print(f"{Fore.YELLOW}[Chrome] No more pages at page {page + 1}")
                    break
        finally:
            driver.quit()

        print(f"{Fore.GREEN}[Chrome] Found {len(results)} results")
        return results

    except Exception as e:
        print(f"{Fore.RED}[Chrome] Error: {e}")
        return set()


def get_search_results(query, max_total=50, use_chrome_fallback=False):
    """Waterfall search: DDG → Bing → Serper (conserved) → (optionally) Chrome."""
    all_results = set()

    # 1. DuckDuckGo (free, unlimited)
    ddg_res = search_duckduckgo(query, max_results=30)
    all_results.update(ddg_res)
    if len(all_results) >= max_total:
        return list(all_results)[:max_total]

    time.sleep(2)

    # 2. Bing (free, scraping)
    bing_res = search_bing(query, num_pages=3)
    all_results.update(bing_res)
    if len(all_results) >= max_total:
        return list(all_results)[:max_total]

    # 3. Serper — ONLY if DDG + Bing found almost nothing (< 5 results)
    #    Conserves limited API credits
    if len(all_results) < 5 and SERPER_API_KEY:
        time.sleep(2)
        print(f"{Fore.YELLOW}[Info] DDG + Bing found < 5 results, using Serper API credits...")
        serper_res = search_serper(query, SERPER_API_KEY)
        all_results.update(serper_res)
        if len(all_results) >= max_total:
            return list(all_results)[:max_total]

    # 4. Chrome fallback (optional — slow)
    if use_chrome_fallback and len(all_results) < MIN_ACCEPTABLE_RESULTS:
        time.sleep(2)
        print(f"\n{Fore.YELLOW}[Info] APIs didn't yield enough, falling back to ChromeDriver...")
        chrome_res = search_chrome(query, num_pages=5)
        all_results.update(chrome_res)

    return list(all_results)[:max_total]


# ============================================
# File Processing & Downloading
# ============================================
def process_image(img_data, out_path):
    """Save image data as high-quality JPG."""
    try:
        img = Image.open(io.BytesIO(img_data))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.save(out_path, format="JPEG", quality=95)
        return True
    except Exception as e:
        print(f"{Fore.RED}Failed to process image {out_path}: {e}")
        return False


def extract_pdf_pages_as_jpg(pdf_data, out_folder, base_filename):
    """Extract all pages of a PDF as high-quality JPGs."""
    try:
        MAX_PDF_PAGES = 50
        pdf_document = fitz.open(stream=pdf_data, filetype="pdf")
        page_count = len(pdf_document)
        
        if page_count == 0:
            print(f"{Fore.YELLOW}PDF has no pages: {base_filename}")
            return False

        zoom = 4 if page_count <= 10 else (2 if page_count <= 30 else 1)
        mat = fitz.Matrix(zoom, zoom)

        for page_num in range(min(page_count, MAX_PDF_PAGES)):
            page = pdf_document.load_page(page_num)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            out_name = f"{base_filename}_page_{page_num + 1}.jpg"
            out_path = os.path.join(out_folder, out_name)
            img.save(out_path, format="JPEG", quality=95)

        pdf_document.close()
        return True
    except Exception as e:
        print(f"{Fore.RED}Failed to extract PDF {base_filename}: {e}")
        return False


def download_file(url, target_folder, base_filename, expected_type, query=""):
    """Download a file. Images→JPG, PDFs→extract pages to JPG. With dedup and state tracking."""
    # Atomically claim this URL — returns False if another thread/process already has it
    # or it's already completed/skipped. Retries 'failed' URLs.
    if _state_manager:
        if not _state_manager.mark_download_pending(url, query):
            print(f"{Fore.YELLOW}  ⏭️  Already handled: {url[:60]}...{Style.RESET_ALL}")
            return True
    
    response = robust_get(url)
    if not response:
        if _state_manager:
            _state_manager.mark_download_failed(url)
        return False

    data = response.content

    # Dedup check
    if _dedup.is_duplicate(data):
        if _state_manager:
            _state_manager.mark_download_skipped(url)
        return False  # Already have this exact content

    content_type = response.headers.get('Content-Type', '').lower()

    is_pdf = 'pdf' in content_type or url.lower().endswith('.pdf') or expected_type == 'pdf'
    is_image = ('image' in content_type
                or url.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif', '.jfif'))
                or expected_type == 'image')

    if is_pdf:
        pdf_path = os.path.join(target_folder, f"{base_filename}.pdf")
        with open(pdf_path, 'wb') as f:
            f.write(data)
        pdf_out_folder = ensure_dir(os.path.join(target_folder, f"{base_filename}_pages"))
        print(f"{Fore.YELLOW}  -> Extracting PDF pages for {base_filename}...{Style.RESET_ALL}")
        success = extract_pdf_pages_as_jpg(data, pdf_out_folder, base_filename)
        if success and _state_manager:
            content_hash = hashlib.sha256(data).hexdigest()
            _state_manager.mark_download_complete(url, pdf_path, content_hash)
        elif _state_manager:
            _state_manager.mark_download_failed(url)
        return success

    elif is_image:
        out_path = os.path.join(target_folder, f"{base_filename}.jpg")
        success = process_image(data, out_path)
        if success and _state_manager:
            content_hash = hashlib.sha256(data).hexdigest()
            _state_manager.mark_download_complete(url, out_path, content_hash)
        elif _state_manager:
            _state_manager.mark_download_failed(url)
        return success

    else:
        out_path = os.path.join(target_folder, f"{base_filename}.jpg")
        success = process_image(data, out_path)
        if success and _state_manager:
            content_hash = hashlib.sha256(data).hexdigest()
            _state_manager.mark_download_complete(url, out_path, content_hash)
        elif _state_manager:
            _state_manager.mark_download_failed(url)
        return success


# ============================================
# Page Spidering & Quality-Gated Download
# ============================================
def is_content_url(url):
    """Return True if the URL looks like actual pictorial/PDF content (not an icon/sprite)."""
    url_lower = url.lower()
    parsed = urlparse(url_lower)
    path = parsed.path

    ext = os.path.splitext(path)[1]
    if ext not in CONTENT_EXTENSIONS:
        return False

    for kw in ICON_KEYWORDS:
        if kw in path:
            return False

    return True


def spider_page(page_url, target_format='all'):
    """Fetch a webpage and extract all linked image/PDF URLs."""
    discovered = set()
    response = robust_get(page_url)
    if not response:
        return discovered

    try:
        content_type = response.headers.get('Content-Type', '').lower()
        if 'html' not in content_type:
            if is_content_url(page_url):
                discovered.add(page_url)
            return discovered

        soup = BeautifulSoup(response.text, 'html.parser')
        candidate_urls = set()

        for tag in soup.find_all('img'):
            src = tag.get('src')
            if src:
                candidate_urls.add(urljoin(page_url, src))
            srcset = tag.get('srcset')
            if srcset:
                for part in srcset.split(','):
                    part = part.strip().split()[0] if part.strip() else ''
                    if part:
                        candidate_urls.add(urljoin(page_url, part))

        for tag in soup.find_all('a', href=True):
            candidate_urls.add(urljoin(page_url, tag['href']))

        for tag in soup.find_all('source'):
            srcset = tag.get('srcset')
            if srcset:
                for part in srcset.split(','):
                    part = part.strip().split()[0] if part.strip() else ''
                    if part:
                        candidate_urls.add(urljoin(page_url, part))

        for tag in soup.find_all(['embed', 'object']):
            src = tag.get('src') or tag.get('data')
            if src:
                candidate_urls.add(urljoin(page_url, src))

        for url in candidate_urls:
            if not is_content_url(url):
                continue
            ext = os.path.splitext(urlparse(url.lower()).path)[1]
            if target_format == 'image' and ext == '.pdf':
                continue
            if target_format == 'pdf' and ext != '.pdf':
                continue
            discovered.add(url)

    except Exception as e:
        print(f"{Fore.RED}  -> Spider error on {page_url}: {e}")

    return discovered


def download_with_quality_gate(url, target_folder, base_filename, expected_type, query=""):
    """Download a file with quality checks: min dimensions, min size, dedup and state tracking."""
    # Atomically claim this URL — skip if completed/pending/skipped, retry if failed
    if _state_manager:
        if not _state_manager.mark_download_pending(url, query):
            print(f"{Fore.YELLOW}  ⏭️  Already handled: {url[:60]}...{Style.RESET_ALL}")
            return True
    
    response = robust_get(url)
    if not response:
        if _state_manager:
            _state_manager.mark_download_failed(url)
        return False

    data = response.content

    # Size gate
    if len(data) < MIN_FILE_SIZE_BYTES:
        if _state_manager:
            _state_manager.mark_download_skipped(url)
        return False

    # Dedup check
    if _dedup.is_duplicate(data):
        if _state_manager:
            _state_manager.mark_download_skipped(url)
        return False

    content_type = response.headers.get('Content-Type', '').lower()
    ext = os.path.splitext(urlparse(url.lower()).path)[1]

    is_pdf = 'pdf' in content_type or ext == '.pdf' or expected_type == 'pdf'
    is_image = ('image' in content_type
                or ext in {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif', '.jfif'}
                or expected_type == 'image')

    if is_pdf:
        pdf_path = os.path.join(target_folder, f"{base_filename}.pdf")
        with open(pdf_path, 'wb') as f:
            f.write(data)
        pdf_out_folder = ensure_dir(os.path.join(target_folder, f"{base_filename}_pages"))
        print(f"{Fore.YELLOW}  -> Extracting PDF pages for {base_filename}...{Style.RESET_ALL}")
        success = extract_pdf_pages_as_jpg(data, pdf_out_folder, base_filename)
        if success and _state_manager:
            content_hash = hashlib.sha256(data).hexdigest()
            _state_manager.mark_download_complete(url, pdf_path, content_hash)
        elif _state_manager:
            _state_manager.mark_download_failed(url)
        return success

    elif is_image:
        try:
            img = Image.open(io.BytesIO(data))
            w, h = img.size
            if w < MIN_IMAGE_DIMENSION or h < MIN_IMAGE_DIMENSION:
                if _state_manager:
                    _state_manager.mark_download_skipped(url)
                return False
        except Exception:
            if _state_manager:
                _state_manager.mark_download_failed(url)
            return False
        out_path = os.path.join(target_folder, f"{base_filename}.jpg")
        success = process_image(data, out_path)
        if success and _state_manager:
            content_hash = hashlib.sha256(data).hexdigest()
            _state_manager.mark_download_complete(url, out_path, content_hash)
        elif _state_manager:
            _state_manager.mark_download_failed(url)
        return success

    else:
        try:
            img = Image.open(io.BytesIO(data))
            w, h = img.size
            if w < MIN_IMAGE_DIMENSION or h < MIN_IMAGE_DIMENSION:
                if _state_manager:
                    _state_manager.mark_download_skipped(url)
                return False
        except Exception:
            if _state_manager:
                _state_manager.mark_download_failed(url)
            return False
        out_path = os.path.join(target_folder, f"{base_filename}.jpg")
        success = process_image(data, out_path)
        if success and _state_manager:
            content_hash = hashlib.sha256(data).hexdigest()
            _state_manager.mark_download_complete(url, out_path, content_hash)
        elif _state_manager:
            _state_manager.mark_download_failed(url)
        return success


# ============================================
# Parallel Download Engine
# ============================================
def _download_single(args):
    """Worker function for parallel downloads."""
    url, folder, base_name, expected_type, use_quality_gate, query = args
    try:
        if use_quality_gate:
            return download_with_quality_gate(url, folder, base_name, expected_type, query)
        else:
            return download_file(url, folder, base_name, expected_type, query)
    except Exception:
        return False


def parallel_download(urls, folder, expected_type, use_quality_gate=False, tracker=None, query=""):
    """Download URLs in parallel using a thread pool. Returns success count."""
    success_count = 0
    total = len(urls)

    tasks = []
    for i, url in enumerate(urls):
        base_name = f"{expected_type}_{i + 1}"
        tasks.append((url, folder, base_name, expected_type, use_quality_gate, query))

    try:
        with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_THREADS) as executor:
            future_to_idx = {executor.submit(_download_single, t): idx for idx, t in enumerate(tasks)}

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    result = future.result()
                    if result:
                        success_count += 1
                        if tracker:
                            tracker.success()
                        print(f"{Fore.GREEN}  ✓ {expected_type}_{idx + 1}")
                    else:
                        if tracker:
                            tracker.skip()
                except Exception:
                    if tracker:
                        tracker.fail()
    except KeyboardInterrupt:
        executor.shutdown(wait=False, cancel_futures=True)
        raise

    return success_count


# ============================================
# Bing Image Search & Download (Mode 2)
# ============================================
def has_transparency(img):
    """Check if an image has transparency (alpha channel)."""
    if img.mode == "RGBA":
        extrema = img.getextrema()
        if extrema[3][0] < 255:
            return True
    return False


def check_image_quality(img, min_quality):
    """Check if image meets minimum quality requirements."""
    width, height = img.size
    total_pixels = width * height
    if min_quality == 0:
        return True
    elif min_quality == 1:
        return total_pixels >= 480000
    elif min_quality == 2:
        return total_pixels >= 2073600
    return False


def get_bing_images(query, num_images=50):
    """Search Bing for images and return direct image URLs."""
    print(f"{Fore.CYAN}🔍 Searching Bing Images for: {query}")
    encoded_query = quote_plus(query)
    url = f"https://www.bing.com/images/search?q={encoded_query}&form=HDRSC2&first=1&count={num_images}"
    response = robust_get(url)
    if not response:
        return []

    try:
        soup = BeautifulSoup(response.text, "html.parser")
        image_urls = []
        for a in soup.find_all("a", class_="iusc"):
            m = a.get("m")
            if m:
                try:
                    m_json = json.loads(m)
                    if "murl" in m_json:
                        image_urls.append(m_json["murl"])
                        if len(image_urls) >= num_images:
                            break
                except json.JSONDecodeError:
                    continue
        print(f"{Fore.GREEN}📊 Found {len(image_urls)} image URLs")
        return image_urls
    except Exception as e:
        print(f"{Fore.RED}Error searching Bing Images: {str(e)}")
        return []


def download_image(url, folder_path, filename, desired_format, min_quality):
    """Download and save an image with format and quality filters."""
    response = robust_get(url, timeout=10)
    if not response:
        print(f"{Fore.RED}Failed to fetch: {filename}")
        return False

    try:
        data = response.content

        # Dedup
        if _dedup.is_duplicate(data):
            print(f"{Fore.YELLOW}Duplicate skipped: {filename}")
            return False

        img = Image.open(io.BytesIO(data))

        if not check_image_quality(img, min_quality):
            print(f"{Fore.YELLOW}Image quality too low: {filename}")
            return False

        file_path = None
        if desired_format == 0:
            file_extension = img.format.lower() if img.format else 'jpg'
            file_path = os.path.join(folder_path, f"{filename}.{file_extension}")
            img.save(file_path)
        elif desired_format == 1:
            if img.format and img.format.lower() not in ['jpeg', 'jpg']:
                print(f"{Fore.YELLOW}Not a JPG image: {filename}")
                return False
            file_path = os.path.join(folder_path, f"{filename}.jpg")
            img.save(file_path, format='JPEG')
        elif desired_format == 2:
            if img.format != "PNG" or not has_transparency(img):
                print(f"{Fore.YELLOW}Not a PNG with transparent background: {filename}")
                return False
            file_path = os.path.join(folder_path, f"{filename}.png")
            img.save(file_path, format='PNG')
        elif desired_format == 3:
            if img.format and img.format.lower() == "png":
                print(f"{Fore.YELLOW}PNG image not allowed: {filename}")
                return False
            file_extension = img.format.lower() if img.format else 'jpg'
            file_path = os.path.join(folder_path, f"{filename}.{file_extension}")
            img.save(file_path)

        if file_path:
            try:
                with Image.open(file_path) as check_img:
                    check_img.verify()
                print(f"{Fore.GREEN}✓ Downloaded: {os.path.basename(file_path)}")
                return True
            except Exception:
                print(f"{Fore.RED}Image validation failed: {os.path.basename(file_path)}")
                if os.path.exists(file_path):
                    os.remove(file_path)
                return False

        return False

    except Exception as e:
        print(f"{Fore.RED}Download error for {filename}: {str(e)}")
        return False


# ============================================
# Dork Runner Helpers (Mode 3)
# ============================================
def save_results(dork, all_results, output_dir):
    """Save search results to a text file."""
    filename = output_dir / f"{dork.replace(':', '_').replace(' ', '_').replace('\"', '')}.txt"
    with open(filename, 'w', encoding='utf-8') as f:
        for url in sorted(all_results):
            f.write(url + '\n')

    print(f"\n{'=' * 60}")
    print(f"Total unique results: {len(all_results)}")
    print(f"Saved to: {filename}")
    print(f"{'=' * 60}\n")


# ##############################################
# MODE 1: Search & Extract Pipeline
# ##############################################
def mode_search_extract():
    """Interactive search → download images/PDFs → convert to JPG."""
    global _dedup
    _dedup = DeduplicationTracker()

    print(f"""
{Fore.CYAN}{Style.BRIGHT}╔══════════════════════════════════════════════════════════════╗
║                    🔍  SEARCH & EXTRACT                        ║
║                                                              ║
║       Multi-engine searching with precise file extraction    ║
║     Convert everything to high-quality JPGs (with spidering)  ║
╚══════════════════════════════════════════════════════════════╝{Style.RESET_ALL}
""")

    print(f"{Fore.CYAN}How would you like to provide search queries?")
    print("1) Paste a comma-separated list of keywords")
    print("2) Provide a path to a .txt file (one query per line)")

    choice = input(f"{Fore.YELLOW}Select an option (1/2): {Style.RESET_ALL}").strip()
    queries = []
    if choice == '2':
        filepath = input(f"{Fore.YELLOW}Enter the path to the .txt file: {Style.RESET_ALL}").strip()
        filepath = filepath.strip('"').strip("'")
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                queries = [line.strip() for line in f if line.strip()]
        except Exception as e:
            print(f"{Fore.RED}Error reading file: {e}")
    else:
        text = input(f"{Fore.YELLOW}Enter comma-separated keywords: {Style.RESET_ALL}").strip()
        queries = [q.strip() for q in text.split(',') if q.strip()]

    if not queries:
        print(f"{Fore.RED}No valid queries provided. Exiting.")
        return

    print(f"\n{Fore.CYAN}What file types do you want to target?")
    print("1) Images only (converts to JPG)")
    print("2) PDFs only (extracts pages to JPGs)")
    print("3) Everything (Images & PDFs)")

    fmt_choice = input(f"{Fore.YELLOW}Select an option (1-3): {Style.RESET_ALL}").strip()
    if fmt_choice == '1':
        target_format = 'image'
    elif fmt_choice == '2':
        target_format = 'pdf'
    else:
        target_format = 'all'

    downloads_root = ensure_dir(prompt_for_download_path("Search Toolkit Downloads"))
    max_results = 20

    enable_spider = input(
        f"\n{Fore.CYAN}Enable page spidering? "
        f"(crawls each result page for linked images/PDFs) (y/n): {Style.RESET_ALL}"
    ).strip().lower() == 'y'

    tracker = ProgressTracker()

    print(f"\n{Fore.GREEN}{Style.BRIGHT}🚀 Starting Search & Extract Pipeline...")
    print(f"{Fore.CYAN}Queries loaded: {len(queries)}")
    print(f"{Fore.CYAN}Spidering: {'Enabled' if enable_spider else 'Disabled'}")
    print(f"{Fore.CYAN}Parallel downloads: {MAX_DOWNLOAD_THREADS} threads")
    print(f"{Fore.CYAN}Output directory: {downloads_root}\n")

    for query in queries:
        print(f"{Fore.BLUE}{Style.BRIGHT}{'=' * 60}")
        print(f"{Fore.BLUE}{Style.BRIGHT}PROCESSING: {query}")
        print(f"{Fore.BLUE}{Style.BRIGHT}{'=' * 60}")

        dorks = []
        if target_format in ('image', 'all'):
            dorks.append(f"{query} filetype:jpg")
            dorks.append(f"{query} filetype:png")
        if target_format in ('pdf', 'all'):
            dorks.append(f"{query} filetype:pdf")

        keyword_folder = ensure_dir(os.path.join(downloads_root, clean_filename(query)))

        for dork in dorks:
            expected_type = 'pdf' if 'filetype:pdf' in dork else 'image'
            print(f"\n{Fore.MAGENTA}>> Engine Search -> {dork}")

            search_urls = get_search_results(dork, max_total=max_results)
            print(f"{Fore.GREEN}Found {len(search_urls)} search result URLs.")

            if enable_spider:
                spidered_urls = set()
                already_seen = set(search_urls)
                print(f"{Fore.CYAN}🕷️  Spidering {len(search_urls)} pages for linked files...")
                for page_url in search_urls:
                    found = spider_page(page_url, target_format=expected_type)
                    new_found = found - already_seen
                    if new_found:
                        spidered_urls.update(new_found)
                        already_seen.update(new_found)
                    time.sleep(random.uniform(0.3, 0.8))
                print(f"{Fore.GREEN}🕷️  Discovered {len(spidered_urls)} additional file URLs.")
                all_download_urls = list(search_urls) + list(spidered_urls)
            else:
                all_download_urls = list(search_urls)

            print(f"{Fore.GREEN}⬇️  Downloading {len(all_download_urls)} URLs ({MAX_DOWNLOAD_THREADS} threads)...")

            success_count = parallel_download(
                all_download_urls,
                keyword_folder,
                expected_type,
                use_quality_gate=enable_spider,
                tracker=tracker,
            )

            print(f"{Fore.GREEN}Completed dork. Saved {success_count} files.")
            print(f"  {tracker.status_line(0, 0)}")
            time.sleep(2)

    print(f"\n{Fore.GREEN}{Style.BRIGHT}{'=' * 60}")
    print(f"{Fore.GREEN}{Style.BRIGHT}🎉 PIPELINE COMPLETE!")
    print(f"{Fore.CYAN}Check your downloads folder: {downloads_root}")
    print(f"{Fore.GREEN}{Style.BRIGHT}{'=' * 60}")
    tracker.final_report()

    input("\nPress ENTER to exit...")


# ##############################################
# MODE 2: Bing Image Downloader
# ##############################################
def mode_bing_images():
    """Interactive Bing Image search with format/quality/naming filters."""
    global _dedup
    _dedup = DeduplicationTracker()

    print(f"""
{Fore.CYAN}{Style.BRIGHT}╔══════════════════════════════════════════════════════════════╗
║                    🖼️  BING IMAGE DOWNLOADER                   ║
║                                                              ║
║          Download images from Bing with custom filters      ║
╚══════════════════════════════════════════════════════════════╝{Style.RESET_ALL}
""")

    # Use CLI output-dir if provided, else prompt interactively
    if _cli_args and _cli_args.output_dir:
        folder_path = ensure_dir(_cli_args.output_dir)
        print(f"{Fore.CYAN}Using output directory from CLI: {folder_path}{Style.RESET_ALL}")
    else:
        folder_path = prompt_for_download_path(context="Bing image search downloads", out_path=None)

    print(f"\n{Fore.CYAN}🎨 Image Format Preferences{Style.RESET_ALL}")
    desired_format = int(input(
        f"{Fore.CYAN}Enter desired format:\n"
        f"  0 = Any format\n"
        f"  1 = JPG only\n"
        f"  2 = PNG with transparency only\n"
        f"  3 = Any except PNG\n"
        f"Choice: {Style.RESET_ALL}"
    ))
    while desired_format not in [0, 1, 2, 3]:
        desired_format = int(input(f"{Fore.RED}Invalid choice. Enter 0-3: {Style.RESET_ALL}"))

    min_quality = int(input(
        f"{Fore.CYAN}Enter minimum quality:\n"
        f"  0 = Any quality\n"
        f"  1 = Medium (800x600+)\n"
        f"  2 = High (1920x1080+)\n"
        f"Choice: {Style.RESET_ALL}"
    ))
    while min_quality not in [0, 1, 2]:
        min_quality = int(input(f"{Fore.RED}Invalid choice. Enter 0-2: {Style.RESET_ALL}"))

    naming_choice = int(input(
        f"{Fore.CYAN}Choose naming format:\n"
        f"  1 = keyword_number (e.g., cat_1.jpg)\n"
        f"  2 = sequential number (e.g., 1.jpg)\n"
        f"Choice: {Style.RESET_ALL}"
    ))
    while naming_choice not in [1, 2]:
        naming_choice = int(input(f"{Fore.RED}Invalid choice. Enter 1 or 2: {Style.RESET_ALL}"))

    images_per_keyword = int(input(
        f"{Fore.CYAN}Images to download per keyword (1-50): {Style.RESET_ALL}"
    ))
    while images_per_keyword < 1 or images_per_keyword > 50:
        images_per_keyword = int(input(f"{Fore.RED}Enter number between 1-50: {Style.RESET_ALL}"))

    # Use CLI query if provided, else prompt interactively
    if _cli_args and _cli_args.query:
        words = _cli_args.query.split(",")
        print(f"{Fore.CYAN}Using query from CLI: {', '.join(words)}{Style.RESET_ALL}")
    else:
        print(f"{Fore.CYAN}🔍 Search Keywords{Style.RESET_ALL}")
        words = input(f"{Fore.CYAN}Enter keywords separated by commas: {Style.RESET_ALL}").split(",")
    keywords = [re.sub(r"[^\w\s-]", "", w.strip()).strip() for w in words if w.strip()]

    if not keywords:
        print(f"{Fore.RED}No valid keywords entered!")
        return

    print(f"{Fore.GREEN}✓ Will search for: {', '.join(keywords)}")
    print(f"\n{Fore.GREEN}{Style.BRIGHT}🚀 Starting download process...")
    print(f"{Fore.CYAN}📊 Will process {len(keywords)} keywords")
    print(f"{Fore.CYAN}📷 Downloading up to {images_per_keyword} images per keyword")

    total_downloaded = 0
    failed_keywords = []
    tracker = ProgressTracker()

    for keyword in keywords:
        if not keyword:
            continue

        print(f"\n{Fore.BLUE}{'=' * 50}")
        print(f"{Fore.BLUE}🔍 Processing keyword: {Fore.CYAN}{keyword}")
        print(f"{Fore.BLUE}{'=' * 50}")

        keyword_folder = os.path.join(folder_path, re.sub(r"[^\w\s-]", "", keyword).strip())
        os.makedirs(keyword_folder, exist_ok=True)

        image_urls = get_bing_images(keyword, num_images=50)

        if not image_urls:
            print(f"{Fore.RED}❌ No images found for: {keyword}")
            failed_keywords.append(keyword)
            continue

        downloaded_count = 0
        for i, image_url in enumerate(image_urls):
            if downloaded_count >= images_per_keyword:
                break

            if naming_choice == 1:
                filename = f"{keyword}_{downloaded_count + 1}"
            else:
                filename = f"{total_downloaded + downloaded_count + 1}"

            print(f"{Fore.YELLOW}📥 Downloading {downloaded_count + 1}/{images_per_keyword}: {filename}")
            if download_image(image_url, keyword_folder, filename, desired_format, min_quality):
                downloaded_count += 1
                tracker.success()
            else:
                tracker.skip()
            time.sleep(random.uniform(0.5, 1.5))

        print(f"{Fore.GREEN}✅ Downloaded {downloaded_count}/{images_per_keyword} images for '{keyword}'")

        if downloaded_count > 0:
            total_downloaded += downloaded_count
        else:
            failed_keywords.append(keyword)

    print(f"\n{Fore.GREEN}{Style.BRIGHT}{'=' * 60}")
    print(f"{Fore.GREEN}{Style.BRIGHT}📊 DOWNLOAD COMPLETE!")
    print(f"{Fore.GREEN}{Style.BRIGHT}{'=' * 60}")

    print(f"{Fore.CYAN}📁 Download location: {Fore.WHITE}{folder_path}")
    print(f"{Fore.CYAN}✅ Images downloaded: {Fore.GREEN}{total_downloaded}")
    print(f"{Fore.CYAN}❌ Failed keywords: {Fore.RED}{len(failed_keywords)}")
    print(f"{Fore.CYAN}📝 Total keywords: {Fore.BLUE}{len(keywords)}")
    print(f"{Fore.MAGENTA}🔄 Duplicates skipped: {Fore.YELLOW}{_dedup.duplicates_skipped}")

    if failed_keywords:
        print(f"\n{Fore.RED}Failed keywords: {', '.join(failed_keywords)}")

    success_rate = (len(keywords) - len(failed_keywords)) / len(keywords) * 100 if keywords else 0
    print(f"{Fore.CYAN}🎯 Success rate: {Fore.GREEN}{success_rate:.1f}%")
    print(f"{Fore.CYAN}⏱  Time elapsed: {Fore.GREEN}{tracker.elapsed:.1f}s")
    print(f"\n{Fore.YELLOW}💡 Tip: Check the subfolders in '{os.path.basename(folder_path)}' for your images!")

    input(f"\n{Fore.YELLOW}Press Enter to exit...{Style.RESET_ALL}")


# ##############################################
# MODE 3: Dork Runner
# ##############################################
def mode_dork_runner():
    """Run dorks across multiple search engines, save URL lists to .txt files."""
    print(f"""
{Fore.CYAN}{Style.BRIGHT}╔══════════════════════════════════════════════════════════════╗
║                    🎯  DORK RUNNER                             ║
║                                                              ║
║       Run Google dorks across DDG/Bing/Serper/Chrome         ║
║               Save collected URLs to text files              ║
╚══════════════════════════════════════════════════════════════╝{Style.RESET_ALL}
""")

    print(f"{Fore.CYAN}Use the default dork list or provide your own?")
    print("1) Use default dork list")
    print("2) Enter custom dorks (comma-separated)")
    print("3) Load dorks from a .txt file (one per line)")

    choice = input(f"{Fore.YELLOW}Select an option (1-3): {Style.RESET_ALL}").strip()

    if choice == '2':
        text = input(f"{Fore.YELLOW}Enter comma-separated dorks: {Style.RESET_ALL}").strip()
        dorks = [d.strip() for d in text.split(',') if d.strip()]
    elif choice == '3':
        filepath = input(f"{Fore.YELLOW}Enter the path to the .txt file: {Style.RESET_ALL}").strip().strip('"').strip("'")
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                dorks = [line.strip() for line in f if line.strip()]
        except Exception as e:
            print(f"{Fore.RED}Error reading file: {e}")
            dorks = []
    else:
        dorks = list(DEFAULT_DORKS)

    if not dorks:
        print(f"{Fore.RED}No dorks to run. Exiting.")
        return

    output_dir = Path("search_results")
    output_dir.mkdir(exist_ok=True)

    use_chrome = input(f"{Fore.YELLOW}Enable Chrome fallback for low-result dorks? (y/n): {Style.RESET_ALL}").strip().lower() == 'y'

    start_time = time.time()

    print(f"\n{'=' * 60}")
    print(f"Multi-Method Search — Dork Runner")
    print(f"Dorks to process: {len(dorks)}")
    print(f"Chrome fallback: {'Enabled' if use_chrome else 'Disabled'}")
    print(f"{'=' * 60}\n")

    total_urls_found = 0

    for dork_idx, dork in enumerate(dorks):
        print(f"\n{'=' * 60}")
        print(f"[{dork_idx + 1}/{len(dorks)}] Processing: {dork}")
        print(f"{'=' * 60}\n")

        all_results = set()

        ddg_results = search_duckduckgo(dork, max_results=50)
        all_results.update(ddg_results)
        if len(all_results) >= MIN_ACCEPTABLE_RESULTS:
            print(f"{Fore.GREEN}[Success] Found {len(all_results)} results. Skipping other methods.")
            save_results(dork, all_results, output_dir)
            total_urls_found += len(all_results)
            continue

        time.sleep(2)

        bing_results = search_bing(dork, num_pages=5)
        all_results.update(bing_results)
        if len(all_results) >= MIN_ACCEPTABLE_RESULTS:
            print(f"{Fore.GREEN}[Success] Found {len(all_results)} results. Skipping other methods.")
            save_results(dork, all_results, output_dir)
            total_urls_found += len(all_results)
            continue

        time.sleep(2)

        if SERPER_API_KEY and len(all_results) < 5:
            serper_results = search_serper(dork, SERPER_API_KEY)
            all_results.update(serper_results)
            if len(all_results) >= MIN_ACCEPTABLE_RESULTS:
                save_results(dork, all_results, output_dir)
                total_urls_found += len(all_results)
                continue
            time.sleep(2)

        if use_chrome and len(all_results) < MIN_ACCEPTABLE_RESULTS:
            print(f"\n{Fore.YELLOW}[Info] APIs didn't yield enough, falling back to ChromeDriver...")
            chrome_results = search_chrome(dork, num_pages=5)
            all_results.update(chrome_results)

        save_results(dork, all_results, output_dir)
        total_urls_found += len(all_results)
        time.sleep(5)

    elapsed = time.time() - start_time
    print(f"\n{Fore.GREEN}{Style.BRIGHT}🎉 Dork Runner complete!")
    print(f"{Fore.CYAN}  📂 Results saved to: {output_dir}")
    print(f"{Fore.CYAN}  🔗 Total URLs found: {total_urls_found}")
    print(f"{Fore.CYAN}  ⏱  Time elapsed: {elapsed:.1f}s")
    input("\nPress ENTER to exit...")


# ##############################################
# CLI ARGUMENT PARSER
# ##############################################
def create_argument_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser with enhanced flags."""
    parser = argparse.ArgumentParser(
        description="Unified Search Toolkit v2 - Enhanced with Tor, caching, and state persistence",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --mode 1 --query "yearbook 2024" --use-tor
  python main.py --mode 2 --query "nature wallpapers" --resume
  python main.py --mode 3 --dorks-file data/search.txt --state-dir ./mystate
  python main.py  # Interactive mode
        """
    )
    
    # Core mode selection
    parser.add_argument("--mode", type=int, choices=[1, 2, 3],
                       help="Operation mode: 1=Search&Extract, 2=BingImages, 3=DorkRunner")
    parser.add_argument("--query", type=str,
                       help="Search query (for mode 1/2)")
    parser.add_argument("--dorks-file", type=str,
                       help="Path to dorks file (for mode 3, one dork per line)")
    
    # Enhanced features
    parser.add_argument("--use-tor", action="store_true",
                       help="Enable Tor proxy for request rotation and circuit rotation on rate limits")
    parser.add_argument("--resume", action="store_true",
                       help="Resume from last checkpoint (uses state persistence)")
    parser.add_argument("--state-dir", type=str,
                       help="Custom state directory path (default: ./state)")
    
    # Caching options
    parser.add_argument("--cache-ttl", type=int, default=24,
                       help="Cache TTL in hours (default: 24)")
    parser.add_argument("--no-cache", action="store_true",
                       help="Disable search result caching")
    
    # Rate limiting options
    parser.add_argument("--rate-limit-delay", type=float, default=2.0,
                       help="Base delay between requests to same domain in seconds (default: 2.0)")
    
    # Output options
    parser.add_argument("--output-dir", type=str,
                       help="Output directory for downloads (overrides interactive prompt)")
    
    # State management options
    parser.add_argument("--reset-state", action="store_true",
                       help="Clear all download history and cache before running.")
    parser.add_argument("--stats", action="store_true",
                       help="Show download stats and API usage summary, then exit.")
    
    # Backward compatibility
    parser.add_argument("legacy_mode", nargs="?", type=str,
                       help=argparse.SUPPRESS)  # Hidden positional arg for backward compat
    
    return parser


def parse_cli_args() -> argparse.Namespace:
    """Parse CLI arguments with backward compatibility."""
    parser = create_argument_parser()
    
    # Handle backward compatibility: python search_toolkit.py 1
    if len(sys.argv) == 2 and sys.argv[1].isdigit() and sys.argv[1] in ['1', '2', '3']:
        args = parser.parse_args([])
        args.mode = int(sys.argv[1])
        return args
    
    return parser.parse_args()


# ##############################################
# MAIN MENU
# ##############################################
def main(mode=None):
    """Main entry point with mode selection. Pass mode=1/2/3 to skip menu."""
    global _cli_args
    
    # Parse CLI arguments if not already parsed
    if _cli_args is None:
        _cli_args = parse_cli_args()
    
    # Handle --stats flag (show stats and exit)
    if _cli_args.stats:
        init_enhanced_components(_cli_args)
        stats = _state_manager.get_stats()
        api = _state_manager.get_api_usage_summary()
        print(f"Downloads: {stats}")
        print(f"API Usage: {api}")
        return
    
    # Handle --reset-state flag
    if _cli_args.reset_state:
        init_enhanced_components(_cli_args)
        _state_manager.clear_all()
        print(f"{Fore.YELLOW}State cleared.{Style.RESET_ALL}")
        return
    
    # Determine mode
    if mode is not None:
        selected_mode = str(mode)
    elif _cli_args.mode is not None:
        selected_mode = str(_cli_args.mode)
    else:
        selected_mode = None
    
    # Show interactive menu if no mode specified
    if selected_mode is None:
        print(f"""
{Fore.CYAN}{Style.BRIGHT}╔══════════════════════════════════════════════════════════════╗
║                  🔧  UNIFIED SEARCH TOOLKIT v2                 ║
╚══════════════════════════════════════════════════════════════╝{Style.RESET_ALL}

{Fore.CYAN}Select a mode:{Style.RESET_ALL}

  {Fore.GREEN}1){Style.RESET_ALL} 🔍 Search & Extract
     Multi-engine search → download images/PDFs → convert to JPG
     Features: spidering, parallel downloads, dedup

  {Fore.GREEN}2){Style.RESET_ALL} 🖼️  Bing Image Downloader
     Download images from Bing with format/quality filters

  {Fore.GREEN}3){Style.RESET_ALL} 🎯 Dork Runner
     Run Google dorks across engines, save URL lists to text files
""")
        selected_mode = input(f"{Fore.YELLOW}Enter your choice (1-3): {Style.RESET_ALL}").strip()
    
    # Initialize enhanced components
    init_enhanced_components(_cli_args)
    
    try:
        if selected_mode == '1':
            mode_search_extract()
        elif selected_mode == '2':
            mode_bing_images()
        elif selected_mode == '3':
            mode_dork_runner()
        else:
            print(f"{Fore.RED}Invalid choice. Please run again and select 1, 2, or 3.")
    finally:
        # Always cleanup
        cleanup_enhanced_components()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}Interrupted by user.")
    except Exception as e:
        print(f"\n{Fore.RED}Critical Error: {e}")
        traceback.print_exc()
        input("Press ENTER to exit...")

