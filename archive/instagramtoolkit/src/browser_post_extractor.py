"""B3: Post data extractor — parse media URLs + metadata from /p/{shortcode}/."""
from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timezone
from typing import Optional

_IG_POST = "https://www.instagram.com/p/{shortcode}/"

# GraphQL typename → media type label
_TYPENAME_MAP = {
    "GraphImage": "image",
    "GraphVideo": "video",
    "GraphSidecar": "carousel",
    "XDTGraphImage": "image",
    "XDTGraphVideo": "video",
    "XDTGraphSidecar": "carousel",
}


def extract_post_data(page, shortcode: str) -> Optional[dict]:
    """Navigate to a post and return structured metadata + media items.

    Returns None on error.

    Return shape:
        {
            shortcode: str,
            post_id: str,
            taken_at: datetime (UTC),
            username: str,
            typename: 'image' | 'video' | 'carousel',
            media: [
                {url: str, type: 'image'|'video', index: int}
            ]
        }
    """
    url = _IG_POST.format(shortcode=shortcode)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(random.uniform(1.2, 2.5))
    except Exception as e:
        print(f"[EXTRACT] {shortcode}: navigation failed: {e}")
        return None

    raw = _extract_json(page)
    if not raw:
        print(f"[EXTRACT] {shortcode}: no JSON data found")
        return None

    try:
        return _parse_post(shortcode, raw)
    except Exception as e:
        print(f"[EXTRACT] {shortcode}: parse error: {e}")
        return None


def _extract_json(page) -> Optional[dict]:
    """Pull post data from the inline <script type="application/json"> tag.

    Instagram embeds the post graph in a script tag on the page.
    We try multiple known extraction patterns in order.
    """
    # Strategy 1: look for window.__additionalDataLoaded via page.evaluate
    try:
        data = page.evaluate("""() => {
            const scripts = document.querySelectorAll('script[type="application/json"]');
            for (const s of scripts) {
                try {
                    const d = JSON.parse(s.textContent);
                    if (d && d.require) return d;
                } catch {}
            }
            return null;
        }""")
        if data:
            return _unwrap_require(data)
    except Exception:
        pass

    # Strategy 2: raw text search for the JSON blob containing shortcode
    try:
        content = page.content()
        # Find the JSON block that contains media data
        for pattern in [
            r'"shortcode_media"\s*:\s*(\{.*?\})\s*}',
            r'{"data":{"shortcode_media":(\{.*?\})}',
        ]:
            match = re.search(pattern, content, re.DOTALL)
            if match:
                return json.loads(match.group(1))
    except Exception:
        pass

    # Strategy 3: intercept the GraphQL XHR (navigate again with response capture)
    return None


def _unwrap_require(data: dict) -> Optional[dict]:
    """Dig through Instagram's require() bundle to find shortcode_media."""
    try:
        # Walk require array looking for shortcode_media
        for entry in data.get("require", []):
            j = json.dumps(entry)
            if "shortcode_media" in j:
                # Find the dict that has shortcode_media key
                idx = j.find('"shortcode_media"')
                # Extract surrounding object
                chunk = j[idx - 10:]
                # Quick parse attempt
                for m in re.finditer(r'\{[^{}]{20,}\}', chunk):
                    try:
                        candidate = json.loads(m.group())
                        if "shortcode_media" in candidate:
                            return candidate["shortcode_media"]
                        # Sometimes it's nested one level deeper
                        for v in candidate.values():
                            if isinstance(v, dict) and "shortcode_media" in v:
                                return v["shortcode_media"]
                    except Exception:
                        continue
    except Exception:
        pass
    return None


def _parse_post(shortcode: str, raw: dict) -> Optional[dict]:
    """Convert raw JSON blob to structured post dict."""
    # raw might be the shortcode_media dict directly or contain it
    if "shortcode_media" in raw:
        raw = raw["shortcode_media"]

    post_id = str(raw.get("id", shortcode))
    taken_at_ts = raw.get("taken_at_timestamp") or raw.get("taken_at") or 0
    taken_at = datetime.fromtimestamp(float(taken_at_ts), tz=timezone.utc) if taken_at_ts else datetime.now(timezone.utc)

    owner = raw.get("owner") or {}
    username = owner.get("username", "unknown")

    typename_raw = raw.get("__typename", "GraphImage")
    typename = _TYPENAME_MAP.get(typename_raw, "image")

    media_items = []

    if typename == "carousel":
        edges = (
            (raw.get("edge_sidecar_to_children") or {}).get("edges") or []
        )
        for i, edge in enumerate(edges, 1):
            node = edge.get("node") or {}
            item = _extract_media_item(node, i)
            if item:
                media_items.append(item)
    else:
        item = _extract_media_item(raw, 1)
        if item:
            media_items.append(item)

    if not media_items:
        return None

    return {
        "shortcode": shortcode,
        "post_id": post_id,
        "taken_at": taken_at,
        "username": username,
        "typename": typename,
        "media": media_items,
    }


def _extract_media_item(node: dict, index: int) -> Optional[dict]:
    """Extract single media item (image or video) from a node dict."""
    is_video = node.get("is_video", False) or node.get("__typename") in ("GraphVideo", "XDTGraphVideo")

    if is_video:
        url = node.get("video_url")
        media_type = "video"
    else:
        # Prefer highest resolution
        resources = node.get("display_resources") or []
        if resources:
            url = resources[-1].get("src")
        else:
            url = node.get("display_url")
        media_type = "image"

    if not url:
        return None

    return {"url": url, "type": media_type, "index": index}
