import hashlib
import logging
from collections import OrderedDict
from pathlib import Path

import aiohttp

from src.core.vault import VAULT_ROOT, AtomicArtifactResult, write_atomic_artifact

logger = logging.getLogger(__name__)

HAMMING_THRESHOLD = 10
_CACHE_MAX_ENTRIES = 4096  # bounded LRU; ~512 KB at 128 bytes/entry


def _hamming_distance(hash1, hash2) -> int:
    return hash1 - hash2


class _LRU(OrderedDict):
    """Tiny size-bounded LRU. Evicts the least recently used on overflow."""

    def __init__(self, maxsize: int):
        super().__init__()
        self._maxsize = maxsize

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value

    def get(self, key, default=None):  # type: ignore[override]
        if key in self:
            return self.__getitem__(key)
        return default

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self._maxsize:
            self.popitem(last=False)


class ProfilePhotoTracker:
    """Two-stage profile photo change detection.

    Stage 1: Compare URL (free — no download needed).
    Stage 2: Download and compare perceptual hash (pHash).
    Hamming distance > HAMMING_THRESHOLD = genuine change; <= threshold = CDN rotation.

    Requires the ``imagehash`` and ``Pillow`` packages.  Import is deferred so
    collectors that don't use this class pay no startup cost.
    """

    def __init__(self, pool=None, blob_max_size_mb: int = 5000):
        self.pool = pool
        self.blob_max_size_mb = blob_max_size_mb
        # Bounded caches — unbounded growth was a slow leak in long-running
        # workers tracking many entities.
        self._url_cache: _LRU = _LRU(_CACHE_MAX_ENTRIES)
        self._hash_cache: _LRU = _LRU(_CACHE_MAX_ENTRIES)
        self._last_artifact: AtomicArtifactResult | None = None

    def set_pool(self, pool):
        self.pool = pool

    def last_artifact_metadata(self) -> dict | None:
        artifact = self._last_artifact
        if artifact is None:
            return None
        sidecar = artifact.sidecar
        return {
            "ok": artifact.ok,
            "partial": artifact.partial,
            "path": artifact.relative_path,
            "blob_path": artifact.blob_relative_path,
            "sha256": artifact.sha256,
            "file_size": artifact.file_size,
            "sidecar_ok": sidecar.ok if sidecar else None,
            "sidecar_path": sidecar.relative_path if sidecar else None,
            "duplicate_blob": artifact.duplicate_blob,
            "error": artifact.error,
        }

    async def check_and_download(
        self,
        url: str,
        entity_id: str,
        source: str,
        save_dir: Path,
        session: aiohttp.ClientSession | None = None,
    ) -> tuple[bool, Path | None]:
        """Check if a profile photo changed and download if so.

        Returns (changed, path) where changed=True means a genuine new photo
        was saved to disk.
        """
        self._last_artifact = None
        cache_key = f"{source}:{entity_id}"

        old_url = self._url_cache.get(cache_key)
        if old_url and old_url == url:
            return False, None

        if not old_url:
            old_url = await self._load_url_from_db(source, entity_id)

        self._url_cache[cache_key] = url

        if old_url == url:
            return False, None

        own_session = session is None
        if own_session:
            session = aiohttp.ClientSession()
        try:
            data = await self._download(session, url)
        finally:
            if own_session:
                await session.close()

        if data is None:
            return False, None

        new_phash = self._compute_phash(data)
        if new_phash is None:
            path = self._save(data, entity_id, source, save_dir, url=url)
            await self._store_metadata(source, entity_id, url, None)
            return True, path

        old_phash = self._hash_cache.get(cache_key)
        if not old_phash:
            old_phash = await self._load_phash_from_db(source, entity_id)

        self._hash_cache[cache_key] = str(new_phash)

        if old_phash:
            try:
                from imagehash import hex_to_hash
                dist = _hamming_distance(new_phash, hex_to_hash(old_phash))
            except Exception:
                dist = HAMMING_THRESHOLD + 1

            if dist <= HAMMING_THRESHOLD:
                logger.debug(
                    "CDN rotation for %s/%s (hamming=%d, threshold=%d)",
                    source, entity_id, dist, HAMMING_THRESHOLD,
                )
                return False, None

        path = self._save(data, entity_id, source, save_dir, url=url)
        await self._store_metadata(source, entity_id, url, str(new_phash))
        return True, path

    def _compute_phash(self, data: bytes):
        try:
            import io
            from PIL import Image
            import imagehash
            img = Image.open(io.BytesIO(data))
            return imagehash.phash(img)
        except Exception as e:
            logger.debug("pHash computation failed: %s", e)
            return None

    async def _download(self, session: aiohttp.ClientSession, url: str) -> bytes | None:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    return None
                return await resp.read()
        except Exception as e:
            logger.debug("Profile photo download failed: %s", e)
            return None

    def _save(self, data: bytes, entity_id: str, source: str, save_dir: Path, *, url: str | None = None) -> Path:
        # SHA-256 of content prevents collisions and is non-cryptographic-use safe.
        digest = hashlib.sha256(data).hexdigest()
        short_digest = digest[:16]
        ext = "jpg"
        if data[:4] == b"\x89PNG":
            ext = "png"
        elif data[:4] == b"RIFF":
            ext = "webp"
        filename = f"{source}_{entity_id}_profile_{short_digest}.{ext}"
        artifact = write_atomic_artifact(
            source=source,
            artifact_id=f"profile_photo/{entity_id}/{short_digest}",
            artifact_kind="media_blob",
            data=data,
            extension=ext,
            expected_sha256=digest,
            metadata={
                "entity_id": str(entity_id),
                "entity_name": str(entity_id),
                "content_type": "profile_photo",
                "content_id": f"profile_{entity_id}",
                "filename": filename,
                "source_url": url,
                "legacy_save_dir": str(save_dir),
                "rebuild_target_tables": ["media_items"],
            },
            root=VAULT_ROOT,
        )
        self._last_artifact = artifact
        if artifact.path is None:
            raise RuntimeError(f"profile photo artifact write failed: {artifact.error}")
        if not artifact.ok and not artifact.partial:
            raise RuntimeError(f"profile photo artifact write failed: {artifact.error}")
        return artifact.path

    async def _store_metadata(self, source: str, entity_id: str, url: str, phash: str | None):
        if not self.pool:
            return
        try:
            import json as _json
            payload = _json.dumps({"url": url, "phash": phash or ""})
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE media_items
                    SET metadata = jsonb_set(
                        COALESCE(metadata, '{}'::jsonb),
                        '{profile_photo}',
                        $1::jsonb
                    )
                    WHERE source = $2 AND entity_id = $3
                      AND content_type = 'profile_photo'
                    """,
                    payload,
                    source,
                    entity_id,
                )
        except Exception as e:
            logger.debug("Failed to store profile photo metadata: %s", e)

    async def _load_url_from_db(self, source: str, entity_id: str) -> str | None:
        if not self.pool:
            return None
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchval(
                    """
                    SELECT metadata->'profile_photo'->>'url'
                    FROM media_items
                    WHERE source = $1 AND entity_id = $2
                      AND content_type = 'profile_photo'
                    ORDER BY collected_at DESC LIMIT 1
                    """,
                    source, entity_id,
                )
                return row
        except Exception:
            return None

    async def _load_phash_from_db(self, source: str, entity_id: str) -> str | None:
        if not self.pool:
            return None
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchval(
                    """
                    SELECT metadata->'profile_photo'->>'phash'
                    FROM media_items
                    WHERE source = $1 AND entity_id = $2
                      AND content_type = 'profile_photo'
                    ORDER BY collected_at DESC LIMIT 1
                    """,
                    source, entity_id,
                )
                return row if row else None
        except Exception:
            return None
