"""Well-known files scanner (Explore #4).

For domains that recur in ``discovered_links`` (>= WELL_KNOWN_MIN_LINKS
rows), fetch a small set of well-known paths and extract identity
signal from them.

Griffin's tools.myosint.training "Root .txt Scanner Bookmarklet"
surfaces contact emails, ad-tech partners, and OIDC discovery info
without any auth on the target.

**v1 scope** (per Z:\\...\\research\\scope-explore-4-well-known-scanner.md):
- robots.txt, security.txt, sitemap.xml only. Other well-known files
  wait for v2 evidence they're worth the code.
- Weekly cadence for top-500 recurring domains. Manual bulk-scan CLI
  also available.
- Sitemap capped at 500 URLs per fetch (not auto-crawled - referenced
  only) so we don't DDoS our own storage.

Default DISABLED (WELL_KNOWN_SCAN_ENABLED=0). Operator flips on after
verifying operator-side rate limits.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from typing import Iterable, Optional
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

_ENABLED = "WELL_KNOWN_SCAN_ENABLED"
_MIN_LINKS = int(os.getenv("WELL_KNOWN_MIN_LINKS", "3"))
_HOST_QPS = float(os.getenv("WELL_KNOWN_HOST_QPS", "2"))
_GLOBAL_HOURLY_CAP = int(os.getenv("WELL_KNOWN_GLOBAL_HOURLY_CAP", "500"))
_TIMEOUT = float(os.getenv("WELL_KNOWN_TIMEOUT_SECONDS", "10"))
_SITEMAP_URL_CAP = int(os.getenv("WELL_KNOWN_SITEMAP_URL_CAP", "500"))
_MAX_CONTENT_BYTES = int(os.getenv("WELL_KNOWN_MAX_CONTENT_BYTES", "102400"))  # 100KB

_TARGET_FILES = ("robots.txt", ".well-known/security.txt", "sitemap.xml")

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_SITEMAP_URL_TAG = "{http://www.sitemaps.org/schemas/sitemap/0.9}url"
_SITEMAP_LOC_TAG = "{http://www.sitemaps.org/schemas/sitemap/0.9}loc"


def _is_enabled() -> bool:
    return os.getenv(_ENABLED, "0") == "1"


def _extract_from_security_txt(text: str) -> dict:
    """Parse RFC 9116 security.txt into a structured dict."""
    out: dict = {"emails": [], "contacts": [], "policies": []}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue
        key, value = parts[0].strip().lower(), parts[1].strip()
        if key == "contact":
            out["contacts"].append(value)
            if value.lower().startswith("mailto:"):
                addr = value[len("mailto:"):]
                if addr and addr not in out["emails"]:
                    out["emails"].append(addr)
        elif key == "policy":
            out["policies"].append(value)
        elif key == "expires":
            out["expires"] = value
        elif key == "encryption":
            out.setdefault("encryption", []).append(value)
    # Fallback: any email in the body.
    for m in _EMAIL_RE.finditer(text):
        e = m.group(0)
        if e not in out["emails"]:
            out["emails"].append(e)
    return out


def _extract_from_robots(text: str) -> dict:
    """Extract disallowed paths + any incidentally exposed emails/URLs."""
    disallowed: list[str] = []
    allowed: list[str] = []
    sitemaps: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        low = line.lower()
        if low.startswith("disallow:"):
            path = line.split(":", 1)[1].strip()
            if path:
                disallowed.append(path)
        elif low.startswith("allow:"):
            path = line.split(":", 1)[1].strip()
            if path:
                allowed.append(path)
        elif low.startswith("sitemap:"):
            path = line.split(":", 1)[1].strip()
            if path:
                sitemaps.append(path)
    emails = list({m.group(0) for m in _EMAIL_RE.finditer(text)})
    return {
        "disallowed": disallowed[:200],
        "allowed": allowed[:100],
        "sitemaps": sitemaps[:20],
        "emails": emails,
    }


def _extract_from_sitemap(text: str) -> dict:
    urls: list[str] = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # Not XML - fall back to line-level URL scrape.
        for m in _URL_RE.finditer(text):
            urls.append(m.group(0))
            if len(urls) >= _SITEMAP_URL_CAP:
                break
        return {"urls": urls, "parser": "fallback_regex"}

    for url_tag in root.iter(_SITEMAP_URL_TAG):
        loc = url_tag.find(_SITEMAP_LOC_TAG)
        if loc is not None and loc.text:
            urls.append(loc.text.strip())
        if len(urls) >= _SITEMAP_URL_CAP:
            break
    # nested sitemaps: also grab the top-level <sitemap><loc> entries.
    for loc in root.iter(_SITEMAP_LOC_TAG):
        if loc.text and loc.text not in urls:
            urls.append(loc.text.strip())
        if len(urls) >= _SITEMAP_URL_CAP:
            break
    return {"urls": urls, "parser": "xml"}


def _extract(file_path: str, text: str) -> dict:
    if file_path.endswith("security.txt"):
        return _extract_from_security_txt(text)
    if file_path == "robots.txt":
        return _extract_from_robots(text)
    if file_path == "sitemap.xml":
        return _extract_from_sitemap(text)
    return {}


async def _fetch_one(client: httpx.AsyncClient, domain: str, path: str) -> dict:
    url = f"https://{domain}/{path}"
    result = {
        "domain": domain,
        "file_path": path,
        "http_status": 0,
        "content": None,
        "content_hash": None,
        "extracted_data": None,
    }
    try:
        r = await client.get(url, timeout=_TIMEOUT, follow_redirects=True)
    except Exception as exc:
        result["http_status"] = -1
        result["extracted_data"] = {"error": f"http:{type(exc).__name__}"}
        return result

    result["http_status"] = r.status_code
    if r.status_code != 200:
        return result

    body = r.text or ""
    if len(body) > _MAX_CONTENT_BYTES:
        body = body[:_MAX_CONTENT_BYTES]
    result["content"] = body
    result["content_hash"] = hashlib.sha256(body.encode("utf-8", errors="replace")).digest()
    try:
        result["extracted_data"] = _extract(path, body)
    except Exception as exc:
        logger.debug("well_known: extraction failed for %s/%s: %s", domain, path, exc)
        result["extracted_data"] = {"extraction_error": str(exc)[:200]}
    return result


async def _refresh_queue(pool) -> int:
    """Populate well_known_scan_queue with any recurring domain not
    already scheduled. Returns count of new rows added."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO well_known_scan_queue (domain, priority, next_scan)
            SELECT lower(regexp_replace(dl.url, '^https?://([^/]+).*$', '\\1')) AS d,
                   count(*)::int AS priority,
                   now() AS next_scan
            FROM discovered_links dl
            WHERE dl.url ~ '^https?://'
            GROUP BY d
            HAVING count(*) >= $1
            ON CONFLICT (domain) DO NOTHING
            """,
            _MIN_LINKS,
        )
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


async def run_well_known_scan(pool) -> dict:
    """Scan queued domains for the 3 well-known files. Returns a
    summary dict for logging."""
    summary = {"skipped": None, "queued_new": 0, "domains_scanned": 0,
               "files_fetched": 0, "extracted_emails": 0, "errors": 0}
    if not _is_enabled():
        summary["skipped"] = "disabled"
        return summary

    summary["queued_new"] = await _refresh_queue(pool)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT domain
            FROM well_known_scan_queue
            WHERE (last_scanned IS NULL OR next_scan < now())
            ORDER BY priority DESC, next_scan ASC
            LIMIT $1
            """,
            min(_GLOBAL_HOURLY_CAP, 100),
        )

    if not rows:
        return summary

    interval = 1.0 / max(_HOST_QPS, 0.01)
    headers = {
        "User-Agent": "Mozilla/5.0 (unifiedcollector-osint) well-known scanner",
        "Accept": "*/*",
    }
    async with httpx.AsyncClient(headers=headers) as client:
        for row in rows:
            domain = row["domain"]
            if not domain or "." not in domain:
                continue
            summary["domains_scanned"] += 1

            for path in _TARGET_FILES:
                result = await _fetch_one(client, domain, path)
                summary["files_fetched"] += 1

                if result["http_status"] < 0:
                    summary["errors"] += 1
                if result.get("extracted_data") and result["extracted_data"].get("emails"):
                    summary["extracted_emails"] += len(result["extracted_data"]["emails"])

                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO well_known_findings
                            (domain, file_path, fetched_at, http_status,
                             content_hash, content, extracted_data)
                        VALUES ($1, $2, now(), $3, $4, $5, $6::jsonb)
                        """,
                        result["domain"],
                        result["file_path"],
                        result["http_status"],
                        result["content_hash"],
                        result["content"],
                        json.dumps(result["extracted_data"]) if result["extracted_data"] else None,
                    )
                await asyncio.sleep(interval)

            # Weekly cadence: next_scan = now() + 7 days.
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE well_known_scan_queue
                    SET last_scanned = now(),
                        next_scan = now() + interval '7 days'
                    WHERE domain = $1
                    """,
                    domain,
                )

    logger.info("well_known_scan: %s", summary)
    return summary


__all__ = ["run_well_known_scan"]
