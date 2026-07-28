"""Shared persistence for Tier 6 URL discovery."""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

from src.core.link_extractor import extract_all_links

logger = logging.getLogger(__name__)


async def persist_discovered_links(
    conn,
    *,
    source: str,
    source_record_id: str,
    text: str | None,
    source_table: str | None = None,
    context_id: str | None = None,
    entity_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Extract URLs from text and persist source-local occurrences.

    Fail-soft by design: Tier 6 link discovery must never break primary message
    or video ingest. Returns the number of attempted link rows that landed.
    """
    try:
        links = extract_all_links(text or "")
    except Exception:
        logger.debug("link extraction failed for %s/%s", source, source_record_id, exc_info=True)
        return 0
    if not links:
        return 0

    base_meta = dict(metadata or {})
    written = 0
    for url, link_type in links:
        try:
            domain = urlparse(url).netloc.lower() or None
        except Exception:
            domain = None
        row_meta = dict(base_meta)
        row_meta["link_type"] = link_type
        try:
            await conn.execute(
                """
                INSERT INTO discovered_links (
                    source, source_table, source_record_id, context_id,
                    entity_id, url, domain, link_type, discovered_at, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW(), $9::jsonb)
                ON CONFLICT (source, source_record_id, url) DO UPDATE SET
                    domain = EXCLUDED.domain,
                    link_type = EXCLUDED.link_type,
                    metadata = COALESCE(discovered_links.metadata, '{}'::jsonb)
                               || EXCLUDED.metadata
                """,
                source,
                source_table,
                str(source_record_id),
                str(context_id) if context_id is not None else None,
                str(entity_id) if entity_id is not None else None,
                url,
                domain,
                link_type,
                json.dumps(row_meta, default=str),
            )
            written += 1
        except Exception:
            logger.debug("discovered_links insert skipped for %s: %s", source, url, exc_info=True)
    return written
