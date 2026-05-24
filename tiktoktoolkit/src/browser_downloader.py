"""Browser automation downloader for bypassing TikTok anti-bot protection.

This module uses Playwright to render TikTok pages in a real browser,
which bypasses anti-bot protection by:
1. Executing JavaScript challenges automatically
2. Providing realistic browser fingerprints
3. Using actual browser cookies and session state
4. Mimicking human-like behavior patterns

This is slower than gallery-dl but has much higher success rate for
private accounts and accounts protected by anti-bot measures.
"""

import logging
import re
import time
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone

from . import resilience
from .models import DownloadResult
from .errors import ProviderError

logger = logging.getLogger('uttk.browser_downloader')

# Check if Playwright is available
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logger.warning("Playwright not installed. Browser automation fallback unavailable.")


# ── Shared Playwright utilities (used by BrowserDownloader and Spider) ────────

_CHROMIUM_ARGS = [
    '--disable-blink-features=AutomationControlled',
    '--disable-dev-shm-usage',
    '--no-sandbox',
]

_CONTEXT_OPTIONS: Dict[str, Any] = {
    'viewport': {'width': 1920, 'height': 1080},
    'user_agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    ),
    'locale': 'en-US',
    'timezone_id': 'America/New_York',
    'extra_http_headers': {
        'Accept-Language': 'en-US,en;q=0.9',
        'sec-ch-ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
    },
}

_STEALTH_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    "Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });"
    "Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });"
)


def _parse_tiktok_count(text: str) -> int:
    """Parse TikTok formatted count: '1.2M' → 1200000, '500K' → 500000, '45' → 45."""
    text = text.strip().replace(',', '').replace(' ', '')
    for suffix, mult in [('B', 1_000_000_000), ('M', 1_000_000), ('K', 1_000)]:
        if text.upper().endswith(suffix):
            try:
                return int(float(text[:-1]) * mult)
            except ValueError:
                return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _parse_netscape_cookies(cookies_file: Path) -> List[Dict[str, Any]]:
    """Parse Netscape-format cookies file into Playwright-compatible dicts."""
    cookies: List[Dict[str, Any]] = []
    try:
        with cookies_file.open('r', encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) < 7:
                    continue
                domain = parts[0].strip()
                if not domain:
                    continue
                if domain.startswith(('http://', 'https://')):
                    domain = domain.split('://', 1)[1]
                expires = int(parts[4]) if parts[4] not in ('0', '') else -1
                cookie: Dict[str, Any] = {
                    'name': parts[5],
                    'value': parts[6],
                    'domain': domain,
                    'path': parts[2] or '/',
                    'secure': parts[3].lower() == 'true',
                }
                if expires > 0:
                    cookie['expires'] = expires
                cookies.append(cookie)
    except Exception as exc:
        logger.error(f"Failed to parse cookies file {cookies_file}: {exc}")
    return cookies


def _make_browser_context(playwright, cookies_file: Optional[Path], headless: bool = True):
    """Launch Chromium and return (browser, context) ready for TikTok with cookies loaded."""
    browser = playwright.chromium.launch(headless=headless, args=_CHROMIUM_ARGS)
    context = browser.new_context(**_CONTEXT_OPTIONS)
    context.add_init_script(_STEALTH_SCRIPT)
    if cookies_file and cookies_file.exists():
        parsed = _parse_netscape_cookies(cookies_file)
        if parsed:
            context.add_cookies(parsed)
    return browser, context


