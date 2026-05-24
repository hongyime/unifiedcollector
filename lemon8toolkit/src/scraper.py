"""
Unified Lemon8 Toolkit - Web Scraping Functions
Enhanced with pylemon8 integration for improved reliability and performance
"""
import re
import os
import random
import requests
import time
import html
from http.cookiejar import LoadError, MozillaCookieJar
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse
from typing import List, Dict, Optional, Set, Any
import json
from rate_limiter import AdaptiveRateLimiter

# Import pylemon8 library with comprehensive error handling
PYLEMON8_AVAILABLE = False
Lemon8 = None

# Temporarily disable pylemon8 to ensure reliability across all systems
# You can enable this later when the pylemon8 library is more stable
USE_PYLEMON8 = True  # Set to True to re-enable pylemon8 integration

if USE_PYLEMON8:
    try:
        from lemon8 import Lemon8 as _Lemon8
        # Test that the class can be instantiated without errors  
        test_instance = _Lemon8(region='sg')
        # If we get here, pylemon8 is working
        Lemon8 = _Lemon8
        PYLEMON8_AVAILABLE = True
        print("✅ pylemon8 library loaded and tested successfully")
    except Exception as e:
        PYLEMON8_AVAILABLE = False
        Lemon8 = None
        print(f"⚠️ pylemon8 not available (falling back to web scraping): {type(e).__name__}")
        print("💡 This is normal - the toolkit will use reliable web scraping methods")
else:
    print("🌐 Using web scraping mode for maximum compatibility")
    print("💡 To enable pylemon8 integration, set USE_PYLEMON8 = True in lemon8_scraper.py")

from config import (
    FEED_URL,
    INCLUDE_PROFILE_IMAGES_IN_FEED,
    INCLUDE_PROFILE_IMAGES_IN_USER_SCRAPES,
    LEMON8_BASE_URL,
    MAX_DELAY,
    MAX_USER_PROFILE_PAGES,
    MIN_DELAY,
    PROFILE_PHOTO_DOWNLOAD_ENABLED,
    USER_AGENT,
    get_tag_url,
    get_user_url,
)


def _interruptible_sleep(seconds: float, check_interval: float = 0.2) -> None:
    """Sleep in short slices so Ctrl+C interrupts long waits quickly."""
    if seconds <= 0:
        return
    end_time = time.time() + seconds
    while True:
        remaining = end_time - time.time()
        if remaining <= 0:
            return
        time.sleep(min(check_interval, remaining))

