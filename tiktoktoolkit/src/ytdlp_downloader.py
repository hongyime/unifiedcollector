"""yt-dlp downloader with curl-cffi browser impersonation for TikTok.

Used as a secondary fallback when gallery-dl fails with 403 / TLS fingerprint
rejection. yt-dlp with --impersonate chrome routes HTTP through curl-cffi which
patches the TLS ClientHello to match a real Chrome fingerprint, bypassing
TikTok's CDN-level bot detection.

Download chain:
  gallery-dl (primary) → YtDlpDownloader (secondary) → BrowserDownloader (last resort)
"""

import logging
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .models import DownloadResult

logger = logging.getLogger('uttk.ytdlp_downloader')

VIDEO_ID_RE = re.compile(r'(\d{15,})')

# Use the same Python executable that's running this code — ensures we use
# the venv's yt-dlp rather than whatever is (or isn't) on the system PATH.
_YTDLP_CMD = [sys.executable, '-m', 'yt_dlp']


class YtDlpDownloader:
    """Downloads TikTok videos using yt-dlp with curl-cffi browser impersonation.

    Attributes:
        cookies_file: Path to Netscape-format cookies file for authentication.
        timeout: Subprocess timeout in seconds.
    """

    def __init__(self, cookies_file: Optional[Path] = None, timeout: int = 120):
        self.cookies_file = cookies_file
        self.timeout = timeout
        self._available: Optional[bool] = None  # cached after first check

    def is_available(self) -> bool:
        """Return True if yt-dlp and curl-cffi are importable."""
        if self._available is not None:
            return self._available

        # Check yt-dlp is runnable
        try:
            result = subprocess.run(
                _YTDLP_CMD + ['--version'],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                logger.warning("yt-dlp not working")
                self._available = False
                return False
            logger.debug(f"yt-dlp version: {result.stdout.strip()}")
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning(f"yt-dlp unavailable: {e}")
            self._available = False
            return False

        # Check curl-cffi is importable (required for --impersonate)
        try:
            import curl_cffi  # noqa: F401
            logger.info("yt-dlp with curl-cffi impersonation is available")
            self._available = True
            return True
        except ImportError:
            logger.warning("curl-cffi not installed — run: pip install curl-cffi")
            self._available = False
            return False

    def download_user_videos(
        self,
        username: str,
        limit: int,
        output_dir: Path,
    ) -> List[DownloadResult]:
        """Download up to `limit` videos from @username using yt-dlp.

        Args:
            username: TikTok username (without @).
            limit: Maximum number of videos to download.
            output_dir: Directory to save downloaded videos.

        Returns:
            List of DownloadResult objects.
        """
        profile_url = f"https://www.tiktok.com/@{username}"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Output template — flat layout: video_id.ext
        output_template = str(output_dir / '%(id)s.%(ext)s')

        cmd = _YTDLP_CMD + [
            '--impersonate', 'chrome',
            '--playlist-end', str(limit),
            '--output', output_template,
            '--no-playlist',
            '--quiet',
            '--no-warnings',
            '--print', 'after_move:filepath',  # emit final path after each download
        ]

        if self.cookies_file and self.cookies_file.exists():
            cmd.extend(['--cookies', str(self.cookies_file)])

        cmd.append(profile_url)

        logger.info(f"yt-dlp downloading @{username} (limit: {limit})")
        logger.debug(f"yt-dlp command: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            logger.error(f"yt-dlp timed out after {self.timeout}s for @{username}")
            return [DownloadResult(
                ok=False,
                url=profile_url,
                status='failed',
                reason=f'yt-dlp timed out after {self.timeout}s'
            )]
        except Exception as e:
            logger.error(f"yt-dlp execution error for @{username}: {e}")
            return [DownloadResult(
                ok=False,
                url=profile_url,
                status='failed',
                reason=f'yt-dlp execution error: {e}'
            )]

        # Parse --print after_move:filepath output — one path per line
        results: List[DownloadResult] = []
        downloaded_paths = [
            line.strip() for line in result.stdout.splitlines()
            if line.strip() and not line.strip().startswith('[')
        ]

        for filepath_str in downloaded_paths:
            filepath = Path(filepath_str)
            if filepath.exists() and filepath.is_file():
                video_id = self._extract_video_id(filepath.name)
                size = filepath.stat().st_size
                logger.info(f"yt-dlp saved: {filepath.name} ({size:,} bytes)")
                results.append(DownloadResult(
                    ok=True,
                    url=profile_url,
                    status='downloaded',
                    filepath=filepath,
                    meta={'video_id': video_id, 'size': size}
                ))
            else:
                logger.debug(f"yt-dlp reported path but file not found: {filepath_str}")

        if result.returncode != 0 and not results:
            stderr_snippet = result.stderr.strip()[:300] if result.stderr else 'no stderr'
            logger.warning(f"yt-dlp failed (code {result.returncode}) for @{username}: {stderr_snippet}")
            return [DownloadResult(
                ok=False,
                url=profile_url,
                status='failed',
                reason=f'yt-dlp exit {result.returncode}: {stderr_snippet}'
            )]

        if not results:
            logger.warning(f"yt-dlp produced no output files for @{username}")
            return [DownloadResult(
                ok=False,
                url=profile_url,
                status='failed',
                reason='yt-dlp completed but no files were downloaded'
            )]

        logger.info(f"yt-dlp downloaded {len(results)} videos for @{username}")
        return results

    def _extract_video_id(self, filename: str) -> Optional[str]:
        """Extract TikTok video ID from filename."""
        match = VIDEO_ID_RE.search(filename)
        return match.group(1) if match else None
