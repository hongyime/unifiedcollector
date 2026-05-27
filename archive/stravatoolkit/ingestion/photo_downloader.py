from __future__ import annotations

import argparse
import hashlib
import io
import json
import mimetypes
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from ingestion.logging_config import get_logger
from ingestion.tools.diagnostics.runtime import (
    bootstrap_requests_dependency_warnings,
    emit_requests_dependency_health_once,
)

bootstrap_requests_dependency_warnings()
import requests

try:
    import imagehash
    from PIL import Image
    _PHASH_AVAILABLE = True
except ImportError:
    _PHASH_AVAILABLE = False

from ingestion import db
from ingestion.config import ensure_runtime_dirs, load_settings
from ingestion.session import StravaSession

logger = get_logger(__name__)


SAFE_SLUG_RE = re.compile(r"[^a-z0-9]+")
_JSON_UNICODE_RE = re.compile(r'\\u([0-9a-fA-F]{4})')


def _normalize_url(url: str) -> str:
    """Decode \\uXXXX escapes in URLs stored from the old parser bug (e.g. \\u0026 → &)."""
    return _JSON_UNICODE_RE.sub(lambda m: chr(int(m.group(1), 16)), url)


@dataclass(slots=True)
class PhotoDownloadSummary:
    mode: str
    output_dir: str
    profile_checked: int = 0
    profile_changed: int = 0
    activity_saved: int = 0
    activity_skipped_existing: int = 0
    failures: int = 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download tracked profile and activity photos.")
    parser.add_argument("--mode", choices=["profiles", "activities", "all"], default="all")
    parser.add_argument("--date", help="Optional YYYY-MM-DD filter for activity photos.")
    parser.add_argument("--output-dir", help="Where downloaded files should be saved.")
    parser.add_argument(
        "--auth-mode",
        choices=["playwright", "cookiestxt"],
        default="cookiestxt",
        help="How to acquire the Strava session cookie.",
    )
    parser.add_argument("--cookies-file", help="Path to Netscape-format cookies.txt file.")
    parser.add_argument("--cookie-value", help="Explicit Strava session cookie value.")
    return parser


def slugify_name(name: str, athlete_id: int) -> str:
    normalized = name.encode("ascii", "ignore").decode("ascii").lower()
    slug = SAFE_SLUG_RE.sub("_", normalized).strip("_")
    if not slug:
        slug = f"athlete_{athlete_id}"
    return f"{slug}_{athlete_id}"


def guess_extension(url: str, content_type: str | None) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov"}:
        return suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
    return ".jpg"


def md5_hex(payload: bytes) -> str:
    return hashlib.md5(payload).hexdigest()


