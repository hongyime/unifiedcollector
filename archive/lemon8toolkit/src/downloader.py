"""
Unified Lemon8 Toolkit - Media Download Manager
"""
import json
import logging
import os
import re
import struct
import time
import hashlib
import sqlite3
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import (
    parse_qsl,
    unquote,
    urlencode,
    urlparse,
    urlunparse,
)
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

import config


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

class MediaDownloader:
    def __init__(self, session: Optional[requests.Session] = None, auto_save: bool = True):
        config.ensure_data_directory()
        self.auto_save = auto_save
        self.downloaded_media: Set[str] = set()
        self._init_sqlite()
        self._sync_sqlite_and_json()
        
        self.logger = self._get_logger()
        
        if session:
            self.session = session
        else:
            self.session = requests.Session()
            _retry = Retry(total=3, backoff_factor=1.0, status_forcelist={429, 500, 502, 503, 504}, allowed_methods={"GET"}, raise_on_status=False)
            _adapter = HTTPAdapter(max_retries=_retry)
            self.session.mount("https://", _adapter)
            self.session.mount("http://", _adapter)
            self.session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
            })
        
        self.downloads_dir = None

    def _init_sqlite(self):
        """Initialize SQLite database and table for media deduplication."""
        self.conn = sqlite3.connect(config.LEMON8_DB_FILE, timeout=5.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        config.configure_db_connection(self.conn)
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS downloaded_media (
                url_hash TEXT PRIMARY KEY,
                downloaded_at TEXT,
                status TEXT DEFAULT 'completed'
            )
        ''')
        # Add status column if missing (migration for existing databases)
        cursor.execute("PRAGMA table_info(downloaded_media)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        if 'status' not in existing_cols:
            cursor.execute("ALTER TABLE downloaded_media ADD COLUMN status TEXT DEFAULT 'completed'")
            cursor.execute("UPDATE downloaded_media SET status = 'completed' WHERE status IS NULL")
        self.conn.commit()

    def _sync_sqlite_and_json(self):
        """Sync SQLite and JSON for deduplication on startup."""
        json_hashes = set()
        if os.path.exists(config.DOWNLOADED_MEDIA_FILE):
            try:
                with open(config.DOWNLOADED_MEDIA_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        json_hashes = set(data)
                    elif isinstance(data, dict) and 'downloaded_urls' in data:
                        json_hashes = set(data['downloaded_urls'])
            except Exception as e:
                print(f"⚠️ Error loading downloaded media file: {e}")

        cursor = self.conn.cursor()
        cursor.execute("SELECT url_hash FROM downloaded_media")
        sqlite_hashes = {row['url_hash'] for row in cursor.fetchall()}
        
        merged_count = 0
        now = datetime.now().isoformat()
        
        # Add any JSON hashes that are missing from SQLite
        for h in json_hashes:
            if h not in sqlite_hashes:
                cursor.execute(
                    "INSERT INTO downloaded_media (url_hash, downloaded_at, status) VALUES (?, ?, 'completed')",
                    (h, now)
                )
                merged_count += 1

        if merged_count > 0:
            self.conn.commit()
            print(f"💾 Merged {merged_count} downloaded media items from JSON to SQLite")

        # Load only completed downloads into in-memory cache
        cursor.execute("SELECT url_hash FROM downloaded_media WHERE status = 'completed'")
        self.downloaded_media = {row['url_hash'] for row in cursor.fetchall()}
        
        # Save back to JSON to ensure backup is fully synced
        self._save_downloaded_media()

    def _get_logger(self) -> logging.Logger:
        """Configure a file logger for download verification events."""
        logger = logging.getLogger("lemon8.media_downloader")
        if logger.handlers:
            return logger

        logger.setLevel(logging.INFO)
        handler = logging.FileHandler(config.DOWNLOAD_VERIFICATION_LOG_FILE, encoding='utf-8')
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.propagate = False
        return logger

    def _log_event(self, level: str, message: str, **context: Any):
        """Write structured download events to the verification log."""
        details = ""
        if context:
            details = f" | context={json.dumps(context, ensure_ascii=False, sort_keys=True)}"
        log_message = f"{message}{details}"
        getattr(self.logger, level, self.logger.info)(log_message)
        
    def _save_downloaded_media(self):
        """Save set of downloaded media URLs/hashes to JSON backup"""
        try:
            data = {
                'downloaded_urls': list(self.downloaded_media),
                'last_updated': datetime.now().isoformat(),
                'total_count': len(self.downloaded_media)
            }
            tmp = config.DOWNLOADED_MEDIA_FILE + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, config.DOWNLOADED_MEDIA_FILE)
        except (OSError, TypeError) as e:
            print(f"⚠️ Error saving downloaded media backup file: {e}")
    
    def _get_downloads_dir(self):
        """Get downloads directory, prompting user if not set"""
        if self.downloads_dir is None:
            self.downloads_dir = config.get_downloads_directory()
        return self.downloads_dir
    
    def _get_url_hash(self, url: str) -> str:
        """
        Get MD5 hash of a URL's base part (without query parameters) 
        for reliable deduplication across sessions.
        """
        base_url = url.split('?')[0]
        # Also remove shrinking patterns from hash for better matching
        base_url = re.sub(r'~tplv-[a-z0-9\-]+-shrink:\d+:\d+:q\d+\.[a-z]+', '', base_url)
        base_url = re.sub(r'~tplv-[a-z0-9\-]+-image\.[a-z]+', '', base_url)
        
        return hashlib.md5(base_url.encode()).hexdigest()

    def _is_image_url(self, url: str) -> bool:
        """Check whether the URL targets an image asset."""
        if not url:
            return False
        url_lower = url.lower()
        return any(ext in url_lower for ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'])

    def _is_profile_photo_url(self, url: str) -> bool:
        """Detect profile-photo style image URLs."""
        if not url:
            return False
        url_lower = url.lower()
        return any(token in url_lower for token in ['user-avatar', 'avatar', 'profile_pic', 'profile-photo'])

    def _url_looks_shrunk(self, url: str) -> bool:
        """Check if the URL appears to reference a resized or thumbnail variant."""
        if not url:
            return False

        url_lower = url.lower()
        shrink_tokens = [
            'shrink',
            'resize',
            'thumb',
            'thumbnail',
            '_small',
            '-small',
            '_mini',
            'preview',
        ]
        if any(token in url_lower for token in shrink_tokens):
            return True

        hints = self._extract_url_quality_hints(url)
        width = hints.get('width') or 0
        height = hints.get('height') or 0
        quality = hints.get('quality') or 0

        return (
            (0 < width < config.HIGH_QUALITY_IMAGE_WIDTH) or
            (0 < height < config.HIGH_QUALITY_IMAGE_HEIGHT) or
            (0 < quality < config.HIGH_QUALITY_IMAGE_QUALITY)
        )

    def _extract_url_quality_hints(self, url: str) -> Dict[str, Optional[int]]:
        """Extract width, height, and quality hints from image URLs."""
        hints: Dict[str, Optional[int]] = {
            'width': None,
            'height': None,
            'quality': None,
        }
        if not url:
            return hints

        parsed = urlparse(url)
        combined = f"{parsed.path}?{parsed.query}".lower()

        for pattern in [
            r'shrink[:_=-](\d{2,4})[:_x-](\d{1,4})[:_x-]q(\d{1,3})',
            r'resize[:_=-](\d{2,4})[:_x-](\d{1,4})[:_x-]q(\d{1,3})',
        ]:
            match = re.search(pattern, combined)
            if match:
                hints['width'] = int(match.group(1))
                hints['height'] = int(match.group(2))
                hints['quality'] = int(match.group(3))
                return hints

        dim_match = re.search(r'(\d{2,4})x(\d{2,4})', combined)
        if dim_match:
            hints['width'] = int(dim_match.group(1))
            hints['height'] = int(dim_match.group(2))

        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            lowered_key = key.lower()
            lowered_value = value.lower()
            if lowered_key in {'w', 'width', 'maxw'} and value.isdigit():
                hints['width'] = int(value)
            elif lowered_key in {'h', 'height', 'maxh'} and value.isdigit():
                hints['height'] = int(value)
            elif lowered_key in {'q', 'quality', 'img_quality'} and value.isdigit():
                hints['quality'] = int(value)
            elif lowered_key in {'s', 'size'}:
                size_match = re.match(r'(\d{2,4})x(\d{2,4})', lowered_value)
                if size_match:
                    hints['width'] = int(size_match.group(1))
                    hints['height'] = int(size_match.group(2))

        quality_match = re.search(r'q(\d{1,3})', combined)
        if quality_match and hints['quality'] is None:
            hints['quality'] = int(quality_match.group(1))

        return hints

    def _enhance_image_url(self, url: str) -> List[str]:
        """
        Build high-quality image URL candidates by removing or replacing
        common shrink and thumbnail conventions while preserving the original
        URL as a fallback.
        """
        if not url or not self._is_image_url(url):
            return [url]

        parsed = urlparse(url)
        candidates: List[str] = []

        def add_candidate(path: str, query_items: List[Tuple[str, str]]):
            candidate = urlunparse((
                parsed.scheme,
                parsed.netloc,
                path,
                parsed.params,
                urlencode(query_items, doseq=True),
                parsed.fragment,
            ))
            if candidate not in candidates:
                candidates.append(candidate)

        original_query_items = parse_qsl(parsed.query, keep_blank_values=True)

        high_quality_path = parsed.path
        high_quality_path = re.sub(
            r'~tplv-([a-z0-9\-]+)-shrink[:_]\d+[:_]\d+[:_]q\d+(\.[a-z0-9]+)',
            r'~tplv-\1-image\2',
            high_quality_path,
            flags=re.IGNORECASE,
        )
        high_quality_path = re.sub(
            r'([_-])(thumb|thumbnail|small|mini|preview)(?=[._-])',
            r'\1large',
            high_quality_path,
            flags=re.IGNORECASE,
        )
        high_quality_path = re.sub(
            r'([_-])(thumb|thumbnail|small|mini|preview)(\.[a-z0-9]+)$',
            r'\3',
            high_quality_path,
            flags=re.IGNORECASE,
        )

        enhanced_query_items: List[Tuple[str, str]] = []
        seen_query_keys = set()
        for key, value in original_query_items:
            lowered_key = key.lower()
            seen_query_keys.add(lowered_key)
            if lowered_key in {'w', 'width', 'maxw'}:
                enhanced_query_items.append((key, str(config.HIGH_QUALITY_IMAGE_WIDTH)))
            elif lowered_key in {'h', 'height', 'maxh'}:
                enhanced_query_items.append((key, str(config.HIGH_QUALITY_IMAGE_HEIGHT)))
            elif lowered_key in {'q', 'quality', 'img_quality'}:
                enhanced_query_items.append((key, str(config.HIGH_QUALITY_IMAGE_QUALITY)))
            elif lowered_key in {'s', 'size'}:
                enhanced_query_items.append(
                    (key, f"{config.HIGH_QUALITY_IMAGE_WIDTH}x{config.HIGH_QUALITY_IMAGE_HEIGHT}")
                )
            else:
                enhanced_query_items.append((key, value))

        if 'w' not in seen_query_keys and 'width' not in seen_query_keys:
            enhanced_query_items.append(('w', str(config.HIGH_QUALITY_IMAGE_WIDTH)))
        if 'h' not in seen_query_keys and 'height' not in seen_query_keys:
            enhanced_query_items.append(('h', str(config.HIGH_QUALITY_IMAGE_HEIGHT)))
        if 'q' not in seen_query_keys and 'quality' not in seen_query_keys:
            enhanced_query_items.append(('q', str(config.HIGH_QUALITY_IMAGE_QUALITY)))

        add_candidate(high_quality_path, enhanced_query_items)
        add_candidate(high_quality_path, original_query_items)
        add_candidate(parsed.path, enhanced_query_items)
        add_candidate(parsed.path, original_query_items)

        return candidates

    def _normalize_username(self, username: Optional[str]) -> str:
        """Sanitize the username used as a filename prefix."""
        value = (username or "").strip().lstrip('@')
        value = unquote(value)
        value = re.sub(r'\s+', '_', value)
        value = re.sub(r'[^A-Za-z0-9._-]+', '_', value)
        value = value.strip('._-')[:config.USERNAME_MAX_LENGTH]

        if config.STRICT_USERNAME_VALIDATION and not value:
            raise ValueError("Username prefix is empty after sanitization")

        return value or "unknownuser"
    
    def _get_filename_from_url(self, url: str, default_ext: str = 'mp4') -> str:
        """
        Extract filename from URL and sanitize for Windows/Linux filesystems.
        """
        # Get path part without query parameters
        path = url.split('?')[0]
        filename = os.path.basename(path)
        
        if not filename or '.' not in filename:
            # Fallback for URLs without a clear filename
            url_hash = self._get_url_hash(url)
            filename = f"media_{url_hash[:12]}.{default_ext}"
        
        # Sanitize filename: replace characters that are invalid on Windows/Linux
        # Invalid: < > : " / \ | ? *
        invalid_chars = '<>:"/\\|?*'
        for char in invalid_chars:
            filename = filename.replace(char, '_')
            
        return filename

    def _build_prefixed_filename(self, username: str, filename: str) -> str:
        """Prefix a filename with the sanitized username once."""
        if not config.USERNAME_PREFIX_ENABLED:
            return filename

        username_prefix = self._normalize_username(username)
        if filename.startswith(f"{username_prefix}_"):
            return filename

        name, ext = os.path.splitext(filename)
        prefixed_name = f"{username_prefix}_{name}{ext}"
        invalid_chars = '<>:"/\\|?*'
        for char in invalid_chars:
            prefixed_name = prefixed_name.replace(char, '_')
        return prefixed_name

    def _ensure_unique_filename(self, directory: str, filename: str) -> str:
        """Avoid collisions by appending an incrementing suffix."""
        candidate = filename
        stem, ext = os.path.splitext(filename)
        counter = 1

        while os.path.exists(os.path.join(directory, candidate)):
            candidate = f"{stem}_{counter}{ext}"
            counter += 1

        return candidate

    def _get_image_info(self, file_path: str) -> Dict[str, Optional[int]]:
        """Read basic image metadata without external dependencies."""
        info: Dict[str, Optional[int]] = {
            'width': None,
            'height': None,
            'format': None,
            'file_size_bytes': 0,
        }

        try:
            with open(file_path, 'rb') as image_file:
                data = image_file.read(256)
                info['file_size_bytes'] = os.path.getsize(file_path)

                if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24:
                    info['format'] = 'png'
                    info['width'], info['height'] = struct.unpack(">II", data[16:24])
                elif data.startswith(b'GIF87a') or data.startswith(b'GIF89a'):
                    info['format'] = 'gif'
                    info['width'], info['height'] = struct.unpack("<HH", data[6:10])
                elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
                    info['format'] = 'webp'
                    chunk_type = data[12:16]
                    if chunk_type == b'VP8X' and len(data) >= 30:
                        width_minus_one = int.from_bytes(data[24:27], 'little')
                        height_minus_one = int.from_bytes(data[27:30], 'little')
                        info['width'] = width_minus_one + 1
                        info['height'] = height_minus_one + 1
                    elif chunk_type == b'VP8 ' and len(data) >= 30:
                        info['width'], info['height'] = struct.unpack("<HH", data[26:30])
                    elif chunk_type == b'VP8L' and len(data) >= 25:
                        bits = int.from_bytes(data[21:25], 'little')
                        info['width'] = (bits & 0x3FFF) + 1
                        info['height'] = ((bits >> 14) & 0x3FFF) + 1
                elif data.startswith(b'\xff\xd8'):
                    info['format'] = 'jpeg'
                    with open(file_path, 'rb') as jpeg_file:
                        jpeg_file.read(2)
                        while True:
                            marker_prefix = jpeg_file.read(1)
                            if not marker_prefix:
                                break
                            if marker_prefix != b'\xff':
                                continue
                            marker_code = jpeg_file.read(1)
                            while marker_code == b'\xff':
                                marker_code = jpeg_file.read(1)
                            if not marker_code or marker_code in {b'\xd8', b'\xd9'}:
                                continue
                            segment_length_bytes = jpeg_file.read(2)
                            if len(segment_length_bytes) != 2:
                                break
                            segment_length = struct.unpack(">H", segment_length_bytes)[0]
                            if marker_code in {
                                b'\xc0', b'\xc1', b'\xc2', b'\xc3',
                                b'\xc5', b'\xc6', b'\xc7',
                                b'\xc9', b'\xca', b'\xcb',
                                b'\xcd', b'\xce', b'\xcf',
                            }:
                                jpeg_file.read(1)
                                info['height'], info['width'] = struct.unpack(">HH", jpeg_file.read(4))
                                break
                            jpeg_file.seek(segment_length - 2, os.SEEK_CUR)
        except (OSError, struct.error, EOFError, ValueError) as e:
            logging.getLogger("lemon8.media_downloader").debug(f"Image parse failed for {file_path}: {e}")
            return info

        return info

    def _compare_image_quality(
        self,
        original_url: str,
        enhanced_url: str,
        original_info: Optional[Dict[str, Optional[int]]] = None,
        enhanced_info: Optional[Dict[str, Optional[int]]] = None,
    ) -> Dict[str, Any]:
        """Compare original and enhanced image references to confirm an upgrade."""
        original_hints = self._extract_url_quality_hints(original_url)
        enhanced_hints = self._extract_url_quality_hints(enhanced_url)

        original_width = (original_info or {}).get('width') or original_hints.get('width') or 0
        original_height = (original_info or {}).get('height') or original_hints.get('height') or 0
        original_quality = original_hints.get('quality') or 0
        original_size = (original_info or {}).get('file_size_bytes') or 0

        enhanced_width = (enhanced_info or {}).get('width') or enhanced_hints.get('width') or 0
        enhanced_height = (enhanced_info or {}).get('height') or enhanced_hints.get('height') or 0
        enhanced_quality = enhanced_hints.get('quality') or 0
        enhanced_size = (enhanced_info or {}).get('file_size_bytes') or 0

        width_better = enhanced_width >= original_width and enhanced_width > 0
        height_better = enhanced_height >= original_height and enhanced_height > 0
        quality_better = enhanced_quality >= original_quality and enhanced_quality > 0
        size_better = enhanced_size >= original_size and enhanced_size > 0
        shrink_removed = self._url_looks_shrunk(original_url) and not self._url_looks_shrunk(enhanced_url)

        is_higher_quality = any([width_better, height_better, quality_better, size_better, shrink_removed])

        return {
            'is_higher_quality': is_higher_quality,
            'width_better': width_better,
            'height_better': height_better,
            'quality_better': quality_better,
            'size_better': size_better,
            'shrink_removed': shrink_removed,
            'original_hints': original_hints,
            'enhanced_hints': enhanced_hints,
        }

    def _verify_image_download(
        self,
        save_path: str,
        original_url: str,
        final_url: str,
        username: str,
        is_profile_photo: bool = False,
    ) -> Dict[str, Any]:
        """Verify image quality thresholds, filename prefix, and enhancement status."""
        image_info = self._get_image_info(save_path)
        comparison = self._compare_image_quality(original_url, final_url, enhanced_info=image_info)
        filename = os.path.basename(save_path)
        filename_has_prefix = filename.startswith(f"{self._normalize_username(username)}_")
        min_width = config.MIN_PROFILE_IMAGE_WIDTH if is_profile_photo else config.MIN_IMAGE_WIDTH
        min_height = config.MIN_PROFILE_IMAGE_HEIGHT if is_profile_photo else config.MIN_IMAGE_HEIGHT
        min_file_size = (
            config.MIN_PROFILE_IMAGE_FILE_SIZE_BYTES if is_profile_photo else config.MIN_IMAGE_FILE_SIZE_BYTES
        )
        thresholds_met = (
            (image_info.get('width') or 0) >= min_width and
            (image_info.get('height') or 0) >= min_height and
            (image_info.get('file_size_bytes') or 0) >= min_file_size
        )
        fallback_used = final_url == original_url and self._url_looks_shrunk(original_url)
        higher_quality_confirmed = comparison['is_higher_quality'] and final_url != original_url

        fallback_ok = fallback_used and config.ENABLE_HIGH_QUALITY_FALLBACK
        passed = filename_has_prefix and (
            (thresholds_met and (higher_quality_confirmed or not self._url_looks_shrunk(original_url))) or
            fallback_ok
        )

        return {
            'passed': passed,
            'thresholds_met': thresholds_met,
            'filename_has_prefix': filename_has_prefix,
            'higher_quality_confirmed': higher_quality_confirmed,
            'fallback_used': fallback_used,
            'is_profile_photo': is_profile_photo,
            'image_info': image_info,
            'comparison': comparison,
        }

    def _download_to_path(
        self,
        url: str,
        save_path: str,
        referer: Optional[str] = None,
    ) -> requests.Response:
        """Download a URL to disk and return the response object."""
        headers = {'Referer': referer or 'https://www.lemon8-app.com/'}
        response = self.session.get(url, headers=headers, stream=True, timeout=30)
        response.raise_for_status()

        content_length = response.headers.get('content-length')
        if content_length:
            size_mb = int(content_length) / (1024 * 1024)
            if size_mb > config.MAX_MEDIA_SIZE_MB:
                raise ValueError(f"File too large ({size_mb:.1f}MB)")

        with open(save_path, 'wb') as output_file:
            for chunk in response.iter_content(chunk_size=config.CHUNK_SIZE):
                if chunk:
                    output_file.write(chunk)

        return response
    
    def is_already_downloaded(self, url: str) -> bool:
        """Check if URL has a completed download record (fast in-memory cache, then DB)."""
        url_hash = self._get_url_hash(url)
        if url_hash in self.downloaded_media or url in self.downloaded_media:
            return True
        # Another process may have completed it since startup — check DB
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT 1 FROM downloaded_media WHERE url_hash = ? AND status = 'completed'",
            (url_hash,)
        )
        if cursor.fetchone():
            self.downloaded_media.add(url_hash)
            return True
        return False

    def _claim_url(self, url: str) -> bool:
        """Atomically reserve a URL for download. Returns True only if this process won.

        INSERT succeeds → we got the claim (status='pending').
        INSERT conflicts + existing status='failed' → we reset it and got the claim.
        INSERT conflicts + existing status='pending'/'completed' → another process has it.
        """
        url_hash = self._get_url_hash(url)
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO downloaded_media (url_hash, downloaded_at, status)
            VALUES (?, ?, 'pending')
            ON CONFLICT(url_hash) DO UPDATE SET
                downloaded_at = excluded.downloaded_at,
                status = 'pending'
            WHERE status = 'failed'
        ''', (url_hash, datetime.now().isoformat()))
        self.conn.commit()
        return cursor.rowcount > 0

    def _mark_download_failed(self, url: str) -> None:
        """Release a pending claim as failed so the next run can retry."""
        url_hash = self._get_url_hash(url)
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE downloaded_media SET status = 'failed' WHERE url_hash = ?",
                (url_hash,)
            )
            self.conn.commit()
        except Exception:
            pass

    def mark_as_downloaded(self, url: str):
        """Mark a URL as successfully downloaded (status='completed')."""
        url_hash = self._get_url_hash(url)
        now = datetime.now().isoformat()
        self.downloaded_media.add(url_hash)

        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO downloaded_media (url_hash, downloaded_at, status)
            VALUES (?, ?, 'completed')
            ON CONFLICT(url_hash) DO UPDATE SET
                downloaded_at = excluded.downloaded_at,
                status = 'completed'
        ''', (url_hash, now))
        self.conn.commit()

        if self.auto_save:
            self.save()
    
    def save(self):
        """Force save downloaded media history to JSON backup"""
        self._save_downloaded_media()
    
    def download_media(self, url: str, scrape_type: str, identifier: str, 
                      custom_filename: Optional[str] = None,
                      referer: Optional[str] = None,
                      filename_prefix: Optional[str] = None,
                      is_profile_photo: bool = False) -> Optional[str]:
        """
        Download media from URL with deduplication
        
        Args:
            url: Media URL to download
            scrape_type: 'user', 'feed', or 'tag'
            identifier: Username, 'foryou', or tag_id
            custom_filename: Optional custom filename
            referer: Optional Referer header for the download
            
        Returns:
            Path to downloaded file or None if failed/skipped
        """
        # Atomic claim: only one process can win this INSERT — no wasted API calls
        if not self._claim_url(url):
            if self.is_already_downloaded(url):
                print(f"⏭️ Media already downloaded, skipping: {url[:50]}...")
            else:
                print(f"⏭️ Download claimed by another process, skipping: {url[:50]}...")
            return None

        try:
            downloads_dir = self._get_downloads_dir()

            if custom_filename:
                base_filename = custom_filename
            elif 'video' in url.lower() or '.mp4' in url.lower():
                base_filename = self._get_filename_from_url(url, 'mp4')
            elif self._is_image_url(url):
                base_filename = self._get_filename_from_url(url, 'jpg')
                # Force save to .jpg to avoid keeping webp
                base_name, _ = os.path.splitext(base_filename)
                base_filename = base_name + '.jpg'
            else:
                base_filename = self._get_filename_from_url(url, 'mp4')

            prefix_name = filename_prefix or identifier
            inferred_profile_photo = is_profile_photo or (
                scrape_type == 'user' and self._is_profile_photo_url(url)
            )

            if self._is_image_url(url):
                base_filename = self._build_prefixed_filename(prefix_name, base_filename)

            target_subfolder = self._normalize_username(prefix_name)

            media_dir = os.path.dirname(
                config.get_media_save_path(
                    downloads_dir,
                    scrape_type,
                    identifier,
                    "placeholder.tmp",
                    subfolder_override=target_subfolder,
                )
            )
            filename = self._ensure_unique_filename(media_dir, base_filename)
            save_path = config.get_media_save_path(
                downloads_dir,
                scrape_type,
                identifier,
                filename,
                subfolder_override=target_subfolder,
            )
            tmp_save_path = save_path + ".tmp"

            if os.path.exists(save_path):
                print(f"⏭️ File already exists, marking as downloaded: {filename}")
                self.mark_as_downloaded(url)
                return save_path

            print(f"⬇️ Downloading: {filename}")

            candidate_urls = [url]
            if config.IMAGE_ENHANCEMENT_ENABLED and self._is_image_url(url):
                candidate_urls = self._enhance_image_url(url)

            last_error: Optional[Exception] = None
            final_url = url

            for candidate_url in candidate_urls:
                try:
                    final_url = candidate_url
                    response = self._download_to_path(candidate_url, tmp_save_path, referer=referer)
                    if not os.path.exists(tmp_save_path) or os.path.getsize(tmp_save_path) <= 0:
                        raise ValueError("Downloaded file is empty")

                    if self._is_image_url(url):
                        verification = self._verify_image_download(
                            tmp_save_path,
                            url,
                            candidate_url,
                            prefix_name,
                            is_profile_photo=inferred_profile_photo,
                        )
                        self._log_event(
                            'info',
                            "Image verification completed",
                            original_url=url,
                            final_url=candidate_url,
                            save_path=tmp_save_path,
                            verification=verification,
                        )
                        if not verification['passed']:
                            raise ValueError(f"Image verification failed: {verification}")

                        # Process image (convert to JPG and handle format)
                        if HAS_PILLOW:
                            try:
                                with Image.open(tmp_save_path) as img:
                                    if img.mode in ('RGBA', 'LA', 'P'):
                                        bg = Image.new('RGB', img.size, (255, 255, 255))
                                        if img.mode == 'P':
                                            img = img.convert('RGBA')
                                        bg.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                                        bg.save(save_path, 'JPEG', quality=100)
                                    else:
                                        img.convert('RGB').save(save_path, 'JPEG', quality=100)
                                if os.path.exists(tmp_save_path):
                                    os.remove(tmp_save_path)
                            except Exception as e:
                                print(f"⚠️ Error converting image, saving as is: {e}")
                                os.rename(tmp_save_path, save_path)
                        else:
                            os.rename(tmp_save_path, save_path)
                    else:
                        # Non-image files just rename
                        os.rename(tmp_save_path, save_path)

                    self.mark_as_downloaded(url)
                    if candidate_url != url:
                        self.mark_as_downloaded(candidate_url)

                    print(f"✅ Downloaded: {filename} ({os.path.getsize(save_path) / 1024:.1f} KB)")
                    _interruptible_sleep(config.MIN_DELAY)
                    return save_path
                except (requests.exceptions.RequestException, ValueError) as exc:
                    last_error = exc
                    if os.path.exists(tmp_save_path):
                        os.remove(tmp_save_path)
                    if os.path.exists(save_path):
                        os.remove(save_path)
                    self._log_event(
                        'warning',
                        "Download attempt failed",
                        original_url=url,
                        candidate_url=candidate_url,
                        save_path=save_path,
                        error=str(exc),
                    )
                    continue

            if last_error:
                self._mark_download_failed(url)
                raise last_error
            return None

        except requests.exceptions.RequestException as e:
            print(f"❌ Network error downloading {url[:50]}...: {e}")
            self._log_event('error', "Network error downloading media", url=url, error=str(e))
            self._mark_download_failed(url)
            return None
        except Exception as e:
            print(f"❌ Error downloading {url[:50]}...: {e}")
            self._log_event('error', "Unexpected download error", url=url, error=str(e))
            self._mark_download_failed(url)
            return None
    
    def download_multiple_media(self, media_urls: List[Any], scrape_type: str, 
                               identifier: str, referer: Optional[str] = None) -> Dict[str, Optional[str]]:
        """
        Download multiple media URLs
        
        Args:
            media_urls: List of media URLs
            scrape_type: 'user', 'feed', or 'tag'
            identifier: Username, 'foryou', or tag_id
            referer: Optional Referer header for the downloads
            
        Returns:
            Dict mapping URLs to downloaded file paths (or None if failed/skipped)
        """
        results = {}
        total = len(media_urls)
        successful = 0
        skipped_existing = 0
        skipped_profile_disabled = 0
        failed = 0
        
        print(f"\n🎬 Downloading {total} media files...")
        
        for i, url in enumerate(media_urls, 1):
            print(f"[{i}/{total}] ", end="")
            media_url = url
            filename_prefix = identifier
            is_profile_photo = False

            if isinstance(url, dict):
                media_url = url.get('url')
                filename_prefix = url.get('username') or identifier
                is_profile_photo = bool(url.get('is_profile_photo'))

            if not media_url:
                continue

            if is_profile_photo and not config.PROFILE_PHOTO_DOWNLOAD_ENABLED:
                results[media_url] = None
                skipped_profile_disabled += 1
                continue

            was_already_downloaded = self.is_already_downloaded(media_url)

            result = self.download_media(
                media_url,
                scrape_type,
                identifier,
                referer=referer,
                filename_prefix=filename_prefix,
                is_profile_photo=is_profile_photo,
            )
            results[media_url] = result

            if result is not None:
                successful += 1
            elif was_already_downloaded:
                skipped_existing += 1
            else:
                failed += 1
            
            # Progress indication
            if i % 10 == 0:
                print(f"📊 Progress: {i}/{total} completed")
            
            # Small delay to avoid aggressive behavior
            if i < total:
                _interruptible_sleep(config.MIN_DELAY)
        
        # Summary
        print(f"\n📈 Download Summary:")
        print(f"✅ Downloaded: {successful}")
        print(f"⏭️ Skipped (already downloaded): {skipped_existing}")
        if skipped_profile_disabled > 0:
            print(f"🚫 Skipped (profile photos disabled): {skipped_profile_disabled}")
        if failed > 0:
            print(f"❌ Failed (will retry next run): {failed}")
        print(f"📁 Total tracked: {len(self.downloaded_media)}")
        
        return results
    
    def get_stats(self) -> Dict:
        """Get download statistics"""
        return {
            'total_downloaded': len(self.downloaded_media),
            'tracking_file': config.DOWNLOADED_MEDIA_FILE,
            'tracking_file_exists': os.path.exists(config.DOWNLOADED_MEDIA_FILE)
        }
    
    def clear_download_history(self):
        """Clear download history (for testing/reset)"""
        self.downloaded_media.clear()
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM downloaded_media")
        self.conn.commit()
        self._save_downloaded_media()
        print("🗑️ Download history cleared")
