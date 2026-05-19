import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class SearchCache:
    """File-based search result cache with TTL expiration and atomic writes.

    Each query+engine pair is stored as a JSON file keyed by MD5(query|engine).
    Corrupt files are auto-deleted on read.  Writes use tempfile→rename for
    crash safety.
    """

    def __init__(self, cache_dir: str | Path, ttl_hours: float = 24.0):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_hours * 3600

    def _key(self, query: str, engine: str = "default") -> str:
        raw = f"{query.strip().lower()}|{engine.strip().lower()}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def get(self, query: str, engine: str = "default") -> dict | None:
        key = self._key(query, engine)
        path = self._path(key)
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Corrupt cache entry %s — removing", path.name)
            path.unlink(missing_ok=True)
            return None

        cached_at = data.get("_cached_at", 0)
        if time.time() - cached_at > self.ttl_seconds:
            logger.debug("Cache expired for %s", path.name)
            path.unlink(missing_ok=True)
            return None

        return data.get("results")

    def put(self, query: str, results: dict | list, engine: str = "default"):
        key = self._key(query, engine)
        path = self._path(key)
        payload = {
            "_query": query,
            "_engine": engine,
            "_cached_at": time.time(),
            "results": results,
        }
        fd, tmp = tempfile.mkstemp(dir=self.cache_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def delete(self, query: str, engine: str = "default"):
        key = self._key(query, engine)
        self._path(key).unlink(missing_ok=True)

    def clear(self):
        for f in self.cache_dir.glob("*.json"):
            f.unlink(missing_ok=True)

    def stats(self) -> dict:
        entries = list(self.cache_dir.glob("*.json"))
        now = time.time()
        engines: dict[str, int] = {}
        oldest = float("inf")
        newest = 0.0
        valid = 0

        for f in entries:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                cached_at = data.get("_cached_at", 0)
                engine = data.get("_engine", "unknown")
                if now - cached_at <= self.ttl_seconds:
                    valid += 1
                    engines[engine] = engines.get(engine, 0) + 1
                    oldest = min(oldest, cached_at)
                    newest = max(newest, cached_at)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

        return {
            "total_files": len(entries),
            "valid_entries": valid,
            "engines": engines,
            "oldest_ts": oldest if oldest != float("inf") else None,
            "newest_ts": newest if newest > 0 else None,
        }