def fetch_profile_stats(
    username: str,
    cookies_file: Optional[Path] = None,
    headless: bool = True,
    timeout_ms: int = 30_000,
) -> Dict[str, Any]:
    """Fetch TikTok profile counts (following, followers, video) via Playwright.

    Returns dict with keys: user_id, followers_count, following_count, video_count.
    On any failure returns zeros — never raises.
    """
    result: Dict[str, Any] = {
        'user_id': None,
        'followers_count': 0,
        'following_count': 0,
        'video_count': 0,
    }
    if not PLAYWRIGHT_AVAILABLE:
        return result

    profile_url = f"https://www.tiktok.com/@{username}"
    try:
        with sync_playwright() as p:
            browser, context = _make_browser_context(p, cookies_file, headless)
            page = context.new_page()
            try:
                page.goto(profile_url, timeout=timeout_ms, wait_until='domcontentloaded')
                try:
                    page.wait_for_load_state('networkidle', timeout=10_000)
                except Exception:
                    pass

                def _get(selector: str) -> int:
                    try:
                        el = page.query_selector(selector)
                        return _parse_tiktok_count(el.inner_text()) if el else 0
                    except Exception:
                        return 0

                result['following_count'] = _get('[data-e2e="following-count"]')
                result['followers_count'] = _get('[data-e2e="followers-count"]')
                result['video_count'] = _get('[data-e2e="video-count"]')

                # Best-effort user_id extraction from embedded page JSON
                try:
                    uid = page.evaluate("""() => {
                        for (const s of document.querySelectorAll('script')) {
                            const m = s.textContent.match(/"uid":"(\\d+)"/);
                            if (m) return m[1];
                        }
                        return null;
                    }""")
                    result['user_id'] = uid
                except Exception:
                    pass
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        logger.warning(f"fetch_profile_stats failed for @{username}: {exc}")

    return result


