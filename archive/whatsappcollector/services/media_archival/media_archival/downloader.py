from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import settings
from .database import database
from .observability import get_logger, media_downloads_total

logger = get_logger(__name__)

_MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "audio/ogg": ".ogg",
    "audio/ogg; codecs=opus": ".opus",
    "application/pdf": ".pdf",
}


def sanitize_jid(jid: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", jid or "unknown")


def mime_to_extension(mime_type: str | None) -> str:
    if not mime_type:
        return ".bin"
    mime_type = mime_type.lower().strip()
    if mime_type in _MIME_TO_EXT:
        return _MIME_TO_EXT[mime_type]
    guessed = mimetypes.guess_extension(mime_type.split(";", 1)[0].strip())
    return guessed or ".bin"


def build_paths(storage_root: Path, chat_jid: str, file_unique_id: str, message_id: str, mime_type: str | None) -> tuple[Path, Path]:
    ext = mime_to_extension(mime_type)
    by_id = storage_root / "by_id" / f"{file_unique_id}{ext}"
    by_message = storage_root / "by_message" / sanitize_jid(chat_jid) / f"{message_id}{ext}"
    return by_id, by_message


def extract_media_metadata(raw_payload: Any) -> dict[str, Any]:
    if isinstance(raw_payload, str):
        try:
            raw_payload = json.loads(raw_payload)
        except Exception:
            return {}

    if not isinstance(raw_payload, dict):
        return {}

    candidates: list[dict[str, Any]] = []

    def walk(value: Any):
        if isinstance(value, dict):
            keys = {k.lower() for k in value.keys()}
            if any(k in keys for k in ("mediatype", "mimetype", "filesha256", "fileuniqueid", "directpath", "url", "mediakey")):
                candidates.append(value)
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(raw_payload)
    if not candidates:
        return {}

    best = candidates[0]
    for candidate in candidates:
        if candidate.get("mimetype") or candidate.get("mime_type"):
            best = candidate
            break

    return best


def row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


@dataclass
class DownloadResult:
    success: bool
    file_unique_id: str | None = None
    mime_type: str | None = None
    by_id_path: str | None = None
    by_message_path: str | None = None
    sha256: str | None = None
    error: str | None = None


class MediaDownloader:
    def __init__(self) -> None:
        self.storage_root = settings.storage_path
        self.bridge_url = settings.MEDIA_BRIDGE_URL.rstrip("/")
        self.secret = settings.MEDIA_BRIDGE_SECRET.encode("utf-8") if settings.MEDIA_BRIDGE_SECRET else b""

    def _ensure_dirs(self) -> None:
        (self.storage_root / "by_id").mkdir(parents=True, exist_ok=True)
        (self.storage_root / "by_message").mkdir(parents=True, exist_ok=True)

    def _signature(self, payload: bytes) -> str:
        if not self.secret:
            return ""
        return hmac.new(self.secret, payload, hashlib.sha256).hexdigest()

    async def _write_link(self, source: Path, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() or dest.is_symlink():
            try:
                dest.unlink()
            except FileNotFoundError:
                pass

        try:
            os.symlink(source, dest)
        except Exception:
            shutil.copy2(source, dest)

    async def _download_from_bridge(self, message: dict[str, Any], by_id_path: Path, base_url: str | None = None) -> tuple[str | None, str | None]:
        url = f"{(base_url or self.bridge_url).rstrip('/')}/media/decrypt"
        payload = json.dumps(message).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        sig = self._signature(payload)
        if sig:
            headers["X-Signature"] = sig

        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream("POST", url, content=payload, headers=headers) as response:
                response.raise_for_status()

                header_sha = response.headers.get("X-Media-SHA256")
                header_mime = response.headers.get("X-Media-MimeType")
                local_path_hint = response.headers.get("X-Local-Path")
                mime_type = header_mime or message.get("media_metadata", {}).get("mimetype")

                if local_path_hint and await asyncio.to_thread(Path(local_path_hint).exists):
                    await asyncio.to_thread(shutil.copy2, local_path_hint, by_id_path)
                    return header_sha or by_id_path.stem, mime_type

                hasher = hashlib.sha256()
                tmp_path = by_id_path.with_suffix(by_id_path.suffix + ".part")
                tmp_path.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp_path, "wb") as fh:
                    async for chunk in response.aiter_bytes():
                        fh.write(chunk)
                        hasher.update(chunk)

                if by_id_path.exists():
                    tmp_path.unlink(missing_ok=True)
                else:
                    tmp_path.replace(by_id_path)

                return header_sha or hasher.hexdigest(), mime_type

    async def download_message(self, row: Any) -> DownloadResult:
        self._ensure_dirs()

        raw_payload = row_value(row, "raw_payload")
        message_id = row_value(row, "message_id")
        chat_jid = row_value(row, "chat_jid")
        raw_message_id = int(row_value(row, "raw_message_id"))

        media = extract_media_metadata(raw_payload)
        mime_type = media.get("mimetype") or media.get("mime_type") or row_value(row, "message_type")
        file_unique_id = re.sub(
            r"[^a-zA-Z0-9_.-]",
            "_",
            str(
                media.get("file_unique_id")
                or media.get("fileSha256")
                or media.get("file_sha256")
                or media.get("mediaKey")
                or media.get("media_key")
                or media.get("sha256")
                or f"{message_id}-{raw_message_id}"
            ),
        )

        by_id_path, by_message_path = build_paths(self.storage_root, chat_jid, file_unique_id, message_id, mime_type)

        existing = None
        if by_id_path.exists():
            existing = by_id_path
        else:
            for candidate in (self.storage_root / "by_id").glob(f"{file_unique_id}.*"):
                existing = candidate
                break

        if existing and existing.exists():
            await self._write_link(existing, by_message_path)
            await database.upsert_media_file(
                raw_message_id=raw_message_id,
                message_id=message_id,
                chat_jid=chat_jid,
                file_unique_id=file_unique_id,
                mime_type=mime_type,
                file_size_bytes=existing.stat().st_size,
                by_id_path=str(existing),
                by_message_path=str(by_message_path),
                sha256=file_unique_id,
                download_status="complete",
                downloaded_at=datetime_now(),
                expiry_at=_parse_expiry(row, media),
            )
            media_downloads_total.labels(status="reused", mime_type=str(mime_type or "unknown")).inc()
            return DownloadResult(True, file_unique_id, mime_type, str(existing), str(by_message_path), file_unique_id)

        try:
            bridge_message = dict(row) if isinstance(row, dict) else {key: row_value(row, key) for key in row.keys()}
            # TS bridge expects rawPayload = the WAMessage envelope.
            # The raw_payload column stores the full normalized message; the
            # actual WAMessage envelope is nested one level deeper at ["raw_payload"].
            if "rawPayload" not in bridge_message:
                outer = bridge_message.get("raw_payload") or {}
                if isinstance(outer, str):
                    try:
                        outer = json.loads(outer)
                    except Exception:
                        outer = {}
                bridge_message["rawPayload"] = outer.get("raw_payload") if isinstance(outer, dict) else None
            bridge_urls = list(settings.wa_clients.values())
            if not bridge_urls:
                bridge_urls = [self.bridge_url]

            last_exc: Exception | None = None
            sha256: str | None = None
            downloaded_mime: str | None = None

            for idx, base_url in enumerate(bridge_urls):
                try:
                    sha256, downloaded_mime = await self._download_from_bridge(bridge_message, by_id_path, base_url=base_url)
                    logger.debug("media_download_success", bridge_idx=idx, url=base_url)
                    last_exc = None
                    break
                except Exception as exc:
                    logger.warning("media_download_bridge_failed", bridge_idx=idx, url=base_url, error=str(exc))
                    last_exc = exc

            if last_exc is not None:
                logger.error("media_download_all_bridges_exhausted", bridge_count=len(bridge_urls))
                raise last_exc

            mime_type = downloaded_mime or mime_type
            await self._write_link(by_id_path, by_message_path)
            file_size = by_id_path.stat().st_size if by_id_path.exists() else None
            await database.upsert_media_file(
                raw_message_id=raw_message_id,
                message_id=message_id,
                chat_jid=chat_jid,
                file_unique_id=file_unique_id,
                mime_type=mime_type,
                file_size_bytes=file_size,
                by_id_path=str(by_id_path),
                by_message_path=str(by_message_path),
                sha256=sha256 or file_unique_id,
                download_status="complete",
                downloaded_at=datetime_now(),
                expiry_at=_parse_expiry(row, media),
            )
            media_downloads_total.labels(status="downloaded", mime_type=str(mime_type or "unknown")).inc()
            return DownloadResult(True, file_unique_id, mime_type, str(by_id_path), str(by_message_path), sha256 or file_unique_id)
        except Exception as exc:
            await database.mark_download_failure(
                message_id=message_id,
                chat_jid=chat_jid,
                error_message=str(exc),
                next_retry_at=datetime_now_plus(minutes=30),
                is_permanent=False,
            )
            media_downloads_total.labels(status="failed", mime_type=str(mime_type or "unknown")).inc()
            logger.warning("media_download_failed", message_id=message_id, chat_jid=chat_jid, error=str(exc))
            return DownloadResult(False, file_unique_id, mime_type, error=str(exc))


def datetime_now():
    from datetime import datetime

    return datetime.utcnow()


def datetime_now_plus(minutes: int):
    from datetime import timedelta

    return datetime_now() + timedelta(minutes=minutes)


def _parse_expiry(row: Any, media: dict[str, Any]):
    expiry = None
    raw_expiry = row_value(row, "expiry_at") or row_value(row, "expires_at")

    if not raw_expiry:
        raw_expiry = media.get("expires_at") or media.get("expiry_at")

    if isinstance(raw_expiry, str):
        from datetime import datetime, timezone

        try:
            dt = datetime.fromisoformat(raw_expiry)
            # Strip timezone to match TIMESTAMP columns
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            expiry = dt
        except Exception:
            expiry = None
    return expiry


media_downloader = MediaDownloader()