class PhotoDownloader:
    def __init__(self, conn, session, output_dir: Path):
        self.conn = conn
        self.session = session
        self.output_dir = output_dir
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": self.session.settings.user_agent})

    def run(self, mode: str, date_string: str | None = None) -> PhotoDownloadSummary:
        summary = PhotoDownloadSummary(mode=mode, output_dir=str(self.output_dir))
        self.output_dir.mkdir(parents=True, exist_ok=True)

        if mode in {"profiles", "all"}:
            self._download_profile_photos(summary)
        if mode in {"activities", "all"}:
            self._download_activity_photos(summary, date_string)
        return summary

    def _download_profile_photos(self, summary: PhotoDownloadSummary) -> None:
        import os
        for target in db.list_profile_photo_targets(self.conn):
            summary.profile_checked += 1
            athlete_id = target["athlete_id"]
            athlete_slug = slugify_name(target["name"], athlete_id)
            logger.info(f"Checking profile photo for {athlete_slug}.")

            # Stage 1 — URL check (fast, zero download cost)
            cursor = self.conn.execute(
                "SELECT id, source_url, photo_phash FROM athlete_photo_history"
                " WHERE athlete_id=? ORDER BY captured_at DESC, id DESC LIMIT 1",
                (athlete_id,),
            )
            latest_row = cursor.fetchone()

            if latest_row and latest_row["source_url"] == target["avatar_url"]:
                db.touch_profile_photo_history(self.conn, int(latest_row["id"]))
                logger.info("  unchanged (URL match)")
                continue

            # Stage 2 — download and pHash comparison (only when URL changed)
            try:
                response = self._download_response(target["avatar_url"])
                response.raise_for_status()
            except Exception as e:
                logger.error(f"Download failed for {target['avatar_url']}: {e}")
                summary.failures += 1
                continue

            payload = response.content

            if _PHASH_AVAILABLE:
                try:
                    img = Image.open(io.BytesIO(payload))
                    new_phash = str(imagehash.phash(img))
                except Exception:
                    new_phash = None

                if new_phash and latest_row and latest_row["photo_phash"]:
                    distance = imagehash.hex_to_hash(new_phash) - imagehash.hex_to_hash(latest_row["photo_phash"])
                    if distance <= 10:
                        # CDN rotation — URL changed but image is the same
                        self.conn.execute(
                            "UPDATE athlete_photo_history SET source_url=? WHERE id=?",
                            (target["avatar_url"], int(latest_row["id"])),
                        )
                        logger.info("  CDN rotation only (pHash match), URL updated")
                        continue
            else:
                new_phash = None

            # Genuine change or first-time — store blob in DB
            db_path = self.conn.execute("PRAGMA database_list").fetchone()[2]
            db_size_mb = os.path.getsize(db_path) / (1024 * 1024)
            max_mb = int(os.environ.get("PROFILE_PHOTO_BLOB_MAX_SIZE_MB", 5000))
            from ingestion.config import now_utc_iso
            now = now_utc_iso()

            photo_md5 = md5_hex(payload)
            if db_size_mb > max_mb:
                logger.warning(f"DB size {db_size_mb:.1f}MB exceeds limit {max_mb}MB. Storing photo without blob.")
                self.conn.execute(
                    "INSERT OR IGNORE INTO athlete_photo_history"
                    " (athlete_id, athlete_name, source_url, local_path, md5_hash, photo_phash, captured_at, last_checked_at)"
                    " VALUES (?, ?, ?, '', ?, ?, ?, ?)",
                    (athlete_id, target["name"], target["avatar_url"], photo_md5, new_phash, now, now),
                )
            else:
                self.conn.execute(
                    "INSERT OR IGNORE INTO athlete_photo_history"
                    " (athlete_id, athlete_name, source_url, local_path, md5_hash, photo_phash, photo_blob, captured_at, last_checked_at)"
                    " VALUES (?, ?, ?, '', ?, ?, ?, ?, ?)",
                    (athlete_id, target["name"], target["avatar_url"], photo_md5, new_phash, payload, now, now),
                )

            summary.profile_changed += 1
            logger.info(f"  stored new photo (pHash: {new_phash})")

    def _download_activity_photos(self, summary: PhotoDownloadSummary, date_string: str | None) -> None:
        for photo in db.list_activity_photo_targets(self.conn, date_string=date_string):
            athlete_slug = slugify_name(photo["athlete_name"], int(photo["athlete_id"]))
            extension = guess_extension(
                photo.get("source_url_large") or photo.get("source_url_thumbnail") or "",
                None,
            )
            athlete_dir = self.output_dir / athlete_slug
            athlete_dir.mkdir(parents=True, exist_ok=True)
            media_prefix = "video" if int(photo.get("media_type") or 1) == 2 else "photo"
            activity_stamp = self._activity_stamp(photo)
            existing_path = Path(photo["local_path"]) if photo.get("local_path") else None
            if existing_path and existing_path.exists():
                target_path = existing_path
            else:
                target_path = self._unique_path(athlete_dir, f"{media_prefix}_{athlete_slug}_{activity_stamp}", extension)
            logger.info(f"Downloading {media_prefix} for {athlete_slug} at {activity_stamp}.")

            if target_path.exists():
                digest = photo.get("md5_hash") or md5_hex(target_path.read_bytes())
                db.mark_activity_photo_downloaded(self.conn, photo["photo_id"], str(target_path), digest)
                summary.activity_skipped_existing += 1
                logger.info("  already saved")
                continue

            source_url = photo.get("source_url_large") or photo.get("source_url_thumbnail")
            if not source_url:
                summary.failures += 1
                continue

            try:
                response = self._download_response(source_url)
                response.raise_for_status()
            except Exception as e:
                logger.error(f"Download failed for {source_url}: {e}")
                summary.failures += 1
                continue

            if response.url and guess_extension(response.url, response.headers.get("Content-Type")) != extension:
                target_path = target_path.with_suffix(guess_extension(response.url, response.headers.get("Content-Type")))
            hasher = hashlib.md5()
            with open(target_path, "wb") as fh:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        fh.write(chunk)
                        hasher.update(chunk)
            db.mark_activity_photo_downloaded(self.conn, photo["photo_id"], str(target_path), hasher.hexdigest())
            summary.activity_saved += 1
            logger.info(f"  saved {target_path.name}")

    def _download_response(self, url: str) -> requests.Response:
        url = _normalize_url(url)
        hostname = urlparse(url).hostname or ""
        headers = {}
        if hostname.endswith("strava.com"):
            headers["Cookie"] = f"_strava4_session={self.session.cookie_value}"
        return self.http.get(
            url,
            headers=headers,
            timeout=self.session.settings.request_timeout_seconds,
            allow_redirects=True,
        )

    def _activity_stamp(self, photo: dict) -> str:
        raw_value = photo.get("start_date_local") or photo.get("start_date_utc")
        if not raw_value:
            return f"activity_{photo['activity_id']}"
        dt = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00")).astimezone(self.session.settings.timezone)
        return dt.strftime("%Y%m%d_%H%M%S")

    def _unique_path(self, athlete_dir: Path, stem: str, extension: str) -> Path:
        candidate = athlete_dir / f"{stem}{extension}"
        if not candidate.exists():
            return candidate

        suffix = 2
        while True:
            candidate = athlete_dir / f"{stem}_{suffix}{extension}"
            if not candidate.exists():
                return candidate
            suffix += 1


def main() -> None:
    args = build_parser().parse_args()
    settings = load_settings()
    emit_requests_dependency_health_once()
    ensure_runtime_dirs(settings)
    output_dir = Path(args.output_dir) if args.output_dir else settings.downloads_dir

    db.init_db(settings.db_path)
    session = StravaSession.from_sources(
        settings,
        auth_mode=args.auth_mode,
        cookie_value=args.cookie_value,
        cookies_file=args.cookies_file,
    )
    conn = db.connect(settings.db_path)
    try:
        summary = PhotoDownloader(conn, session, output_dir).run(args.mode, args.date)
    finally:
        conn.close()

    logger.info(json.dumps(asdict(summary), indent=2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Photo download stopped safely. Completed files stay saved, and tracked history remains consistent.")
        raise SystemExit(130)
