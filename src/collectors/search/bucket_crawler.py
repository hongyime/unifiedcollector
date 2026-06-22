"""Open cloud-bucket enumeration for the search collector.

When a search dork surfaces a misconfigured **public** bucket, its root URL returns
an XML directory listing (`<ListBucketResult>` for S3-compatible stores: AWS S3,
DigitalOcean Spaces, Wasabi, MinIO; GCS returns a similar `<ListBucketResult>` too).
This parses that listing into object URLs so the collector can pull the media,
routed through the same magic-byte file gate as everything else.

This is OSINT over *already-public* data — it reads listings the bucket owner left
open. It does not bypass auth. Pure functions here are unit-testable; the async
fetch/pagination lives in the collector.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from urllib.parse import quote, urljoin, urlparse

# Only pull real media/docs (the file gate is the final authority on bytes).
MEDIA_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".heic",
    ".mp4", ".mov", ".webm", ".m4v", ".mkv", ".avi",
    ".pdf",
    ".mp3", ".m4a", ".wav", ".flac", ".ogg",
}

# Hosts whose root path returns an S3-style listing.
_BUCKET_HOST_HINTS = (
    "s3.amazonaws.com", ".s3.", "digitaloceanspaces.com", "wasabisys.com",
    "storage.googleapis.com", "blob.core.windows.net", "r2.cloudflarestorage.com",
)


def looks_like_bucket_host(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return any(h in host for h in _BUCKET_HOST_HINTS)


def is_bucket_listing(text: str) -> bool:
    return "<ListBucketResult" in (text or "")[:2000]


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def parse_bucket_listing(xml_text: str, base_url: str):
    """Return (object_urls, next_token).

    next_token is ("continuation-token", v) for S3 list-type=2, ("marker", v) for
    v1 / GCS, or None when the listing is complete. base_url is the bucket root.
    """
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return [], None
    keys: list[str] = []
    next_token = None
    is_truncated = False
    for el in root.iter():
        t = _local(el.tag)
        if t == "Key" and el.text:
            keys.append(el.text)
        elif t == "IsTruncated":
            is_truncated = (el.text or "").strip().lower() == "true"
        elif t == "NextContinuationToken" and el.text:
            next_token = ("continuation-token", el.text)
        elif t == "NextMarker" and el.text:
            next_token = ("marker", el.text)
    if is_truncated and next_token is None and keys:
        next_token = ("marker", keys[-1])  # v1 buckets without NextMarker
    base = base_url if base_url.endswith("/") else base_url + "/"
    urls = [urljoin(base, quote(k, safe="/")) for k in keys]
    return urls, (next_token if is_truncated else None)


def media_only(urls):
    out = []
    for u in urls:
        ext = os.path.splitext(urlparse(u).path.lower())[1]
        if ext in MEDIA_EXT:
            out.append(u)
    return out


def next_page_url(base_url: str, token) -> str:
    kind, val = token
    sep = "&" if "?" in base_url else "?"
    if kind == "continuation-token":
        return f"{base_url}{sep}list-type=2&continuation-token={quote(val, safe='')}"
    return f"{base_url}{sep}marker={quote(val, safe='')}"


def bucket_root(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


# Dork query templates that tend to surface open buckets + loose PDFs. {q} = topic.
# Seeded into config/sources/search.targets (expanded per topic by the collector).
DORK_TEMPLATES = [
    'site:s3.amazonaws.com {q}',
    'site:storage.googleapis.com {q}',
    'site:digitaloceanspaces.com {q}',
    'site:blob.core.windows.net {q}',
    '"index of" {q} (pdf OR jpg OR mp4)',
    '{q} filetype:pdf',
    'intitle:"ListBucketResult" {q}',
]


def expand_dorks(topics):
    out = []
    for topic in topics:
        for tmpl in DORK_TEMPLATES:
            out.append(tmpl.format(q=topic))
    return out