class Lemon8Scraper:
    ROTATING_HEADER_PROFILES = [
        {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-GB,en;q=0.7,en-US;q=0.6',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        },
        {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Mobile/15E148 Safari/604.1',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.8',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        },
        {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-GB,en;q=0.7,en-US;q=0.6',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        },
        {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        },
        {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        },
    ]

    def __init__(self, cookie_file: Optional[str] = None):
        # Initialize traditional scraper session with retry adapter
        self.session = requests.Session()
        _retry = Retry(total=3, backoff_factor=1.0, status_forcelist={429, 500, 502, 503, 504}, allowed_methods={"GET"}, raise_on_status=False)
        _adapter = HTTPAdapter(max_retries=_retry)
        self.session.mount("https://", _adapter)
        self.session.mount("http://", _adapter)
        self.lemon8_api = None
        self.api_user_endpoint_blocked = False
        self.cookie_file_path = self._resolve_cookie_file_path(cookie_file)
        self.loaded_cookie_summary: Dict[str, Any] = {'source': None, 'cookie_count': 0, 'tt_webid': None}
        
        # Initialize rate limiter with conservative delays for Lemon8's strict rate limiting
        # GLOBAL rate limiting - when ANY request gets 403, ALL requests slow down
        self.rate_limiter = AdaptiveRateLimiter(
            base_delay=3.0,
            min_delay=2.0,
            max_delay=120.0,  # Up to 2 minutes for aggressive backoff
            success_threshold=5,
            delay_reduction=0.5,
            jitter=0.3,
            forbidden_backoff=30.0  # Jump to 30s on first 403
        )
        print("✅ Rate limiter initialized with base_delay=3.0s, jitter=±30%, 403_backoff=30s")
        
        # Use mobile user agent for better content access
        self._apply_rotating_headers(endpoint_kind='page')
        self._load_cookies_into_session()
        
        # Initialize pylemon8 if available
        if PYLEMON8_AVAILABLE and Lemon8:
            try:
                api_cookie = self.loaded_cookie_summary.get('tt_webid')
                self.lemon8_api = Lemon8(region='sg', cookie=api_cookie)
                if hasattr(self.lemon8_api, 'session'):
                    self.lemon8_api.session.headers.update(self._build_rotating_headers(endpoint_kind='api'))
                    self.lemon8_api.session.cookies.update(self.session.cookies)
                print("✅ pylemon8 API client initialized")
            except Exception as e:
                print(f"⚠️ Failed to initialize pylemon8 API: {e}")
                print("🔄 Falling back to web scraping methods")
                self.lemon8_api = None
    
    def human_sleep(self, min_seconds: float = 1.0, max_seconds: float = 3.0):
        """
        Sleep for a random duration to simulate human behavior between operations.
        Use this between processing different users, not for HTTP requests (use rate_limiter for that).
        
        Args:
            min_seconds: Minimum sleep duration
            max_seconds: Maximum sleep duration
        """
        sleep_time = random.uniform(min_seconds, max_seconds)
        print(f"😴 Human-like pause: {sleep_time:.2f}s")
        time.sleep(sleep_time)

    def _resolve_cookie_file_path(self, cookie_file: Optional[str] = None) -> Optional[Path]:
        """Resolve a cookies.txt file from an explicit path, environment variable, or common defaults."""
        candidates: List[Path] = []

        if cookie_file:
            candidates.append(Path(cookie_file).expanduser())

        env_cookie_file = os.getenv('LEMON8_COOKIE_FILE')
        if env_cookie_file:
            candidates.append(Path(env_cookie_file).expanduser())

        candidates.extend([
            Path.cwd() / 'cookies.txt',
            Path(__file__).resolve().parent / 'cookies.txt',
        ])

        for candidate in candidates:
            try:
                if candidate and candidate.is_file():
                    return candidate
            except OSError:
                continue

        return None

    def _extract_tt_webid_from_cookiejar(self, cookiejar) -> Optional[str]:
        """Extract tt_webid from any cookiejar-like object."""
        try:
            for cookie in cookiejar:
                if getattr(cookie, 'name', None) == 'tt_webid' and getattr(cookie, 'value', None):
                    return str(cookie.value)
        except (AttributeError, TypeError, StopIteration):
            return None
        return None

    def _load_cookie_file(self, cookie_path: Path) -> Dict[str, Any]:
        """Load cookies from a Netscape/Mozilla cookies.txt file or simple name=value text."""
        loaded_summary = {'source': str(cookie_path), 'cookie_count': 0, 'tt_webid': None}

        try:
            first_line = cookie_path.read_text(encoding='utf-8', errors='ignore').splitlines()[0] if cookie_path.stat().st_size > 0 else ''
            if 'Netscape HTTP Cookie File' not in first_line and 'HTTP Cookie File' not in first_line:
                print(f"⚠️ Cookie file may not be Netscape format (header: {first_line[:60]!r}); attempting parse anyway")
        except (OSError, IndexError):
            pass

        try:
            jar = MozillaCookieJar(str(cookie_path))
            jar.load(ignore_discard=True, ignore_expires=True)
            self.session.cookies.update(jar)
            loaded_summary['cookie_count'] = len(list(jar))
            loaded_summary['tt_webid'] = self._extract_tt_webid_from_cookiejar(jar)
            return loaded_summary
        except (OSError, LoadError, ValueError) as e:
            print(f"⚠️ MozillaCookieJar load failed, trying text fallback: {e}")

        # Fallback parser for simple cookie dumps like "name=value" per line.
        try:
            raw_text = cookie_path.read_text(encoding='utf-8', errors='ignore')
        except (OSError, UnicodeDecodeError) as e:
            print(f"⚠️ Could not read cookie file: {e}")
            return loaded_summary

        cookies_loaded = 0
        tt_webid = None

        for line in raw_text.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            parts = line.split('\t')
            if len(parts) >= 7:
                # Netscape-style line: domain, flag, path, secure, expires, name, value
                name = parts[5].strip()
                value = parts[6].strip()
                domain = parts[0].strip()
                path = parts[2].strip() or '/'
            elif '=' in line:
                name, value = line.split('=', 1)
                name = name.strip()
                value = value.strip()
                domain = None
                path = '/'
            else:
                continue

            if name:
                self.session.cookies.set(name, value, domain=domain, path=path)
                cookies_loaded += 1
                if name == 'tt_webid' and value:
                    tt_webid = value

        loaded_summary['cookie_count'] = cookies_loaded
        loaded_summary['tt_webid'] = tt_webid
        return loaded_summary

    def _load_cookies_into_session(self) -> None:
        """Load cookies from a cookie file if one is available."""
        if not self.cookie_file_path:
            return

        try:
            self.loaded_cookie_summary = self._load_cookie_file(self.cookie_file_path)
            if self.loaded_cookie_summary.get('cookie_count', 0) > 0:
                print(
                    f"🍪 Loaded {self.loaded_cookie_summary['cookie_count']} cookies from "
                    f"{self.loaded_cookie_summary['source']}"
                )
                if self.loaded_cookie_summary.get('tt_webid'):
                    print("🔐 Found tt_webid cookie for authenticated requests")
        except Exception as e:
            print(f"⚠️ Failed to load cookie file {self.cookie_file_path}: {e}")

    def _build_rotating_headers(self, endpoint_kind: str = 'page', referer: Optional[str] = None) -> Dict[str, str]:
        """Build a randomized, browser-like header set for the next request."""
        headers = dict(random.choice(self.ROTATING_HEADER_PROFILES))

        if endpoint_kind == 'api':
            headers['Accept'] = 'application/json, text/plain, */*'
            headers['X-Requested-With'] = 'XMLHttpRequest'

        headers['Accept-Encoding'] = 'gzip, deflate, br'

        if referer:
            headers['Referer'] = referer

        return headers

    def _apply_rotating_headers(self, endpoint_kind: str = 'page', referer: Optional[str] = None) -> None:
        """Apply a fresh header profile to the session before a request."""
        self.session.headers.update(self._build_rotating_headers(endpoint_kind=endpoint_kind, referer=referer))
        if getattr(self, 'lemon8_api', None) and hasattr(self.lemon8_api, 'session'):
            self.lemon8_api.session.headers.update(self._build_rotating_headers(endpoint_kind='api', referer=referer))
    
    def _make_request_with_retry(
        self,
        url: str,
        max_retries: int = 3,
        account: str = 'default',
        referer: Optional[str] = None,
        timeout: int = 30
    ) -> requests.Response:
        """
        Make an HTTP GET request with rate limiting, retry logic, and exponential backoff.
        
        Args:
            url: The URL to request
            max_retries: Maximum number of retry attempts (default: 3)
            account: Account identifier for rate limiting (default: 'default')
            referer: Optional referer header value
            timeout: Request timeout in seconds (default: 30)
            
        Returns:
            requests.Response object on success
            
        Raises:
            requests.exceptions.HTTPError: After all retries exhausted or on non-retryable errors
            requests.exceptions.RequestException: On connection/timeout errors
        """
        last_exception = None
        
        for attempt in range(max_retries):
            try:
                # Wait with rate limiting before each attempt (uses base delay + jitter)
                self.rate_limiter.wait(account)
                
                # Apply rotating headers with referer
                self._apply_rotating_headers(endpoint_kind='page', referer=referer)
                
                # Make the request
                response = self.session.get(url, timeout=timeout)
                
                # Check status code
                if response.status_code >= 200 and response.status_code < 300:
                    # Success - record it and return
                    self.rate_limiter.record_success(account)
                    return response
                elif response.status_code == 429:
                    # Rate limit - just log and retry (don't modify delay during retries)
                    print(f"⚠️ Rate limit (429) on attempt {attempt + 1}/{max_retries} for {url}")
                    last_exception = requests.exceptions.HTTPError(f"429 Rate Limit: {url}", response=response)
                    if attempt < max_retries - 1:
                        continue
                    # Last attempt - record rate limit for next request
                    self.rate_limiter.record_rate_limit(account)
                    break
                elif response.status_code == 403:
                    # Forbidden - just log and retry (don't modify delay during retries)
                    print(f"⚠️ Forbidden (403) on attempt {attempt + 1}/{max_retries} for {url}")
                    last_exception = requests.exceptions.HTTPError(f"403 Forbidden: {url}", response=response)
                    if attempt < max_retries - 1:
                        continue
                    # Last attempt - record error for next request
                    self.rate_limiter.record_error(account)
                    break
                else:
                    # Other 4xx/5xx errors - don't retry, raise immediately
                    response.raise_for_status()
                    
            except requests.exceptions.HTTPError as e:
                # If we already set last_exception (429/403), keep it
                if last_exception is None:
                    last_exception = e
                # For non-retryable errors, raise immediately
                # Use `is not None` — Response.__bool__ returns False for 4xx,
                # which would silently swallow the raise for 404 responses.
                if e.response is not None and e.response.status_code not in [403, 429]:
                    raise
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                # Connection/timeout errors - retry
                print(f"⚠️ Connection error on attempt {attempt + 1}/{max_retries}: {type(e).__name__}")
                last_exception = e
                if attempt < max_retries - 1:
                    continue
            except requests.exceptions.RequestException as e:
                # Other request exceptions - don't retry
                raise
        
        # All retries exhausted - raise the last exception
        if last_exception:
            print(f"❌ All {max_retries} retry attempts exhausted for {url}")
            raise last_exception
        else:
            # Should not reach here, but just in case
            raise requests.exceptions.HTTPError(f"Request failed after {max_retries} attempts: {url}")
    
    def _extract_media_urls(self, html_content: str) -> List[str]:
        """
        Enhanced media URL extraction from HTML content
        Looks for various patterns including JSON data, script tags, and HTML elements
        """
        media_urls = []
        
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Method 1: Extract from JSON data in script tags
            script_tags = soup.find_all('script', {'type': 'application/json'})
            for script in script_tags:
                try:
                    json_data = json.loads(script.string or '{}')
                    media_urls.extend(self._extract_urls_from_json(json_data))
                except (json.JSONDecodeError, AttributeError):
                    continue
            
            # Method 2: Extract from inline script content AND full HTML
            # Look for URL patterns in the entire HTML content
            import re
            url_patterns = [
                # TikTok CDN and ByteImg patterns (most important for Lemon8)
                r'"(https?://[^"]*tiktokcdn[^"]*)"',
                r'"(https?://[^"]*byteimg[^"]*)"',
                r'"(https?://[^"]*muscdn[^"]*)"',
                # Direct media file patterns
                r'"(https?://[^"]*\.(mp4|jpg|jpeg|png|gif|webm|m4v)[^"]*)"',
                r"'(https?://[^']*\.(mp4|jpg|jpeg|png|gif|webm|m4v)[^']*)'",
                # Lemon8 specific patterns
                r'"(https?://[^"]*lemon8[^"]*\.(mp4|jpg|jpeg|png|gif)[^"]*)"'
            ]
            
            # Search in full HTML content for better coverage
            for pattern in url_patterns:
                matches = re.findall(pattern, html_content, re.IGNORECASE)
                for match in matches:
                    url = match[0] if isinstance(match, tuple) else match
                    if self._is_valid_media_url(url):
                        media_urls.append(url)
            
            # Also search in individual script tags for additional coverage
            script_tags = soup.find_all('script')
            for script in script_tags:
                if script.string:
                    content = script.string
                    for pattern in url_patterns:
                        matches = re.findall(pattern, content, re.IGNORECASE)
                        for match in matches:
                            url = match[0] if isinstance(match, tuple) else match
                            if self._is_valid_media_url(url):
                                media_urls.append(url)
            
            # Method 3: Traditional HTML element extraction (enhanced)
            # Extract video URLs from <source> tags
            video_sources = soup.find_all('source', {'src': True})
            for source in video_sources:
                src = source.get('src')
                if self._is_valid_media_url(src):
                    media_urls.append(src)
            
            # Extract video URLs from <video> tags
            videos = soup.find_all('video', {'src': True})
            for video in videos:
                src = video.get('src')
                if self._is_valid_media_url(src):
                    media_urls.append(src)
            
            # Extract image URLs from <img> tags (more flexible)
            images = soup.find_all('img', {'src': True})
            for img in images:
                src = img.get('src')
                if self._is_valid_media_url(src) and not self._is_small_image(src):
                    media_urls.append(src)
            
            # Look for data-src attributes (lazy loading)
            lazy_elements = soup.find_all(['img', 'video', 'source'], {'data-src': True})
            for element in lazy_elements:
                src = element.get('data-src')
                if self._is_valid_media_url(src):
                    media_urls.append(src)
            
            # Method 4: Extract from data attributes and other patterns
            all_elements = soup.find_all(attrs={'data-url': True})
            for element in all_elements:
                url = element.get('data-url')
                if self._is_valid_media_url(url):
                    media_urls.append(url)
            
            # Clean URLs and remove duplicates while preserving order
            unique_urls = []
            seen = set()
            for url in media_urls:
                # Clean the URL by removing query parameters
                clean_url = self._clean_media_url(url)
                if clean_url and clean_url not in seen:
                    unique_urls.append(clean_url)
                    seen.add(clean_url)
            
            print(f"🎬 Extracted {len(unique_urls)} media URLs from page")
            if len(unique_urls) > 0:
                print(f"📋 Sample URLs (cleaned): {unique_urls[:3]}")  # Show first 3 URLs for debugging
            
        except Exception as e:
            print(f"⚠️ Error extracting media URLs: {e}")
        
        return unique_urls

    def _normalize_username(self, value: Optional[str]) -> Optional[str]:
        """Normalize a discovered username into a safe, consistent form."""
        if not value or not isinstance(value, str):
            return None

        username = value.strip().strip('@').lower()
        username = re.sub(r'[^a-z0-9._]+', '', username)
        return username or None

    def _extract_urls_from_resource_value(self, value: Any) -> List[str]:
        """Extract URLs from common Lemon8 resource shapes."""
        urls: List[str] = []

        if isinstance(value, str):
            if self._is_valid_media_url(value):
                urls.append(value)
        elif isinstance(value, dict):
            url_list = value.get('urlList')
            if isinstance(url_list, list):
                for item in url_list:
                    urls.extend(self._extract_urls_from_resource_value(item))
            else:
                for key in ['url', 'uri', 'src', 'playAddr']:
                    if key in value:
                        urls.extend(self._extract_urls_from_resource_value(value[key]))
        elif isinstance(value, list):
            for item in value:
                urls.extend(self._extract_urls_from_resource_value(item))

        unique_urls: List[str] = []
        seen = set()
        for url in urls:
            cleaned_url = self._clean_media_url(url)
            if cleaned_url and cleaned_url not in seen:
                unique_urls.append(cleaned_url)
                seen.add(cleaned_url)

        return unique_urls

    def _extract_profile_photo_urls_from_author(self, author_info: Dict[str, Any]) -> List[str]:
        """Extract profile-photo style URLs from author metadata."""
        if not isinstance(author_info, dict):
            return []

        urls: List[str] = []
        avatar_keys = [
            'avatar',
            'avatarLarger',
            'avatarLarge',
            'avatarMedium',
            'avatarThumb',
            'avatarUrl',
            'profilePhoto',
            'profileImage',
        ]

        for key in avatar_keys:
            if key in author_info:
                urls.extend(self._extract_urls_from_resource_value(author_info[key]))

        unique_urls: List[str] = []
        seen = set()
        for url in urls:
            if url and url not in seen:
                unique_urls.append(url)
                seen.add(url)

        return unique_urls

    def _is_profile_photo_url(self, url: str) -> bool:
        """Detect profile-photo style URLs without matching generic post names."""
        if not url:
            return False
        url_lower = url.lower()
        return any(
            token in url_lower
            for token in ['user-avatar', 'avatar', 'profile_photo', 'profile-photo', 'profile_pic', 'profile-image']
        )

    def _resolve_include_profile_images(
        self,
        include_profile_images: Optional[bool],
        default_enabled: bool,
    ) -> bool:
        """Resolve an optional override against the configured default."""
        if include_profile_images is None:
            return default_enabled
        return bool(include_profile_images)

    def _extract_username_from_author(self, author_info: Any) -> Optional[str]:
        """Extract the best available author handle from nested author metadata."""
        if not isinstance(author_info, dict):
            return None

        for key in [
            'uniqueId',
            'username',
            'userName',
            'screenName',
            'handle',
            'displayName',
            'linkName',
            'userId',
            'uid',
            'secUid',
            'nickName',
        ]:
            value = author_info.get(key)
            if isinstance(value, str) and value.strip():
                return self._normalize_username(value)

        nested_value = self._find_key_in_json(
            author_info,
            [
                'uniqueId',
                'username',
                'userName',
                'screenName',
                'handle',
                'displayName',
                'linkName',
                'userId',
                'uid',
                'secUid',
                'nickName',
            ],
        )
        if isinstance(nested_value, str) and nested_value.strip():
            return self._normalize_username(nested_value)

        return None

    def _extract_username_from_item(self, item: Dict[str, Any]) -> Optional[str]:
        """Extract an item-level username without forcing placeholder prefixes."""
        author = item.get('authorInfo') or item.get('author') or item.get('user') or {}
        username = self._extract_username_from_author(author)
        if username:
            return username

        for key in ['uniqueId', 'username', 'userName', 'authorId', 'linkName', 'userId', 'uid']:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return self._normalize_username(value)

        return None
    
    def _extract_user_id_from_author(self, author_info: Any) -> Optional[str]:
        """Extract the numeric user_id from author metadata."""
        if not isinstance(author_info, dict):
            return None

        # Try common user ID keys
        for key in ['userId', 'uid', 'user_id', 'id', 'secUid']:
            value = author_info.get(key)
            if value is not None:
                # Convert to string and check if it's numeric or alphanumeric ID
                str_value = str(value).strip()
                if str_value and (str_value.isdigit() or len(str_value) > 10):
                    return str_value

        # Try nested search
        nested_value = self._find_key_in_json(
            author_info,
            ['userId', 'uid', 'user_id', 'id', 'secUid'],
        )
        if nested_value is not None:
            str_value = str(nested_value).strip()
            if str_value and (str_value.isdigit() or len(str_value) > 10):
                return str_value

        return None
    
    def _extract_user_id_from_item(self, item: Dict[str, Any]) -> Optional[str]:
        """Extract user_id from an item."""
        # Try author info first
        author = item.get('authorInfo') or item.get('author') or item.get('user') or {}
        user_id = self._extract_user_id_from_author(author)
        if user_id:
            return user_id

        # Try direct keys on item
        for key in ['userId', 'uid', 'user_id', 'authorId', 'secUid']:
            value = item.get(key)
            if value is not None:
                str_value = str(value).strip()
                if str_value and (str_value.isdigit() or len(str_value) > 10):
                    return str_value

        return None

    def _build_media_item(
        self,
        url: str,
        username: Optional[str] = None,
        is_profile_photo: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Create a normalized media descriptor."""
        cleaned_url = self._clean_media_url(url)
        if not cleaned_url or not self._is_valid_media_url(cleaned_url):
            return None

        normalized_username = None
        if isinstance(username, str) and username.strip():
            normalized_username = self._normalize_username(username)

        item: Dict[str, Any] = {
            'url': cleaned_url,
            'username': normalized_username,
            'is_profile_photo': is_profile_photo,
            'media_type': 'image' if any(
                ext in cleaned_url.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp']
            ) else 'video',
        }
        return item

    def _deduplicate_media_items(self, media_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Remove duplicate media descriptors by URL while keeping richer metadata."""
        unique_items_by_url: Dict[str, Dict[str, Any]] = {}

        for item in media_items:
            url = item.get('url')
            if not url:
                continue

            existing_item = unique_items_by_url.get(url)
            if existing_item is None:
                unique_items_by_url[url] = dict(item)
                continue

            if not existing_item.get('username') and item.get('username'):
                existing_item['username'] = item['username']
            if not existing_item.get('is_profile_photo') and item.get('is_profile_photo'):
                existing_item['is_profile_photo'] = True
            if existing_item.get('media_type') != 'video' and item.get('media_type') == 'video':
                existing_item['media_type'] = 'video'

        return list(unique_items_by_url.values())

    def _find_item_lists_in_json(self, data: Any, found_lists: Optional[List[List[Dict[str, Any]]]] = None) -> List[List[Dict[str, Any]]]:
        """Recursively locate post-item lists in structured JSON payloads."""
        if found_lists is None:
            found_lists = []

        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
                    first_item = value[0]
                    if any(
                        field in first_item
                        for field in [
                            'authorInfo',
                            'author',
                            'user',
                            'imageResource',
                            'imageList',
                            'videoResource',
                            'video',
                            'videoList',
                            'coverResource',
                            'largeImage',
                            'coverImage',
                        ]
                    ):
                        found_lists.append(value)
                if isinstance(value, (dict, list)):
                    self._find_item_lists_in_json(value, found_lists)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    self._find_item_lists_in_json(item, found_lists)

        return found_lists
    
    def _extract_urls_from_json(self, data, urls=None) -> List[str]:
        """Recursively extract URLs from JSON data"""
        if urls is None:
            urls = []
        
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str) and self._is_valid_media_url(value):
                    urls.append(value)
                elif isinstance(value, (dict, list)):
                    self._extract_urls_from_json(value, urls)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, str) and self._is_valid_media_url(item):
                    urls.append(item)
                elif isinstance(item, (dict, list)):
                    self._extract_urls_from_json(item, urls)
        
        return urls
    
    def _clean_media_url(self, url: str) -> str:
        """
        Clean media URL by unescaping HTML entities.
        Preserves all parameters and shrinking patterns for maximum compatibility with signed URLs.
        """
        if not url:
            return ""
        
        # Unescape HTML entities like &amp; to &
        url = html.unescape(url)
        
        return url
    
    def _is_valid_media_url(self, url: str) -> bool:
        """Check if URL is a valid media URL (photos and videos only)"""
        if not url or not isinstance(url, str):
            return False
        
        # Must be HTTP/HTTPS
        if not url.startswith(('http://', 'https://')):
            return False
        
        url_lower = url.lower()
        
        # Exclude non-media files
        excluded_patterns = [
            '.js', '.css', '.json', '.xml', '.txt', '.html', '.htm',
            'favicon', 'logo', 'icon', 'sprite', 'button', 'badge',
            'sdk-web', 'slardar', 'browser.', '_assets/', 'static/css',
            'static/js', '.svg', '.woff', '.ttf', '.eot', '.otf'
        ]
        
        if any(pattern in url_lower for pattern in excluded_patterns):
            return False
        
        # Only accept URLs with actual media file extensions or from media CDN paths
        # Video extensions
        video_extensions = ['.mp4', '.webm', '.m4v', '.mov', '.avi', '.flv', '.mkv']
        
        # Image extensions  
        image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp']
        
        # Check for explicit file extensions
        has_media_extension = any(url_lower.endswith(ext) or f'{ext}?' in url_lower 
                                   for ext in video_extensions + image_extensions)
        
        # CDN patterns that indicate user content (not site assets)
        user_content_patterns = [
            'tos-alisg-i-sdweummd6v-sg',  # TikTok CDN user content
            'tos-alisg-v-a3e477-sg',       # TikTok CDN video content
            'user-avatar-alisg',            # User avatars
            '/post/',                       # Post content paths
            '/item/',                       # Item content paths
            'tplv-sdweummd6v',             # TikTok processing pipeline for user media
        ]
        
        has_user_content_pattern = any(pattern in url_lower for pattern in user_content_patterns)
        
        # URL must have either a media extension OR be from a known user content CDN path
        return has_media_extension or has_user_content_pattern
    
    def _is_small_image(self, url: str) -> bool:
        """
        Check if image URL appears to be a small thumbnail.
        Uses regex to find dimension patterns like '150:150' or '200x200'.
        """
        if not url:
            return False
        
        url_lower = url.lower()
        
        # Obvious small image indicators
        small_indicators = [
            'thumb', 'avatar', 'profile_pic', 'icon', 'favicon', 'logo'
        ]
        if any(indicator in url_lower for indicator in small_indicators):
            return True

        def dimensions_look_small(width: int, height: int) -> bool:
            dimensions = [value for value in (width, height) if value > 0]
            return bool(dimensions) and min(dimensions) < 250
        
        # Look for dimension patterns: e.g., 150x150, 150:150, width=150, etc.
        # Pattern 1: {width}x{height} (e.g., 150x150)
        dim_match = re.search(r'(\d+)x(\d+)', url_lower)
        if dim_match:
            width, height = map(int, dim_match.groups())
            if dimensions_look_small(width, height):
                return True
        
        # Pattern 2: :{width}:{height} (e.g., :150:150)
        colon_dim_match = re.search(r':(\d+):(\d+)', url_lower)
        if colon_dim_match:
            width, height = map(int, colon_dim_match.groups())
            if dimensions_look_small(width, height):
                return True
        
        # Pattern 3: width=... or height=...
        width_match = re.search(r'width=(\d+)', url_lower)
        if width_match:
            if int(width_match.group(1)) < 250:
                return True
                
        return False
    
    def _extract_media_items_from_pylemon8_items(
        self,
        items: List[Dict],
        include_profile_images: bool = False,
    ) -> List[Dict[str, Any]]:
        """Extract media descriptors with usernames from API/web item payloads."""
        media_items: List[Dict[str, Any]] = []

        try:
            for item in items:
                if not isinstance(item, dict):
                    continue

                author = item.get('authorInfo') or item.get('author') or item.get('user') or {}
                username = self._extract_username_from_item(item)

                post_media_added = False

                for video_key in ['videoResource', 'video', 'videoList', 'videoUrl', 'playAddr']:
                    video_resource = item.get(video_key)
                    if not video_resource:
                        continue

                    video_urls = self._extract_urls_from_resource_value(video_resource)
                    if not video_urls:
                        continue

                    media_item = self._build_media_item(video_urls[0], username=username)
                    if media_item:
                        media_items.append(media_item)
                        post_media_added = True

                for image_key in ['imageResource', 'imageList']:
                    image_resource = item.get(image_key)
                    if not image_resource:
                        continue

                    if isinstance(image_resource, list):
                        for image_item in image_resource:
                            image_urls = self._extract_urls_from_resource_value(image_item)
                            if image_urls:
                                media_item = self._build_media_item(
                                    image_urls[-1],
                                    username=username,
                                )
                                if media_item:
                                    media_items.append(media_item)
                                    post_media_added = True
                    else:
                        image_urls = self._extract_urls_from_resource_value(image_resource)
                        if image_urls:
                            media_item = self._build_media_item(
                                image_urls[-1],
                                username=username,
                            )
                            if media_item:
                                media_items.append(media_item)
                                post_media_added = True

                if not post_media_added:
                    for cover_key in ['coverResource', 'largeImage', 'coverImage']:
                        cover_resource = item.get(cover_key)
                        if not cover_resource:
                            continue

                        cover_urls = self._extract_urls_from_resource_value(cover_resource)
                        if cover_urls:
                            media_item = self._build_media_item(
                                cover_urls[-1],
                                username=username,
                            )
                            if media_item:
                                media_items.append(media_item)
                                post_media_added = True
                            break

                if include_profile_images and PROFILE_PHOTO_DOWNLOAD_ENABLED:
                    for avatar_url in self._extract_profile_photo_urls_from_author(author):
                        media_item = self._build_media_item(
                            avatar_url,
                            username=username,
                            is_profile_photo=True,
                        )
                        if media_item:
                            media_items.append(media_item)

        except Exception as e:
            print(f"⚠️ Error extracting media items from pylemon8 items: {e}")

        return self._deduplicate_media_items(media_items)

    def _extract_media_from_pylemon8_items(self, items: List[Dict]) -> List[str]:
        """
        Extract media URLs from pylemon8 API response items
        """
        return [item['url'] for item in self._extract_media_items_from_pylemon8_items(items)]

    def _extract_media_items_from_html(
        self,
        html_content: str,
        include_profile_images: bool = False,
    ) -> List[Dict[str, Any]]:
        """Extract structured media descriptors from embedded JSON in HTML."""
        media_items: List[Dict[str, Any]] = []

        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            script_tags = soup.find_all('script', {'type': 'application/json'})
            for script in script_tags:
                try:
                    json_data = json.loads(script.string or '{}')
                except (json.JSONDecodeError, AttributeError):
                    continue

                for items in self._find_item_lists_in_json(json_data):
                    media_items.extend(
                        self._extract_media_items_from_pylemon8_items(
                            items,
                            include_profile_images=include_profile_images,
                        )
                    )
        except Exception as e:
            print(f"⚠️ Error extracting structured media items: {e}")

        return self._deduplicate_media_items(media_items)

    def _extract_profile_photo_urls_from_html(self, html_content: str) -> List[str]:
        """Extract profile-photo URLs directly from HTML when item JSON is unavailable."""
        avatar_urls: List[str] = []
        avatar_patterns = [
            r'"(https?://[^"]*user-avatar[^"]*)"',
            r'"(https?://[^"]*(?:avatar|profile[_-]?(?:photo|image|pic))[^"]*\.(?:jpg|jpeg|png|webp)[^"]*)"',
            r"'(https?://[^']*user-avatar[^']*)'",
        ]

        for pattern in avatar_patterns:
            matches = re.findall(pattern, html_content, re.IGNORECASE)
            for match in matches:
                cleaned_url = self._clean_media_url(match)
                if cleaned_url and cleaned_url not in avatar_urls:
                    avatar_urls.append(cleaned_url)

        return avatar_urls

    def _extract_media_urls_from_fragment_html(
        self,
        fragment_html: str,
        include_small_images: bool = False,
    ) -> List[str]:
        """Extract media URLs from a smaller DOM fragment for username association."""
        urls: List[str] = []

        try:
            soup = BeautifulSoup(fragment_html, 'html.parser')

            for tag_name, attr_name in [
                ('img', 'src'),
                ('img', 'data-src'),
                ('video', 'src'),
                ('video', 'data-src'),
                ('source', 'src'),
                ('source', 'data-src'),
            ]:
                for element in soup.find_all(tag_name):
                    value = element.get(attr_name)
                    if not value or not self._is_valid_media_url(value):
                        continue
                    if tag_name == 'img' and not include_small_images and self._is_small_image(value):
                        continue
                    urls.append(self._clean_media_url(value))

            regex_patterns = [
                r'https?://[^"\']*tiktokcdn[^"\']*',
                r'https?://[^"\']*byteimg[^"\']*',
            ]
            for pattern in regex_patterns:
                for match in re.findall(pattern, fragment_html, re.IGNORECASE):
                    if self._is_valid_media_url(match):
                        if not include_small_images and self._is_small_image(match):
                            continue
                        urls.append(self._clean_media_url(match))
        except Exception as e:
            print(f"⚠️ Error extracting media URLs from fragment: {e}")

        unique_urls: List[str] = []
        seen = set()
        for url in urls:
            if url and url not in seen:
                unique_urls.append(url)
                seen.add(url)

        return unique_urls

    def _extract_media_items_from_dom(self, html_content: str) -> List[Dict[str, Any]]:
        """Associate media URLs with usernames from nearby feed card DOM nodes."""
        media_items: List[Dict[str, Any]] = []

        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            for link in soup.find_all('a', href=True):
                href = link.get('href', '')
                match = re.search(r'/@([a-zA-Z0-9_.]{3,30})', href)
                if not match:
                    continue

                username = self._normalize_username(match.group(1))
                current_node = link
                chosen_urls: List[str] = []

                for _ in range(6):
                    current_node = current_node.parent
                    if current_node is None:
                        break

                    fragment_html = str(current_node)
                    candidate_urls = self._extract_media_urls_from_fragment_html(fragment_html)
                    if 0 < len(candidate_urls) <= 8:
                        chosen_urls = candidate_urls
                        break

                for media_url in chosen_urls:
                    media_item = self._build_media_item(media_url, username=username)
                    if media_item:
                        media_items.append(media_item)
        except Exception as e:
            print(f"⚠️ Error extracting DOM-associated media items: {e}")

        return self._deduplicate_media_items(media_items)

    def _extract_media_items_from_feed_cards(
        self,
        html_content: str,
        include_profile_images: bool = False,
    ) -> List[Dict[str, Any]]:
        """Extract username-associated media directly from Lemon8 feed cards."""
        media_items: List[Dict[str, Any]] = []

        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            for card in soup.find_all('a', class_='article_card'):
                href = card.get('href', '')
                match = re.search(r'/@([a-zA-Z0-9_.]{3,30})', href)
                if not match:
                    user_link = card.find('a', href=re.compile(r'/@[a-zA-Z0-9_.]{3,30}'))
                    if user_link:
                        match = re.search(r'/@([a-zA-Z0-9_.]{3,30})', user_link.get('href', ''))
                if not match:
                    continue

                username = self._normalize_username(match.group(1))
                for image in card.find_all('img', src=True):
                    image_url = image.get('src')
                    if not image_url or not self._is_valid_media_url(image_url):
                        continue
                    if self._is_small_image(image_url):
                        if include_profile_images and self._is_profile_photo_url(image_url):
                            media_item = self._build_media_item(
                                image_url,
                                username=username,
                                is_profile_photo=True,
                            )
                            if media_item:
                                media_items.append(media_item)
                        continue

                    media_item = self._build_media_item(image_url, username=username)
                    if media_item:
                        media_items.append(media_item)
        except Exception as e:
            print(f"⚠️ Error extracting media items from feed cards: {e}")

        return self._deduplicate_media_items(media_items)
    
    def _extract_users_from_pylemon8_items(self, items: List[Dict]) -> Set[str]:
        """
        Extract user handles from pylemon8 API response items
        """
        users = set()
        
        try:
            for item in items:
                normalized_username = self._extract_username_from_item(item)
                if normalized_username:
                    users.add(normalized_username)

                # Extract author information
                author = item.get('authorInfo') or item.get('author') or item.get('user') or {}
                if isinstance(author, dict):
                    for key in ['uniqueId', 'linkName', 'username', 'userName', 'userId']:
                        value = author.get(key)
                        if isinstance(value, str) and value.strip():
                            normalized = self._normalize_username(value)
                            if normalized:
                                users.add(normalized)
                
                # Extract mentions from content
                if 'title' in item:
                    title = item['title']
                    mentions = re.findall(r'@([a-zA-Z0-9_\.]+)', title)
                    users.update(mention.lower() for mention in mentions)
                
                if 'shortContent' in item:
                    content = item['shortContent']
                    mentions = re.findall(r'@([a-zA-Z0-9_\.]+)', content)
                    users.update(mention.lower() for mention in mentions)
                        
        except Exception as e:
            print(f"⚠️ Error extracting users from pylemon8 items: {e}")
        
        return users
    
    def _extract_user_handles(self, html_content: str) -> Set[str]:
        """Enhanced user handle extraction from HTML content and JSON data"""
        handles = set()
        
        try:
            # Method 1: Look for @username patterns in text
            username_patterns = [
                r'@([a-zA-Z0-9_\.]{3,30})',
                r'"uniqueId":"([a-zA-Z0-9_\.]{3,30})"',
                r'"username":"([a-zA-Z0-9_\.]{3,30})"',
                r'"displayName":"@?([a-zA-Z0-9_\.]{3,30})"'
            ]
            
            for pattern in username_patterns:
                matches = re.findall(pattern, html_content, re.IGNORECASE)
                for match in matches:
                    match = match.strip('@').lower()
                    # Filter out common CSS/HTML keywords and system names
                    excluded = [
                        'lemon8', 'tiktok', 'admin', 'official', 'font', 'media', 
                        'keyframes', 'supports', 'import', 'charset', 'root',
                        'container', 'wrapper', 'header', 'footer', 'sidebar',
                        'content', 'article', 'section', 'button', 'input'
                    ]
                    if len(match) > 2 and match not in excluded:
                        handles.add(match)
            
            # Method 2: Extract from HTML elements and URLs
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Look for profile URL patterns
            links = soup.find_all('a', href=True)
            for link in links:
                href = link.get('href')
                if href:
                    # Extract username from URL patterns
                    url_patterns = [
                        r'/@([a-zA-Z0-9_\.]{3,30})',
                        r'/user/([a-zA-Z0-9_\.]{3,30})',
                        r'user=([a-zA-Z0-9_\.]{3,30})'
                    ]
                    
                    for pattern in url_patterns:
                        match = re.search(pattern, href)
                        if match:
                            username = match.group(1).lower()
                            if len(username) > 2:
                                handles.add(username)
            
            # Method 3: Extract from JSON data in script tags
            script_tags = soup.find_all('script', {'type': 'application/json'})
            for script in script_tags:
                try:
                    json_data = json.loads(script.string or '{}')
                    self._extract_users_from_json(json_data, handles)
                except (json.JSONDecodeError, AttributeError):
                    continue
            
            print(f"👥 Found {len(handles)} user handles") if handles else None
        
        except Exception as e:
            print(f"⚠️ Error extracting user handles: {e}")
        
        return handles
    
    def _extract_users_from_json(self, data, users_set):
        """Recursively extract usernames from JSON data"""
        if isinstance(data, dict):
            # Look for common username keys
            username_keys = ['uniqueId', 'username', 'displayName', 'authorId', 'userId']
            for key in username_keys:
                if key in data and isinstance(data[key], str):
                    username = data[key].strip('@').lower()
                    if len(username) > 2 and username.replace('_', '').replace('.', '').isalnum():
                        users_set.add(username)
            
            # Recursively check nested objects
            for value in data.values():
                if isinstance(value, (dict, list)):
                    self._extract_users_from_json(value, users_set)
                    
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    self._extract_users_from_json(item, users_set)
    
    def _extract_tag_ids(self, html_content: str) -> Set[str]:
        """Enhanced tag/topic ID extraction from HTML content and JSON data"""
        tag_ids = set()
        
        try:
            # Method 1: Look for topic/tag URL patterns
            tag_patterns = [
                r'/topic/(\d+)',
                r'"topicId":"(\d+)"',
                r'"tagId":"(\d+)"',
                r'"challengeId":"(\d+)"',
                r'topic=(\d+)',
                r'tag=(\d+)'
            ]
            
            for pattern in tag_patterns:
                matches = re.findall(pattern, html_content, re.IGNORECASE)
                for match in matches:
                    if len(match) > 5:  # Tag IDs are usually long numbers
                        tag_ids.add(match)
            
            # Method 2: Extract from HTML elements
            soup = BeautifulSoup(html_content, 'html.parser')
            links = soup.find_all('a', href=True)
            for link in links:
                href = link.get('href')
                if href:
                    # Look for various tag/topic patterns
                    url_patterns = [
                        r'/topic/(\d+)',
                        r'/tag/(\d+)', 
                        r'/challenge/(\d+)',
                        r'[?&]topic=(\d+)',
                        r'[?&]tag=(\d+)'
                    ]
                    
                    for pattern in url_patterns:
                        match = re.search(pattern, href)
                        if match and len(match.group(1)) > 5:
                            tag_ids.add(match.group(1))
            
            # Method 3: Extract from JSON data
            script_tags = soup.find_all('script', {'type': 'application/json'})
            for script in script_tags:
                try:
                    json_data = json.loads(script.string or '{}')
                    self._extract_tags_from_json(json_data, tag_ids)
                except (json.JSONDecodeError, AttributeError):
                    continue
                    
            print(f"🏷️ Found {len(tag_ids)} tag IDs") if tag_ids else None
        
        except Exception as e:
            print(f"⚠️ Error extracting tag IDs: {e}")
        
        return tag_ids
    
    def _extract_tags_from_json(self, data, tags_set):
        """Recursively extract tag IDs from JSON data"""
        if isinstance(data, dict):
            # Look for common tag ID keys
            tag_keys = ['topicId', 'tagId', 'challengeId', 'hashtag', 'topic']
            for key in tag_keys:
                if key in data:
                    value = data[key]
                    if isinstance(value, str) and value.isdigit() and len(value) > 5:
                        tags_set.add(value)
                    elif isinstance(value, (int, float)) and len(str(value)) > 5:
                        tags_set.add(str(value))
            
            # Recursively check nested objects
            for value in data.values():
                if isinstance(value, (dict, list)):
                    self._extract_tags_from_json(value, tags_set)
                    
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    self._extract_tags_from_json(item, tags_set)

    def _normalize_hashtag(self, value: str) -> Optional[str]:
        """Normalize a hashtag token into a consistent value without the # prefix."""
        if not value or not isinstance(value, str):
            return None

        hashtag = value.strip().lstrip('#').strip().lower()
        hashtag = re.sub(r'[^0-9a-zA-Z_\u00C0-\u024F\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+', '', hashtag)

        if len(hashtag) < 2:
            return None

        return hashtag

    def _extract_hashtags_from_text(self, text: str) -> Set[str]:
        """Extract hashtag tokens from free-form text."""
        hashtags = set()
        if not text or not isinstance(text, str):
            return hashtags

        matches = re.findall(
            r'(?<![\w&])#([0-9A-Za-z_\u00C0-\u024F\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]{2,60})',
            text,
            re.UNICODE,
        )
        for match in matches:
            normalized = self._normalize_hashtag(match)
            if normalized:
                hashtags.add(normalized)

        return hashtags

    def _extract_hashtags_from_json(self, data: Any, hashtags: Optional[Set[str]] = None) -> Set[str]:
        """Recursively extract caption-style hashtags from JSON payloads."""
        if hashtags is None:
            hashtags = set()

        text_like_keys = {
            'title',
            'shortcontent',
            'desc',
            'description',
            'caption',
            'content',
            'text',
            'subtitle',
        }
        direct_tag_name_keys = {
            'hashtag',
            'hashtagname',
            'hashtag_name',
            'tagname',
            'tag_name',
            'topicname',
            'topic_name',
            'challengename',
        }

        if isinstance(data, dict):
            for key, value in data.items():
                key_lower = str(key).lower()

                if isinstance(value, str):
                    if key_lower in direct_tag_name_keys:
                        normalized = self._normalize_hashtag(value)
                        if normalized:
                            hashtags.add(normalized)
                    if key_lower in text_like_keys or '#' in value:
                        hashtags.update(self._extract_hashtags_from_text(value))
                elif isinstance(value, (dict, list)):
                    self._extract_hashtags_from_json(value, hashtags)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    self._extract_hashtags_from_json(item, hashtags)
                elif isinstance(item, str):
                    hashtags.update(self._extract_hashtags_from_text(item))
        elif isinstance(data, str):
            hashtags.update(self._extract_hashtags_from_text(data))

        return hashtags

    def _extract_hashtags(self, html_content: str) -> Set[str]:
        """Extract hashtags from Lemon8 HTML/JSON content."""
        hashtags = set()

        try:
            soup = BeautifulSoup(html_content, 'html.parser')

            script_tags = soup.find_all('script', {'type': 'application/json'})
            for script in script_tags:
                try:
                    json_data = json.loads(script.string or '{}')
                    hashtags.update(self._extract_hashtags_from_json(json_data))
                except (json.JSONDecodeError, AttributeError, TypeError):
                    continue

            visible_text = soup.get_text(' ', strip=True)
            hashtags.update(self._extract_hashtags_from_text(visible_text))

            for anchor in soup.find_all('a'):
                anchor_text = anchor.get_text(strip=True)
                if anchor_text and '#' in anchor_text:
                    hashtags.update(self._extract_hashtags_from_text(anchor_text))

            if hashtags:
                print(f"🏷️ Found {len(hashtags)} hashtags")

        except Exception as e:
            print(f"⚠️ Error extracting hashtags: {e}")

        return hashtags

    def _build_user_api_identifiers(self, username: str) -> List[str]:
        """Build candidate identifiers for pylemon8 user routes."""
        normalized = str(username or '').strip().lstrip('@')
        candidates: List[str] = []

        for candidate in [f"@{normalized}" if normalized else '', normalized]:
            candidate = candidate.strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        return candidates

    def _find_dict_with_any_keys(self, data: Any, keys: Set[str]) -> Optional[Dict[str, Any]]:
        """Recursively find the first dict containing any of the target keys."""
        if isinstance(data, dict):
            if any(key in data for key in keys):
                return data
            for value in data.values():
                found = self._find_dict_with_any_keys(value, keys)
                if found:
                    return found
        elif isinstance(data, list):
            for item in data:
                found = self._find_dict_with_any_keys(item, keys)
                if found:
                    return found
        return None

    def _extract_user_details_from_api_payloads(
        self,
        payloads: List[Dict[str, Any]],
        identifier: str,
    ) -> Dict[str, Any]:
        """Extract user details from Remix JSON payloads without strict key assumptions."""
        normalized = identifier.lstrip('@')
        key_candidates = [
            f"$UserDetailV2+{identifier}",
            f"$UserDetailV2+{normalized}",
            f"$UserDetailV2+{quote(identifier, safe='')}",
            f"$UserDetailV2+{quote(normalized, safe='')}",
        ]

        for payload in payloads:
            for key in key_candidates:
                value = payload.get(key)
                if isinstance(value, dict):
                    return value

        detail_like_keys = {
            'displayName',
            'nickname',
            'followerCount',
            'followingCount',
            'desc',
            'verified',
            'avatarLarger',
            'avatarThumb',
        }

        for payload in payloads:
            detail_dict = self._find_dict_with_any_keys(payload, detail_like_keys)
            if detail_dict:
                return detail_dict

        return {}

    def _fetch_posts_from_profile_page(self, identifier: str) -> List[Dict[str, Any]]:
        """Fetch ALL user posts by paginating the profile ?_data= endpoint.

        The pylemon8 user().get_forced() call appends position=follow_list which
        tells the server to omit posts.  We hit the profile route directly and
        follow cursor-based pagination (same pattern as tag/feed scraping) so
        every post is collected, not just the first page (~12-20 items).
        """
        normalized = identifier.lstrip('@')
        region = getattr(self.lemon8_api, 'region', 'us')

        all_payloads: List[Dict[str, Any]] = []
        current_cursor: Optional[str] = None
        seen_cursors: Set[str] = set()
        working_handle: Optional[str] = None

        for page_num in range(MAX_USER_PROFILE_PAGES):
            # First page: try both @handle and handle formats to find which works.
            # Subsequent pages: reuse the format that worked.
            handles_to_try = [working_handle] if working_handle else [f'@{normalized}', normalized]

            page_payloads: List[Dict[str, Any]] = []
            for handle in handles_to_try:
                base_url = (
                    f'https://www.lemon8-app.com/{handle}'
                    f'?_data=routes%2F%24user_link_name&region={region}&_version=1'
                )
                url = self._add_cursor_to_url(base_url, current_cursor) if current_cursor else base_url
                try:
                    response = self.lemon8_api.session.get(url, timeout=15)
                    text = response.text or ''
                    if not text.strip():
                        continue
                    for line in text.split('\n'):
                        line = line.strip()
                        if not line or not line.startswith('{'):
                            continue
                        try:
                            payload = json.loads(line)
                            if isinstance(payload, dict):
                                page_payloads.append(payload)
                        except json.JSONDecodeError:
                            continue
                    if page_payloads:
                        if working_handle is None:
                            working_handle = handle
                        break
                except Exception:
                    continue

            if not page_payloads:
                break

            all_payloads.extend(page_payloads)

            # Look for pagination signals in the returned payloads.
            next_cursor: Optional[str] = None
            has_more: Optional[bool] = None
            for payload in page_payloads:
                if has_more is None:
                    has_more_val = self._find_key_in_json(
                        payload, ['has_more', 'hasMore', 'hasNext']
                    )
                    has_more = self._normalize_bool_flag(has_more_val)
                if next_cursor is None:
                    cursor_val = self._find_key_in_json(
                        payload, ['next_cursor', 'nextCursor', 'cursor', 'max_cursor']
                    )
                    if cursor_val:
                        next_cursor = str(cursor_val).strip() or None

            print(
                f"📖 User profile page {page_num + 1}"
                + (f" (cursor: {current_cursor})" if current_cursor else "")
                + f": {len(page_payloads)} payload(s)"
            )

            # Stop conditions
            if has_more is False:
                print(f"ℹ️ No more profile pages after page {page_num + 1}")
                break
            if not next_cursor:
                break  # single-page profile or server doesn't paginate this endpoint
            if next_cursor in seen_cursors or next_cursor == current_cursor:
                print(f"ℹ️ Repeated cursor — stopping profile pagination")
                break

            seen_cursors.add(next_cursor)
            current_cursor = next_cursor

            # Rate-limit between pages (page 1 is rate-limited by the caller).
            self.rate_limiter.wait()

        pages_fetched = len(seen_cursors) + 1
        if pages_fetched > 1:
            print(f"✅ Fetched {pages_fetched} pages for @{normalized}")

        return all_payloads

    def _fetch_user_profile_payload_via_api(
        self,
        username: str,
    ) -> Dict[str, Any]:
        """Fetch user profile payload via pylemon8 with robust identifier handling."""
        errors: List[str] = []
        status_codes: List[int] = []

        for identifier in self._build_user_api_identifiers(username):
            try:
                # CRITICAL: Rate limit BEFORE making API call (server tracks your IP!)
                self.rate_limiter.wait()
                
                user_obj = self.lemon8_api.user(identifier)
                response = user_obj.get_forced()
                status_code = response.status_code
                status_codes.append(status_code)
                response_text = response.text or ''

                if not response_text.strip():
                    errors.append(f"{identifier}: empty body (HTTP {status_code})")
                    continue

                payloads: List[Dict[str, Any]] = []
                for line in response_text.split('\n'):
                    line = line.strip()
                    if not line or not line.startswith('{'):
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        payloads.append(payload)

                if not payloads:
                    errors.append(f"{identifier}: no JSON payload lines (HTTP {status_code})")
                    continue
                
                # Record success with rate limiter
                self.rate_limiter.record_success()

                # The pylemon8 call includes position=follow_list which causes the
                # server to omit posts.  Fetch the full profile page separately.
                self.rate_limiter.wait()
                post_payloads = self._fetch_posts_from_profile_page(identifier)
                all_payloads = payloads + [p for p in post_payloads if p not in payloads]

                return {
                    'identifier': identifier,
                    'payloads': all_payloads,
                    'user_details': self._extract_user_details_from_api_payloads(all_payloads, identifier),
                }
            except Exception as e:
                errors.append(f"{identifier}: {type(e).__name__}: {e}")

        if status_codes and all(code in {401, 403, 429} for code in status_codes):
            status_summary = ', '.join(str(code) for code in status_codes)
            # Record error with rate limiter on 403/429
            if any(code in {403, 429} for code in status_codes):
                if 403 in status_codes:
                    self.rate_limiter.record_error()
                if 429 in status_codes:
                    self.rate_limiter.record_rate_limit()
            raise PermissionError(
                f"pylemon8 user endpoint appears blocked/throttled for this session (HTTP {status_summary})"
            )

        error_details = '; '.join(errors) if errors else 'unknown API failure'
        raise ValueError(
            f"pylemon8 user endpoint did not return a usable JSON payload for '{username}' ({error_details})"
        )
    
    def scrape_user_profile(
        self,
        username: str,
        use_api: bool = True,
        include_profile_images: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Scrape a user's profile page using pylemon8 API (preferred) or fallback to web scraping
        
        Args:
            username: Username without @ symbol
            use_api: Whether to use pylemon8 API first (default: True)
            
        Returns:
            Dict with media URLs, user info, and metadata
        """
        username = username.lstrip('@')
        if not re.match(r'^[a-zA-Z0-9_.][a-zA-Z0-9_.-]{0,59}$', username):
            return {'error': f"Invalid username format: {username!r}"}

        print(f"🔍 Scraping user profile: @{username}")
        
        # Try pylemon8 API first if available and requested
        if use_api and self.lemon8_api and PYLEMON8_AVAILABLE and not self.api_user_endpoint_blocked:
            try:
                print("🚀 Using pylemon8 API for enhanced data retrieval")
                return self._scrape_user_with_api(
                    username,
                    include_profile_images=include_profile_images,
                )
            except PermissionError as e:
                self.api_user_endpoint_blocked = True
                print(f"⚠️ API access blocked/throttled: {e}")
                print("ℹ️ Disabling pylemon8 user API for this run and switching to web scraping.")
            except Exception as e:
                print(f"⚠️ API method failed, falling back to web scraping: {e}")
        elif use_api and self.api_user_endpoint_blocked:
            print("ℹ️ Skipping pylemon8 user API (blocked earlier in this session).")
        
        # Fallback to traditional web scraping
        print("🌐 Using traditional web scraping method")
        return self._scrape_user_with_web(
            username,
            include_profile_images=include_profile_images,
        )
    
    def _scrape_user_with_api(
        self,
        username: str,
        include_profile_images: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Scrape user profile using pylemon8 API
        """
        try:
            resolved_include_profile_images = self._resolve_include_profile_images(
                include_profile_images,
                INCLUDE_PROFILE_IMAGES_IN_USER_SCRAPES,
            )
            api_payload = self._fetch_user_profile_payload_via_api(username)
            payloads: List[Dict[str, Any]] = api_payload.get('payloads', [])
            user_details = api_payload.get('user_details', {})
            identifier_used = api_payload.get('identifier', username)
            
            # Extract user_id from user_details
            user_id = self._extract_user_id_from_author(user_details)
            
            # Extract basic user information
            user_info = {
                'username': username,
                'user_id': user_id,
                'display_name': user_details.get('displayName', ''),
                'follower_count': user_details.get('followerCount', 0),
                'following_count': user_details.get('followingCount', 0),
                'bio': user_details.get('desc', ''),
                'verified': user_details.get('verified', False),
                'api_identifier_used': identifier_used,
                'api_method': True
            }

            # Try to extract structured data from profile page
            media_items: List[Dict[str, Any]] = []
            related_users = set()
            tag_ids = set()
            hashtags = set()
            
            # Parse JSON data from profile payloads
            for data in payloads:
                hashtags.update(self._extract_hashtags_from_json(data))
                for items in self._find_item_lists_in_json(data):
                    media_items.extend(
                        self._extract_media_items_from_pylemon8_items(
                            items,
                            include_profile_images=resolved_include_profile_images,
                        )
                    )
                    related_users.update(self._extract_users_from_pylemon8_items(items))
            
            media_items = self._deduplicate_media_items(media_items)
            media_urls = [item['url'] for item in media_items]
            related_users.discard(username.lower())
            
            result = {
                'username': username,
                'user_id': user_id,
                'user_info': user_info,
                'media_items': media_items,
                'media_urls': media_urls,
                'related_users': list(related_users),
                'tag_ids': list(tag_ids),
                'hashtags': list(hashtags),
                'scrape_timestamp': time.time(),
                'total_media': len(media_urls),
                'method': 'pylemon8_api'
            }
            
            print(
                f"✅ Profile scraped via API: {len(media_urls)} media, "
                f"{len(related_users)} related users, {len(hashtags)} hashtags"
            )
            if user_id:
                print(f"🆔 User ID: {user_id}")

            if not media_items and not user_id and not user_details:
                # Only fall back when the API returned nothing at all (no user data, no posts).
                # If we have valid user data, the account exists but has no posts — trust the API.
                raise ValueError(
                    "API returned 0 media items with no user details — triggering web-scraping fallback"
                )

            return result

        except PermissionError:
            raise
        except Exception as e:
            print(f"❌ API scraping failed for user {username}: {e}")
            raise e
    
    def _scrape_user_with_web(
        self,
        username: str,
        include_profile_images: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Scrape user profile using traditional web scraping (fallback method)
        """
        url = get_user_url(username)
        print(f"🌐 URL: {url}")
        
        try:
            resolved_include_profile_images = self._resolve_include_profile_images(
                include_profile_images,
                INCLUDE_PROFILE_IMAGES_IN_USER_SCRAPES,
            )
            response = self._make_request_with_retry(url, referer=get_user_url(username))
            
            html_content = response.text
            
            # Try to extract user_id from JSON in HTML
            user_id = None
            try:
                soup = BeautifulSoup(html_content, 'html.parser')
                script_tags = soup.find_all('script', {'type': 'application/json'})
                for script in script_tags:
                    try:
                        json_data = json.loads(script.string or '{}')
                        # Look for user details in JSON
                        user_details = self._extract_user_details_from_api_payloads([json_data], username)
                        if user_details:
                            user_id = self._extract_user_id_from_author(user_details)
                            if user_id:
                                break
                    except (json.JSONDecodeError, AttributeError):
                        continue
            except Exception:
                pass
            
            media_items = self._extract_media_items_from_html(
                html_content,
                include_profile_images=resolved_include_profile_images,
            )
            if resolved_include_profile_images and PROFILE_PHOTO_DOWNLOAD_ENABLED:
                for avatar_url in self._extract_profile_photo_urls_from_html(html_content):
                    media_item = self._build_media_item(
                        avatar_url,
                        username=username,
                        is_profile_photo=True,
                    )
                    if media_item:
                        media_items.append(media_item)

            media_items = self._deduplicate_media_items(media_items)
            media_urls = [item['url'] for item in media_items]
            if not media_urls:
                media_urls = self._extract_media_urls(html_content)
                media_items = [
                    self._build_media_item(url, username=username)
                    for url in media_urls
                ]
                media_items = [item for item in media_items if item]
            
            # Extract related users/handles
            related_users = self._extract_user_handles(html_content)
            related_users.discard(username.lower())  # Remove the current user
            
            # Extract tag IDs
            tag_ids = self._extract_tag_ids(html_content)
            hashtags = self._extract_hashtags(html_content)
            
            result = {
                'username': username,
                'user_id': user_id,
                'url': url,
                'media_items': media_items,
                'media_urls': media_urls,
                'related_users': list(related_users),
                'tag_ids': list(tag_ids),
                'hashtags': list(hashtags),
                'scrape_timestamp': time.time(),
                'total_media': len(media_urls)
            }
            
            print(
                f"✅ Profile scraped: {len(media_urls)} media, {len(related_users)} related users, "
                f"{len(hashtags)} hashtags, {len(tag_ids)} topic IDs"
            )
            if user_id:
                print(f"🆔 User ID: {user_id}")
            
            # Rate limiting
            _interruptible_sleep(MIN_DELAY)
            
            return result
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Network error scraping user {username}: {e}")
            return {'username': username, 'error': str(e), 'media_urls': []}
        except Exception as e:
            print(f"❌ Error scraping user {username}: {e}")
            return {'username': username, 'error': str(e), 'media_urls': []}
    
    def scrape_for_you_feed(
        self,
        pages: int = 1,
        use_api: bool = True,
        include_profile_images: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Scrape the For You feed using pylemon8 API (preferred) or fallback to web scraping
        
        Args:
            pages: Number of pages to scrape (limited pagination available)
            use_api: Whether to use pylemon8 API first (default: True)
            
        Returns:
            Dict with media URLs and metadata
        """
        print(f"🔍 Scraping For You feed ({pages} pages)")
        
        # Try pylemon8 API first if available and requested
        if use_api and self.lemon8_api and PYLEMON8_AVAILABLE:
            try:
                print("🚀 Using pylemon8 API for enhanced feed data")
                return self._scrape_feed_with_api(
                    'foryou',
                    pages,
                    include_profile_images=include_profile_images,
                )
            except Exception as e:
                print(f"⚠️ API method failed, falling back to web scraping: {e}")
        
        # Fallback to traditional web scraping
        print("🌐 Using traditional web scraping method")
        return self._scrape_feed_with_web(
            pages,
            include_profile_images=include_profile_images,
        )
    
    def _scrape_feed_with_api(
        self,
        category: str = 'foryou',
        pages: int = 1,
        include_profile_images: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Scrape feed using pylemon8 API
        """
        try:
            resolved_include_profile_images = self._resolve_include_profile_images(
                include_profile_images,
                INCLUDE_PROFILE_IMAGES_IN_FEED,
            )
            
            # CRITICAL: Rate limit BEFORE making API call
            self.rate_limiter.wait()
            
            feed_obj = self.lemon8_api.feed(category)
            
            all_media_items: List[Dict[str, Any]] = []
            all_users = set()
            all_tag_ids = set()
            
            for page in range(pages):
                print(f"📖 API: Fetching page {page + 1}/{pages}")
                
                # Rate limit before each page fetch
                if page > 0:
                    self.rate_limiter.wait()
                
                # Get feed items
                items = feed_obj.get_items()
                
                # Record success
                self.rate_limiter.record_success()
                
                media_items = self._extract_media_items_from_pylemon8_items(
                    items,
                    include_profile_images=resolved_include_profile_images,
                )
                all_media_items.extend(media_items)
                
                # Extract users
                users = self._extract_users_from_pylemon8_items(items)
                all_users.update(users)
                
                print(f"📄 API Page {page + 1}: {len(media_items)} media found")
            
            unique_media_items = self._deduplicate_media_items(all_media_items)
            unique_media_urls = [item['url'] for item in unique_media_items]
            
            result = {
                'feed_type': category,
                'pages_scraped': pages,
                'media_items': unique_media_items,
                'media_urls': unique_media_urls,
                'discovered_users': list(all_users),
                'discovered_tags': list(all_tag_ids),
                'scrape_timestamp': time.time(),
                'total_media': len(unique_media_urls),
                'method': 'pylemon8_api'
            }
            
            print(f"✅ Feed scraped via API: {len(unique_media_urls)} unique media, {len(all_users)} users")
            
            return result
            
        except Exception as e:
            print(f"❌ API feed scraping failed: {e}")
            raise e
    
    def _scrape_feed_with_web(
        self,
        pages: int = 1,
        include_profile_images: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Scrape feed using traditional web scraping (fallback method)
        """
        # Use mobile user agent to access feed content
        print(f"🌐 URL: {FEED_URL}")
        
        all_media_items: List[Dict[str, Any]] = []
        all_users = set()
        all_tag_ids = set()
        
        current_cursor = "0"
        
        try:
            resolved_include_profile_images = self._resolve_include_profile_images(
                include_profile_images,
                INCLUDE_PROFILE_IMAGES_IN_FEED,
            )
            for page in range(pages):
                print(f"📖 Scraping page {page + 1}/{pages} (cursor: {current_cursor})")
                
                # Try adding cursor to URL if it's not the first page
                url = FEED_URL
                if page > 0 and current_cursor:
                    url = f"{FEED_URL}?cursor={current_cursor}"
                
                response = self._make_request_with_retry(url, referer=FEED_URL)
                
                html_content = response.text
                
                media_items = self._extract_media_items_from_feed_cards(
                    html_content,
                    include_profile_images=resolved_include_profile_images,
                )
                if not media_items:
                    media_items = self._extract_media_items_from_html(
                        html_content,
                        include_profile_images=resolved_include_profile_images,
                    )
                if not any(item.get('username') for item in media_items):
                    card_media_items = self._extract_media_items_from_feed_cards(
                        html_content,
                        include_profile_images=resolved_include_profile_images,
                    )
                    if card_media_items:
                        media_items_by_url = {
                            item['url']: item for item in media_items
                        }
                        for card_item in card_media_items:
                            existing_item = media_items_by_url.get(card_item['url'])
                            if existing_item:
                                if not existing_item.get('username') and card_item.get('username'):
                                    existing_item['username'] = card_item['username']
                            else:
                                media_items.append(card_item)

                if not any(item.get('username') for item in media_items):
                    media_items = self._extract_media_items_from_html(
                    html_content,
                    include_profile_images=resolved_include_profile_images,
                    )
                    dom_media_items = self._extract_media_items_from_dom(html_content)
                    if dom_media_items:
                        media_items_by_url = {
                            item['url']: item for item in media_items
                        }
                        for dom_item in dom_media_items:
                            existing_item = media_items_by_url.get(dom_item['url'])
                            if existing_item:
                                if not existing_item.get('username') and dom_item.get('username'):
                                    existing_item['username'] = dom_item['username']
                            else:
                                media_items.append(dom_item)

                if media_items:
                    all_media_items.extend(media_items)
                    media_urls = [item['url'] for item in media_items]
                else:
                    media_urls = self._extract_media_urls(html_content)
                    for media_url in media_urls:
                        media_item = self._build_media_item(media_url)
                        if media_item:
                            all_media_items.append(media_item)
                
                # Extract users
                users = self._extract_user_handles(html_content)
                all_users.update(users)
                
                # Extract tag IDs
                tag_ids = self._extract_tag_ids(html_content)
                all_tag_ids.update(tag_ids)
                
                # Try to extract next cursor from JSON
                try:
                    soup = BeautifulSoup(html_content, 'html.parser')
                    script_tags = soup.find_all('script', {'type': 'application/json'})
                    found_cursor = False
                    for script in script_tags:
                        try:
                            json_data = json.loads(script.string or '{}')
                            # Search for cursor/max_cursor in JSON
                            cursor = self._find_key_in_json(json_data, ['cursor', 'max_cursor', 'next_cursor'])
                            if cursor:
                                current_cursor = str(cursor)
                                found_cursor = True
                                break
                        except (json.JSONDecodeError, AttributeError):
                            continue
                    
                    if not found_cursor:
                        # If no cursor found, maybe use offset as fallback if supported
                        current_cursor = str(len(all_media_items))
                except (KeyError, TypeError, AttributeError, ValueError, IndexError) as e:
                    print(f"⚠️ Cursor extraction failed, using offset fallback: {e}")
                    current_cursor = str(len(all_media_items))
                
                print(f"📄 Page {page + 1}: {len(media_urls)} media found")
                
                # Rate limiting between pages
                if page < pages - 1:
                    _interruptible_sleep(MAX_DELAY)
            
            # Remove duplicates while preserving order
            unique_media_items = self._deduplicate_media_items(all_media_items)
            unique_media_urls = [item['url'] for item in unique_media_items]
            
            result = {
                'feed_type': 'foryou',
                'pages_scraped': pages,
                'media_items': unique_media_items,
                'media_urls': unique_media_urls,
                'discovered_users': list(all_users),
                'discovered_tags': list(all_tag_ids),
                'scrape_timestamp': time.time(),
                'total_media': len(unique_media_urls),
                'method': 'web_scraping'
            }
            
            print(f"✅ Feed scraped: {len(unique_media_urls)} unique media, {len(all_users)} users, {len(all_tag_ids)} tags")
            
            return result
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Network error scraping feed: {e}")
            return {'feed_type': 'foryou', 'error': str(e), 'media_urls': [], 'method': 'web_scraping'}
        except Exception as e:
            print(f"❌ Error scraping feed: {e}")
            return {'feed_type': 'foryou', 'error': str(e), 'media_urls': [], 'method': 'web_scraping'}
    
    def _find_key_in_json(self, data: Any, keys: List[str]) -> Any:
        """Recursively find a key in JSON data"""
        if isinstance(data, dict):
            for key in keys:
                if key in data:
                    return data[key]
            for value in data.values():
                result = self._find_key_in_json(value, keys)
                if result is not None:
                    return result
        elif isinstance(data, list):
            for item in data:
                result = self._find_key_in_json(item, keys)
                if result is not None:
                    return result
        return None

    def _normalize_bool_flag(self, value: Any) -> Optional[bool]:
        """Normalize mixed JSON truthy/falsey markers into a strict bool when possible."""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {'true', '1', 'yes', 'y'}:
                return True
            if normalized in {'false', '0', 'no', 'n'}:
                return False
        return None

    def _add_cursor_to_url(self, base_url: str, cursor: str) -> str:
        """Append/replace cursor query parameter while preserving existing query args."""
        parsed = urlparse(base_url)
        query_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query_params['cursor'] = str(cursor)
        updated_query = urlencode(query_params)
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            updated_query,
            parsed.fragment,
        ))

    def _is_numeric_topic_id(self, value: str) -> bool:
        """Return True when a topic identifier is purely numeric."""
        return bool(str(value).strip().isdigit())

    def _build_discover_url(self, query: str) -> str:
        """Build a discover URL from a free-form topic query."""
        normalized_query = str(query or '').strip().lstrip('#')
        normalized_query = re.sub(r'[_\-]+', ' ', normalized_query).strip()
        encoded_query = quote(normalized_query or str(query).strip(), safe='')
        return f"{LEMON8_BASE_URL}/discover/{encoded_query}?region=sg"

    def _extract_post_urls_from_html(self, html_content: str) -> List[str]:
        """Extract canonical Lemon8 post URLs from a page."""
        post_urls: List[str] = []

        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            for link in soup.find_all('a', href=True):
                href = (link.get('href') or '').strip()
                if not href:
                    continue

                absolute_url = urljoin(LEMON8_BASE_URL, href)
                absolute_url = absolute_url.split('#', 1)[0]

                if not absolute_url.startswith(LEMON8_BASE_URL):
                    continue

                if re.search(r'/(?:@)?[a-zA-Z0-9_.-]+/\d+(?:\?|$)', absolute_url):
                    post_urls.append(absolute_url)
        except Exception as e:
            print(f"⚠️ Error extracting post URLs from page: {e}")

        unique_post_urls: List[str] = []
        seen = set()
        for post_url in post_urls:
            if post_url not in seen:
                unique_post_urls.append(post_url)
                seen.add(post_url)

        return unique_post_urls

    def _extract_username_from_post_url(self, post_url: str) -> Optional[str]:
        """Extract username/handle from a Lemon8 post URL."""
        match = re.search(r'lemon8-app\.com/(?:@)?([a-zA-Z0-9_.-]+)/\d+', post_url)
        if not match:
            return None
        return self._normalize_username(match.group(1))

    def _is_relevant_post_media_url(self, media_url: str) -> bool:
        """Filter noisy media URLs (avatars/share cards) from post/discover fallbacks."""
        if not media_url or not self._is_valid_media_url(media_url):
            return False

        media_url_lower = media_url.lower()
        if self._is_profile_photo_url(media_url):
            return False

        noisy_tokens = [
            'share_card',
            'source=share_card',
            'source=feed_user',
            'user-avatar',
            '/avatar',
            'avatar-',
        ]
        if any(token in media_url_lower for token in noisy_tokens):
            return False

        if self._is_small_image(media_url):
            return False

        return True

    def _scrape_post_pages_for_media(self, post_urls: List[str], max_posts: int = 12) -> Dict[str, Any]:
        """Scrape individual post pages to recover media when list pages are sparse."""
        recovered_media_items: List[Dict[str, Any]] = []
        recovered_users = set()
        recovered_tags = set()
        pages_scraped = 0

        for index, post_url in enumerate(post_urls[:max_posts], 1):
            try:
                response = self._make_request_with_retry(post_url, referer=post_url)
                html_content = response.text
                pages_scraped += 1

                username_from_url = self._extract_username_from_post_url(post_url)
                if username_from_url:
                    recovered_users.add(username_from_url)

                post_media_items = self._extract_media_items_from_html(
                    html_content,
                    include_profile_images=False,
                )

                if not post_media_items:
                    post_media_urls = self._extract_media_urls(html_content)
                    post_media_urls = [
                        media_url
                        for media_url in post_media_urls
                        if self._is_relevant_post_media_url(media_url)
                    ]
                    post_media_items = [
                        self._build_media_item(media_url, username=username_from_url)
                        for media_url in post_media_urls
                    ]
                    post_media_items = [item for item in post_media_items if item]

                for media_item in post_media_items:
                    if username_from_url and not media_item.get('username'):
                        media_item['username'] = username_from_url

                recovered_media_items.extend(post_media_items)
                recovered_users.update(self._extract_user_handles(html_content))
                recovered_tags.update(self._extract_tag_ids(html_content))

                if index < min(len(post_urls), max_posts):
                    _interruptible_sleep(MIN_DELAY)
            except requests.exceptions.RequestException:
                continue
            except (KeyError, TypeError, AttributeError, ValueError) as e:
                print(f"⚠️ Post processing error, skipping: {e}")
                continue

        unique_media_items = self._deduplicate_media_items(recovered_media_items)

        return {
            'media_items': unique_media_items,
            'media_urls': [item['url'] for item in unique_media_items],
            'related_users': list(recovered_users),
            'related_tags': list(recovered_tags),
            'pages_scraped': pages_scraped,
            'posts_considered': min(len(post_urls), max_posts),
        }

    def _scrape_discover_keyword(self, query: str) -> Dict[str, Any]:
        """Scrape discover results for keyword-based topic requests."""
        discover_url = self._build_discover_url(query)
        print(f"🔎 Discover fallback URL: {discover_url}")

        response = self._make_request_with_retry(discover_url, referer=discover_url)
        html_content = response.text

        media_items = self._extract_media_items_from_html(
            html_content,
            include_profile_images=False,
        )
        if not media_items:
            media_items = self._extract_media_items_from_feed_cards(
                html_content,
                include_profile_images=False,
            )
        if media_items and not any(item.get('username') for item in media_items):
            dom_media_items = self._extract_media_items_from_dom(html_content)
            if dom_media_items:
                media_items_by_url = {
                    item['url']: item for item in media_items
                }
                for dom_item in dom_media_items:
                    existing_item = media_items_by_url.get(dom_item['url'])
                    if existing_item:
                        if not existing_item.get('username') and dom_item.get('username'):
                            existing_item['username'] = dom_item['username']
                    else:
                        media_items.append(dom_item)

        related_users = set(self._extract_user_handles(html_content))
        related_tags = set(self._extract_tag_ids(html_content))
        post_urls = self._extract_post_urls_from_html(html_content)

        post_page_results = {
            'media_items': [],
            'media_urls': [],
            'related_users': [],
            'related_tags': [],
            'pages_scraped': 0,
            'posts_considered': 0,
        }

        if post_urls:
            post_page_results = self._scrape_post_pages_for_media(post_urls)
            media_items.extend(post_page_results.get('media_items', []))
            related_users.update(post_page_results.get('related_users', []))
            related_tags.update(post_page_results.get('related_tags', []))

        if not media_items and not post_urls:
            media_urls = self._extract_media_urls(html_content)
            media_urls = [
                media_url
                for media_url in media_urls
                if self._is_relevant_post_media_url(media_url)
            ]
            media_items = [
                self._build_media_item(media_url)
                for media_url in media_urls
            ]
            media_items = [item for item in media_items if item]

        unique_media_items = self._deduplicate_media_items(media_items)

        return {
            'query': query,
            'url': discover_url,
            'media_items': unique_media_items,
            'media_urls': [item['url'] for item in unique_media_items],
            'related_users': list(related_users),
            'related_tags': list(related_tags),
            'post_urls_found': len(post_urls),
            'post_pages_scraped': post_page_results.get('pages_scraped', 0),
        }
    
    def scrape_tag_topic(self, tag_id: str, pages: int = 10) -> Dict[str, Any]:
        """
        Scrape a specific tag/topic page
        
        Args:
            tag_id: Tag/topic ID (numeric)
            pages: Number of pages to scrape
            
        Returns:
            Dict with media URLs and metadata
        """
        tag_id = str(tag_id)
        pages = max(1, int(pages))
        base_url = get_tag_url(tag_id)
        
        print(f"🔍 Scraping tag/topic: {tag_id} ({pages} pages)")
        print(f"🌐 URL: {base_url}")

        all_media_items: List[Dict[str, Any]] = []
        all_related_users = set()
        all_related_tag_ids = set()
        pages_scraped = 0
        current_cursor: Optional[str] = None
        seen_cursors = set()
        topic_no_content_shell = False
        fallback_used = False
        fallback_url: Optional[str] = None
        fallback_post_pages_scraped = 0
        
        try:
            for page in range(pages):
                page_url = base_url
                if current_cursor:
                    page_url = self._add_cursor_to_url(base_url, current_cursor)
                    print(f"📖 Scraping tag page {page + 1}/{pages} (cursor: {current_cursor})")
                else:
                    print(f"📖 Scraping tag page {page + 1}/{pages} (initial page)")

                response = self._make_request_with_retry(page_url, referer=page_url)

                html_content = response.text
                if 'No content' in html_content:
                    topic_no_content_shell = True

                page_media_items = self._extract_media_items_from_html(
                    html_content,
                    include_profile_images=False,
                )
                if not page_media_items:
                    page_media_items = self._extract_media_items_from_feed_cards(
                        html_content,
                        include_profile_images=False,
                    )
                if page_media_items and not any(item.get('username') for item in page_media_items):
                    dom_media_items = self._extract_media_items_from_dom(html_content)
                    if dom_media_items:
                        media_items_by_url = {
                            item['url']: item for item in page_media_items
                        }
                        for dom_item in dom_media_items:
                            existing_item = media_items_by_url.get(dom_item['url'])
                            if existing_item:
                                if not existing_item.get('username') and dom_item.get('username'):
                                    existing_item['username'] = dom_item['username']
                            else:
                                page_media_items.append(dom_item)

                page_media_items = self._deduplicate_media_items(page_media_items)
                if page_media_items:
                    page_media_urls = [item['url'] for item in page_media_items]
                else:
                    page_media_urls = self._extract_media_urls(html_content)
                    page_media_items = [
                        self._build_media_item(media_url)
                        for media_url in page_media_urls
                    ]
                    page_media_items = [item for item in page_media_items if item]

                all_media_items.extend(page_media_items)
                pages_scraped += 1

                # Extract related users for this page
                page_related_users = self._extract_user_handles(html_content)
                page_related_users.update(
                    item['username']
                    for item in page_media_items
                    if isinstance(item, dict) and item.get('username')
                )
                all_related_users.update(page_related_users)

                # Extract related tags for this page
                all_related_tag_ids.update(self._extract_tag_ids(html_content))

                print(f"📄 Tag page {page + 1}: {len(page_media_urls)} media found")

                # No pagination work needed on the last requested page
                if page >= pages - 1:
                    continue

                next_cursor = None
                has_more_flag = None

                try:
                    soup = BeautifulSoup(html_content, 'html.parser')
                    script_tags = soup.find_all('script', {'type': 'application/json'})

                    for script in script_tags:
                        try:
                            json_data = json.loads(script.string or '{}')

                            if has_more_flag is None:
                                has_more_value = self._find_key_in_json(json_data, ['has_more', 'hasMore', 'hasNext'])
                                has_more_flag = self._normalize_bool_flag(has_more_value)

                            cursor_candidate = self._find_key_in_json(
                                json_data,
                                ['next_cursor', 'nextCursor', 'cursor', 'max_cursor'],
                            )
                            if cursor_candidate is not None:
                                candidate = str(cursor_candidate).strip()
                                if candidate:
                                    next_cursor = candidate
                                    break
                        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
                            continue
                except (KeyError, TypeError, AttributeError, ValueError, IndexError) as e:
                    print(f"⚠️ Next-page cursor parse failed: {e}")
                    next_cursor = None

                if has_more_flag is False:
                    print(f"ℹ️ Tag topic indicates no more pages after page {page + 1}; stopping early.")
                    break

                if not next_cursor:
                    print(f"ℹ️ No next-page cursor found after page {page + 1}; stopping early.")
                    break

                if current_cursor and next_cursor == current_cursor:
                    print(f"ℹ️ Repeated cursor detected at page {page + 1}; stopping early.")
                    break

                if next_cursor in seen_cursors:
                    print(f"ℹ️ Already-seen cursor detected at page {page + 1}; stopping early.")
                    break

                seen_cursors.add(next_cursor)
                current_cursor = next_cursor

                # Rate limiting between pages
                _interruptible_sleep(MAX_DELAY)

            unique_media_items = self._deduplicate_media_items(all_media_items)
            unique_media_urls = [item['url'] for item in unique_media_items]
            all_related_tag_ids.discard(tag_id)

            if not unique_media_urls and not self._is_numeric_topic_id(tag_id):
                print(
                    "ℹ️ No media found on the public topic page. "
                    "Trying keyword discover fallback..."
                )
                try:
                    fallback_result = self._scrape_discover_keyword(tag_id)
                    fallback_media_items = fallback_result.get('media_items', [])
                    if fallback_media_items:
                        fallback_used = True
                        fallback_url = fallback_result.get('url')
                        fallback_post_pages_scraped = int(fallback_result.get('post_pages_scraped', 0) or 0)
                        unique_media_items = fallback_media_items
                        unique_media_urls = [item['url'] for item in unique_media_items]
                        all_related_users.update(fallback_result.get('related_users', []))
                        all_related_tag_ids.update(fallback_result.get('related_tags', []))
                        all_related_tag_ids.discard(tag_id)
                        print(
                            f"✅ Discover fallback recovered {len(unique_media_urls)} media items "
                            f"from keyword '{tag_id}'."
                        )
                except requests.exceptions.RequestException as e:
                    print(f"⚠️ Discover fallback network error for '{tag_id}': {e}")
                except Exception as e:
                    print(f"⚠️ Discover fallback error for '{tag_id}': {e}")

            if not unique_media_urls and topic_no_content_shell:
                print(
                    "ℹ️ Lemon8 web returned 'No content' for this topic page. "
                    "Try another keyword/topic or use user/feed scraping."
                )

            result = {
                'tag_id': tag_id,
                'url': base_url,
                'pages_requested': pages,
                'pages_scraped': pages_scraped,
                'media_items': unique_media_items,
                'media_urls': unique_media_urls,
                'related_users': list(all_related_users),
                'related_tags': list(all_related_tag_ids),
                'topic_no_content_shell': topic_no_content_shell,
                'fallback_used': fallback_used,
                'fallback_url': fallback_url,
                'fallback_post_pages_scraped': fallback_post_pages_scraped,
                'scrape_timestamp': time.time(),
                'total_media': len(unique_media_urls)
            }
            
            print(
                f"✅ Tag scraped: {len(unique_media_urls)} media, "
                f"{len(all_related_users)} users, {len(all_related_tag_ids)} related tags "
                f"across {pages_scraped}/{pages} pages"
            )
            
            return result
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Network error scraping tag {tag_id}: {e}")
            return {'tag_id': tag_id, 'error': str(e), 'media_urls': []}
        except Exception as e:
            print(f"❌ Error scraping tag {tag_id}: {e}")
            return {'tag_id': tag_id, 'error': str(e), 'media_urls': []}