def fetch_following_list(
    username: str,
    cookies_file: Optional[Path] = None,
    headless: bool = True,
    timeout_ms: int = 30_000,
    max_scrolls: int = 12,
) -> List[str]:
    """Fetch TikTok following list for *username* via Playwright.

    Opens the profile, clicks the Following count to trigger the modal,
    scrolls to load all entries (caller guarantees count ≤ threshold),
    then returns a deduplicated list of discovered usernames.

    On any failure returns [] — never raises.
    """
    if not PLAYWRIGHT_AVAILABLE:
        return []

    profile_url = f"https://www.tiktok.com/@{username}"
    discovered: List[str] = []

    try:
        with sync_playwright() as p:
            browser, context = _make_browser_context(p, cookies_file, headless)
            page = context.new_page()
            try:
                page.goto(profile_url, timeout=timeout_ms, wait_until='domcontentloaded')
                try:
                    page.wait_for_load_state('networkidle', timeout=10_000)
                except Exception:
                    pass

                # Click the "Following" count to open the modal
                for selector in (
                    '[data-e2e="following-count"]',
                    'strong[title*="Following"]',
                    'a[href$="/following"] strong',
                ):
                    el = page.query_selector(selector)
                    if el:
                        try:
                            el.click()
                            time.sleep(1.5)
                            break
                        except Exception:
                            pass

                # Wait for the following modal to appear before scrolling
                time.sleep(1.0)

                # Scroll the modal's inner list container, not the background page.
                # TikTok renders the following list inside an overflow-y:auto div;
                # window.scrollBy has no effect on it.
                _SCROLL_JS = """() => {
                    const sel = [
                        '[data-e2e="follow-list-container"]',
                        '[data-e2e="following-list"]',
                        'div[class*="DivUserList"]',
                        'div[class*="DivFollowList"]',
                        'div[role="dialog"] [class*="List"]',
                        'div[role="dialog"]',
                    ];
                    for (const s of sel) {
                        const el = document.querySelector(s);
                        if (el && el.scrollHeight > el.clientHeight) {
                            el.scrollBy(0, 600);
                            return s;
                        }
                    }
                    window.scrollBy(0, 600);
                    return 'window';
                }"""

                for _ in range(max_scrolls):
                    if resilience.is_shutdown():
                        break
                    page.evaluate(_SCROLL_JS)
                    time.sleep(0.6)

                # Extract all /@username links visible in the page
                raw: List[str] = page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('a[href^="/@"]'))
                        .map(a => a.getAttribute('href'))
                        .filter(h => h && h.startsWith('/@'))
                        .map(h => h.slice(2).split('/')[0].split('?')[0]);
                }""") or []

                seen: set = set()
                for u in raw:
                    u = u.strip().lstrip('@')
                    if u and u.lower() != username.lower() and u not in seen:
                        seen.add(u)
                        discovered.append(u)

                logger.debug(f"fetch_following_list @{username}: {len(discovered)} accounts")
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        logger.warning(f"fetch_following_list failed for @{username}: {exc}")

    return discovered


class BrowserDownloader:
    """Downloads TikTok content using browser automation to bypass anti-bot protection.
    
    This downloader launches a real browser (Chromium) and navigates to TikTok
    pages like a human user would. It can handle JavaScript challenges and
    access private accounts that gallery-dl cannot reach.
    
    Attributes:
        headless: Whether to run browser in headless mode (invisible)
        timeout: Maximum time to wait for page loads (seconds)
        user_data_dir: Optional persistent browser profile directory
    """
    
    VIDEO_ID_PATTERN = re.compile(r'/video/(\d{19})')
    LOGIN_COOKIE_NAMES = {'sessionid', 'sid_tt', 'uid_tt', 'ttwid', 'msToken'}
    
    def __init__(self, headless: bool = True, timeout: int = 30, user_data_dir: Optional[Path] = None):
        """Initialize browser downloader.
        
        Args:
            headless: Run browser without visible window
            timeout: Page load timeout in seconds
            user_data_dir: Optional directory for persistent browser profile
            
        Raises:
            ImportError: If Playwright is not installed and strict=True
        """
        if not PLAYWRIGHT_AVAILABLE:
            # Store params but don't raise — download_user_with_browser will return a
            # graceful error result instead of crashing at construction time.
            self.headless = headless
            self.timeout = timeout * 1000
            self.user_data_dir = user_data_dir
            return

        self.headless = headless
        self.timeout = timeout * 1000  # Convert to milliseconds for Playwright
        self.user_data_dir = user_data_dir
        
        logger.info(f"Browser downloader initialized (headless={headless}, timeout={timeout}s)")

    def _shutdown_result(self, url: str) -> List[DownloadResult]:
        return [DownloadResult(
            ok=False,
            url=url,
            status='failed',
            reason='Shutdown requested'
        )]

    def _has_login_cookies(self, cookies: List[Dict[str, Any]]) -> bool:
        return any(cookie.get('name') in self.LOGIN_COOKIE_NAMES for cookie in cookies)

    def _wait_for_shutdown_aware_timeout(self, page, milliseconds: int) -> bool:
        deadline = time.time() + (milliseconds / 1000)
        while time.time() < deadline:
            if resilience.is_shutdown():
                return False
            try:
                page.wait_for_timeout(250)
            except Exception:
                time.sleep(0.25)
        return True
    
    def download_user_with_browser(
        self,
        username: str,
        limit: int,
        output_dir: Path,
        cookies_file: Optional[Path] = None,
        chrome_user_data_dir: Optional[Path] = None
    ) -> List[DownloadResult]:
        """Download videos from user profile using browser automation.
        
        Args:
            username: TikTok username (without @)
            limit: Maximum number of videos to download
            output_dir: Directory to save downloaded videos
            cookies_file: Optional Netscape-format cookies file for authentication
            
        Returns:
            List of DownloadResult objects
        """
        logger.info(f"Starting browser automation download for @{username} (limit: {limit})")
        profile_url = f"https://www.tiktok.com/@{username}"

        if resilience.is_shutdown():
            return self._shutdown_result(profile_url)
        
        if not PLAYWRIGHT_AVAILABLE:
            return [DownloadResult(
                ok=False,
                url=f"https://www.tiktok.com/@{username}",
                status='failed',
                reason='Playwright not installed. Install with: pip install playwright && playwright install chromium'
            )]

        results = []
        
        # Detect Chrome user data dir if not provided
        # NOTE: Chrome persistent context is disabled — Chrome is almost always running
        # and its profile is locked, causing 3-minute timeouts before falling back.
        # Chromium with exported cookies works reliably instead.
        chrome_user_data_dir = None

        try:
            with sync_playwright() as p:
                # Always use Chromium with cookies — Chrome profile causes timeouts
                browser = self._launch_browser(p)
                context = self._create_context(browser, cookies_file)
                page = context.new_page()

                try:
                    if resilience.is_shutdown():
                        return self._shutdown_result(profile_url)

                    logger.info("Priming TikTok home page before profile navigation...")
                    try:
                        page.goto('https://www.tiktok.com/', timeout=min(self.timeout, 20000), wait_until='domcontentloaded')
                        self._wait_for_shutdown_aware_timeout(page, 1500)
                    except Exception as home_err:
                        logger.debug(f"Home page prime failed, continuing to profile: {home_err}")

                    if resilience.is_shutdown():
                        return self._shutdown_result(profile_url)

                    # Navigate to user profile
                    logger.info(f"Navigating to {profile_url}")
                    
                    try:
                        page.goto(profile_url, timeout=self.timeout, wait_until='domcontentloaded')
                    except PlaywrightTimeout:
                        logger.error(f"Timeout loading profile page for @{username}")
                        return [DownloadResult(
                            ok=False,
                            url=profile_url,
                            status='failed',
                            reason='Page load timeout'
                        )]
                    except Exception as nav_err:
                        err_str = str(nav_err)
                        if 'ERR_HTTP_RESPONSE_CODE_FAILURE' in err_str or 'net::ERR' in err_str:
                            # Playwright threw on non-2xx — check what actually loaded
                            logger.warning(f"Navigation error (may be recoverable): {err_str[:100]}")
                        else:
                            return [DownloadResult(
                                ok=False, url=profile_url, status='failed',
                                reason=f'Navigation failed: {err_str[:200]}'
                            )]
                    
                    # Give the page extra time to hydrate after domcontentloaded
                    # TikTok is a heavy SPA — JS needs time to render the video grid
                    logger.info("Waiting for page to fully hydrate...")
                    try:
                        page.wait_for_load_state('networkidle', timeout=15000)
                    except PlaywrightTimeout:
                        logger.debug("networkidle timeout — continuing anyway")

                    if resilience.is_shutdown():
                        return self._shutdown_result(profile_url)
                    
                    # Scroll down slightly to trigger lazy loading
                    page.evaluate("window.scrollBy(0, 300)")
                    logger.info("Waiting for content to load...")
                    
                    # TikTok has changed selectors over time — try multiple known ones
                    VIDEO_GRID_SELECTORS = [
                        '[data-e2e="user-post-item"]',
                        '[data-e2e="user-post-item-list"]',
                        'div[class*="DivItemContainerV2"]',
                        'div[class*="DivVideoFeedV2"]',
                        'a[href*="/video/"]',
                    ]
                    
                    grid_found = False
                    for selector in VIDEO_GRID_SELECTORS:
                        if resilience.is_shutdown():
                            return self._shutdown_result(profile_url)
                        try:
                            # Use 1/3 of total timeout per selector, min 15s, max 30s
                            per_selector_ms = max(15000, min(30000, self.timeout // 3))
                            page.wait_for_selector(selector, timeout=per_selector_ms)
                            logger.info(f"Video grid found with selector: {selector}")
                            grid_found = True
                            break
                        except PlaywrightTimeout:
                            logger.debug(f"Selector not found: {selector}")
                            continue
                    
                    if not grid_found:
                        logger.warning("Video grid not found with any known selector, checking page state")
                        
                        # Dump page title and a snippet for debugging
                        try:
                            title = page.title()
                            logger.warning(f"Page title: {title}")
                            body_text = page.inner_text('body')[:500] if page.query_selector('body') else ''
                            logger.warning(f"Page body snippet: {body_text}")
                        except Exception:
                            pass
                        
                        # Check if account is private or doesn't exist
                        page_content = page.content().lower()
                        if 'private account' in page_content or 'this account is private' in page_content:
                            return [DownloadResult(
                                ok=False,
                                url=profile_url,
                                status='failed',
                                reason='Private account (not following or not logged in)'
                            )]
                        elif "couldn't find this account" in page_content or 'user not found' in page_content:
                            return [DownloadResult(
                                ok=False,
                                url=profile_url,
                                status='failed',
                                reason='Account not found'
                            )]
                        elif 'login' in page_content and 'sign up' in page_content and not self._has_login_cookies(context.cookies()):
                            return [DownloadResult(
                                ok=False,
                                url=profile_url,
                                status='failed',
                                reason='Not logged in — cookies missing, expired, or not applied to TikTok domain'
                            )]
                        else:
                            return [DownloadResult(
                                ok=False,
                                url=profile_url,
                                status='failed',
                                reason='Could not find video grid — TikTok page structure may have changed or anti-bot blocked the page'
                            )]
                    
                    # Verify we're actually on the right profile page
                    try:
                        current_url = page.url
                        page_title = page.title()
                        active_cookies = context.cookies()
                        tiktok_cookies = [cookie for cookie in active_cookies if 'tiktok.com' in cookie.get('domain', '')]
                        logger.debug(f"Current URL after load: {current_url}")
                        logger.debug(f"Page title: {page_title}")
                        logger.info(f"Browser context cookies: total={len(active_cookies)}, tiktok={len(tiktok_cookies)}, has_login_cookie={self._has_login_cookies(tiktok_cookies)}")
                        # If we got redirected away from the profile, bail out
                        if username.lower() not in current_url.lower() and username.lower() not in page_title.lower():
                            page_content = page.content().lower()
                            if 'private account' in page_content:
                                return [DownloadResult(ok=False, url=profile_url, status='failed',
                                    reason='Private account (not following or not logged in)')]
                            return [DownloadResult(ok=False, url=profile_url, status='failed',
                                reason=f'Page redirected away from profile (landed on: {current_url})')]
                    except Exception:
                        pass

                    # Scroll down to trigger lazy loading before extracting URLs
                    try:
                        for _ in range(3):
                            if resilience.is_shutdown():
                                return self._shutdown_result(profile_url)
                            page.evaluate("window.scrollBy(0, 800)")
                            time.sleep(0.5)
                    except Exception:
                        pass

                    # Extract video URLs
                    logger.info("Extracting video URLs from page...")
                    video_urls = self._extract_video_urls(page, limit)

                    if not video_urls:
                        # Log all hrefs found on page for debugging
                        try:
                            all_hrefs = page.evaluate("""
                                Array.from(document.querySelectorAll('a[href]'))
                                    .map(a => a.href)
                                    .filter(h => h.includes('tiktok.com'))
                                    .slice(0, 20)
                            """)
                            logger.warning(f"No /video/ links found. Sample hrefs on page: {all_hrefs}")
                        except Exception:
                            pass
                        logger.warning(f"No videos found for @{username}")
                        return [DownloadResult(
                            ok=False,
                            url=profile_url,
                            status='failed',
                            reason='No videos found on profile'
                        )]
                    
                    logger.info(f"Found {len(video_urls)} video URLs")
                    
                    # Download each video
                    for idx, video_url in enumerate(video_urls, 1):
                        if resilience.is_shutdown():
                            if not results:
                                return self._shutdown_result(profile_url)
                            break
                        logger.info(f"Downloading video {idx}/{len(video_urls)}: {video_url}")
                        result = self._download_video(page, video_url, output_dir, username)
                        results.append(result)
                        
                        # Small delay between downloads to avoid rate limiting
                        if idx < len(video_urls):
                            time.sleep(1)
                    
                finally:
                    try:
                        context.close()
                    except Exception:
                        pass
                    try:
                        browser.close()
                    except Exception:
                        pass
                    
        except Exception as e:
            logger.error(f"Browser automation failed: {e}")
            if not results:
                results.append(DownloadResult(
                    ok=False,
                    url=f"https://www.tiktok.com/@{username}",
                    status='failed',
                    reason=f"Browser automation error: {str(e)}"
                ))
        
        successful = len([r for r in results if r.ok])
        logger.info(f"Browser automation complete: {successful}/{len(results)} successful")
        
        return results
    
    def _launch_browser(self, playwright):
        """Launch Chromium browser with appropriate settings."""
        launch_options: Dict[str, Any] = {'headless': self.headless, 'args': _CHROMIUM_ARGS}
        if self.user_data_dir:
            launch_options['user_data_dir'] = str(self.user_data_dir)
        return playwright.chromium.launch(**launch_options)

    def _create_context(self, browser, cookies_file: Optional[Path]):
        """Create browser context with cookies loaded."""
        context = browser.new_context(**_CONTEXT_OPTIONS)
        context.add_init_script(_STEALTH_SCRIPT)
        if cookies_file and cookies_file.exists():
            logger.info(f"Loading cookies from {cookies_file}")
            cookies = _parse_netscape_cookies(cookies_file)
            if cookies:
                context.add_cookies(cookies)
                loaded = context.cookies()
                tiktok = [c for c in loaded if 'tiktok.com' in c.get('domain', '')]
                logger.info(
                    f"Loaded {len(cookies)} cookies; tiktok={len(tiktok)}; "
                    f"has_login_cookie={self._has_login_cookies(tiktok)}"
                )
                if not self._has_login_cookies(tiktok):
                    logger.warning(
                        "Cookies loaded but no TikTok login cookies detected "
                        "(expected sessionid/sid_tt/uid_tt/ttwid/msToken)"
                    )
            else:
                logger.warning("No valid cookies found in file")
        return context
    
    def _parse_netscape_cookies(self, cookies_file: Path) -> List[Dict[str, Any]]:
        """Parse Netscape-format cookies file into Playwright format."""
        return _parse_netscape_cookies(cookies_file)
    
    def _extract_video_urls(self, page, limit: int) -> List[str]:
        """Extract video URLs from user profile page.
        
        Args:
            page: Playwright page object
            limit: Maximum number of URLs to extract
            
        Returns:
            List of video URLs
        """
        video_urls = []
        
        try:
            if resilience.is_shutdown():
                return video_urls
            # Strategy 1: find all <a> tags pointing to /video/ directly (most reliable)
            all_links = page.query_selector_all('a[href*="/video/"]')
            for link in all_links:
                href = link.get_attribute('href')
                if href and '/video/' in href:
                    if href.startswith('/'):
                        href = f"https://www.tiktok.com{href}"
                    if href not in video_urls:
                        video_urls.append(href)
                    if len(video_urls) >= limit:
                        break
            
            if video_urls:
                logger.debug(f"Extracted {len(video_urls)} video URLs via direct link scan")
                return video_urls
            
            # Strategy 2: data-e2e post item containers
            for container_selector in ['[data-e2e="user-post-item"]', '[data-e2e="user-post-item-list"] a']:
                elements = page.query_selector_all(container_selector)
                for elem in elements[:limit]:
                    link = elem if elem.get_attribute('href') else elem.query_selector('a')
                    if link:
                        href = link.get_attribute('href')
                        if href and '/video/' in href:
                            if href.startswith('/'):
                                href = f"https://www.tiktok.com{href}"
                            if href not in video_urls:
                                video_urls.append(href)
                    if len(video_urls) >= limit:
                        break
                if video_urls:
                    logger.debug(f"Extracted {len(video_urls)} video URLs via container selector: {container_selector}")
                    break

        except Exception as e:
            logger.error(f"Failed to extract video URLs: {e}")
        
        return video_urls[:limit]
    
    def _download_video(self, page, video_url: str, output_dir: Path, username: str) -> DownloadResult:
        """Download a single video using browser network interception.

        TikTok serves video via blob: URLs in the <video> element, which cannot
        be fetched directly. Instead we intercept the underlying CDN network
        request that the browser makes when the video starts playing, capture
        the real https:// CDN URL, and download from there.
        """
        try:
            if resilience.is_shutdown():
                return DownloadResult(
                    ok=False, url=video_url, status='failed',
                    reason='Shutdown requested'
                )

            video_id = self._extract_video_id(video_url)
            if not video_id:
                return DownloadResult(
                    ok=False, url=video_url, status='failed',
                    reason='Could not extract video ID from URL'
                )

            # Flat layout: video_id.mp4
            filename = f"{video_id}.mp4"
            filepath = output_dir / filename

            if filepath.exists():
                logger.info(f"Video already exists: {filename}")
                return DownloadResult(
                    ok=True, url=video_url, status='skipped',
                    filepath=filepath, reason='File already exists',
                    meta={'video_id': video_id}
                )

            # Intercept network requests to capture the real CDN video URL.
            # TikTok CDN URLs contain 'mime_type=video' or end in .mp4 and come
            # from v19-webapp.tiktok.com / v26-webapp.tiktok.com etc.
            captured_cdn_url: list = []

            def handle_request(request):
                url = request.url
                if (
                    not captured_cdn_url
                    and ('mime_type=video' in url or url.endswith('.mp4'))
                    and ('tiktok.com' in url or 'tiktokcdn.com' in url or 'tiktokv.com' in url)
                    and not url.startswith('blob:')
                ):
                    logger.debug(f"Captured CDN URL: {url[:120]}")
                    captured_cdn_url.append(url)

            page.on('request', handle_request)

            try:
                page.goto(video_url, timeout=self.timeout, wait_until='domcontentloaded')
            except PlaywrightTimeout:
                return DownloadResult(
                    ok=False, url=video_url, status='failed',
                    reason='Video page load timeout'
                )

            # Wait for video element then trigger playback to fire CDN request
            try:
                page.wait_for_selector('video', timeout=self.timeout)
            except PlaywrightTimeout:
                return DownloadResult(
                    ok=False, url=video_url, status='failed',
                    reason='Video element not found on page'
                )

            # Click play / unmute to trigger the CDN fetch
            try:
                page.evaluate("""
                    const v = document.querySelector('video');
                    if (v) { v.muted = true; v.play(); }
                """)
            except Exception:
                pass

            # Give the browser up to 10 seconds to fire the CDN request
            deadline = time.time() + 10
            while not captured_cdn_url and time.time() < deadline:
                if resilience.is_shutdown():
                    page.remove_listener('request', handle_request)
                    return DownloadResult(
                        ok=False, url=video_url, status='failed',
                        reason='Shutdown requested'
                    )
                time.sleep(0.2)

            page.remove_listener('request', handle_request)

            if not captured_cdn_url:
                # Fallback: try reading src attribute (may still be blob, will fail gracefully)
                video_elem = page.query_selector('video')
                src = video_elem.get_attribute('src') if video_elem else None
                if src and not src.startswith('blob:'):
                    captured_cdn_url.append(src)
                else:
                    return DownloadResult(
                        ok=False, url=video_url, status='failed',
                        reason='Could not capture CDN video URL (blob stream only — video may be DRM protected)'
                    )

            cdn_url = captured_cdn_url[0]
            logger.info(f"Downloading video from CDN: {cdn_url[:80]}...")

            # Use Playwright's request context — it carries the browser session
            # cookies and headers that signed CDN URLs require.
            # urllib.request fails on -prime CDN URLs because they're session-signed.
            output_dir.mkdir(parents=True, exist_ok=True)
            try:
                response = page.request.get(
                    cdn_url,
                    headers={
                        'Referer': 'https://www.tiktok.com/',
                        'Origin': 'https://www.tiktok.com',
                    },
                    timeout=120000  # 2 minutes
                )
                if response.status not in (200, 206):
                    return DownloadResult(
                        ok=False, url=video_url, status='failed',
                        reason=f'CDN download failed with HTTP {response.status}'
                    )
                data = response.body()
                if len(data) < 10000:  # less than 10KB is definitely not a real video
                    return DownloadResult(
                        ok=False, url=video_url, status='failed',
                        reason=f'CDN returned suspiciously small response ({len(data)} bytes) — likely an error page'
                    )
                filepath.write_bytes(data)
            except Exception as e:
                return DownloadResult(
                    ok=False, url=video_url, status='failed',
                    reason=f'CDN download error: {e}'
                )

            file_size = filepath.stat().st_size
            logger.info(f"Saved video: {filename} ({file_size:,} bytes)")

            return DownloadResult(
                ok=True, url=video_url, status='downloaded',
                filepath=filepath,
                meta={'video_id': video_id, 'size': file_size}
            )

        except Exception as e:
            logger.error(f"Failed to download video {video_url}: {e}")
            return DownloadResult(
                ok=False, url=video_url, status='failed',
                reason=f'Download error: {str(e)}'
            )
    
    def _extract_video_id(self, url: str) -> Optional[str]:
        """Extract video ID from TikTok URL.
        
        Args:
            url: TikTok video URL
            
        Returns:
            19-digit video ID or None
        """
        match = self.VIDEO_ID_PATTERN.search(url)
        return match.group(1) if match else None
