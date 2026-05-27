"""
search_cache.py — Search result caching for SearchToolkit.

Provides TTL-based caching of search results to avoid redundant API calls
and reduce costs.

Design inspired by FORGE's caching patterns but simplified for SearchToolkit needs.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Set


class SearchCache:
    """TTL-based cache for search results."""

    def __init__(self, cache_dir: Path, ttl_hours: int = 24):
        """
        Initialize search cache.

        Args:
            cache_dir: Directory to store cache files.
            ttl_hours: Time-to-live for cached results in hours.
        """
        self.cache_dir = cache_dir
        self.ttl_hours = ttl_hours
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _cache_path(self, query: str, engine: str) -> Path:
        """Generate cache file path for a query/engine combination."""
        key = hashlib.md5(f"{engine}:{query}".encode()).hexdigest()
        return self.cache_dir / f"{key}.json"

    def get(self, query: str, engine: str) -> Optional[Set[str]]:
        """
        Get cached search results.

        Args:
            query: Search query string.
            engine: Search engine name (duckduckgo, bing, serper).

        Returns:
            Set of URLs if cache hit and not expired, None otherwise.
        """
        path = self._cache_path(query, engine)
        
        with self._lock:
            if not path.exists():
                return None

            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                # Corrupted cache file, remove it
                path.unlink(missing_ok=True)
                return None

            # Check TTL
            cached_at = datetime.fromisoformat(data['cached_at'])
            if datetime.now(timezone.utc) - cached_at > timedelta(hours=self.ttl_hours):
                # Expired, remove and return None
                path.unlink(missing_ok=True)
                return None

            return set(data['results'])

    def set(self, query: str, engine: str, results: Set[str]) -> None:
        """
        Cache search results.

        Args:
            query: Search query string.
            engine: Search engine name.
            results: Set of URLs to cache.
        """
        path = self._cache_path(query, engine)
        
        data = {
            'cached_at': datetime.now(timezone.utc).isoformat(),
            'query': query,
            'engine': engine,
            'results': sorted(results)
        }

        with self._lock:
            # Atomic write: write to temp file, then rename
            temp_path = path.with_suffix('.tmp')
            try:
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2)
                temp_path.replace(path)
            except IOError as e:
                temp_path.unlink(missing_ok=True)
                # Cache write failures are non-fatal — log and continue
                import logging
                logging.getLogger(__name__).warning("Cache write failed: %s", e)

    def clear(self, query: Optional[str] = None, engine: Optional[str] = None) -> int:
        """
        Clear cache entries.

        Args:
            query: If provided, only clear cache for this query.
            engine: If provided, only clear cache for this engine.

        Returns:
            Number of cache entries cleared.
        """
        cleared = 0
        
        with self._lock:
            for cache_file in self.cache_dir.glob("*.json"):
                try:
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    
                    # Check if this entry matches the filter
                    if query and data.get('query') != query:
                        continue
                    if engine and data.get('engine') != engine:
                        continue
                    
                    cache_file.unlink(missing_ok=True)
                    cleared += 1
                except (json.JSONDecodeError, IOError):
                    # Corrupted file, remove it
                    cache_file.unlink(missing_ok=True)
                    cleared += 1

        return cleared

    def cleanup_expired(self) -> int:
        """
        Remove all expired cache entries.

        Returns:
            Number of entries removed.
        """
        removed = 0
        
        with self._lock:
            for cache_file in self.cache_dir.glob("*.json"):
                try:
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    
                    cached_at = datetime.fromisoformat(data['cached_at'])
                    if datetime.now(timezone.utc) - cached_at > timedelta(hours=self.ttl_hours):
                        cache_file.unlink(missing_ok=True)
                        removed += 1
                except (json.JSONDecodeError, IOError):
                    # Corrupted file, remove it
                    cache_file.unlink(missing_ok=True)
                    removed += 1

        return removed

    def get_stats(self) -> Dict:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache stats.
        """
        stats = {
            'total_entries': 0,
            'expired_entries': 0,
            'total_results': 0,
            'engines': {},
            'oldest_entry': None,
            'newest_entry': None
        }

        with self._lock:
            for cache_file in self.cache_dir.glob("*.json"):
                try:
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    
                    stats['total_entries'] += 1
                    results_count = len(data.get('results', []))
                    stats['total_results'] += results_count
                    
                    engine = data.get('engine', 'unknown')
                    stats['engines'][engine] = stats['engines'].get(engine, 0) + 1
                    
                    cached_at = datetime.fromisoformat(data['cached_at'])
                    
                    # Check if expired
                    if datetime.now(timezone.utc) - cached_at > timedelta(hours=self.ttl_hours):
                        stats['expired_entries'] += 1
                    
                    # Track oldest/newest
                    if stats['oldest_entry'] is None or cached_at < stats['oldest_entry']:
                        stats['oldest_entry'] = cached_at
                    if stats['newest_entry'] is None or cached_at > stats['newest_entry']:
                        stats['newest_entry'] = cached_at
                        
                except (json.JSONDecodeError, IOError):
                    continue

        return stats

