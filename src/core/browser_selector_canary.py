"""Automated Selector Health Canaries for UnifiedCollector browser platforms.

Periodically or on-demand verifies that DOM selectors (articles, images, permalink links)
still match live rendered platform DOMs in Chrome CDP.
When platforms update their frontend classes/markup, this canary detects selector decay
before hours of collection are lost to silent zero yields.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger("selector_canary")

CDP_DEFAULT_PORT = "9223"

PLATFORM_SELECTOR_SPECS: dict[str, dict[str, str]] = {
    "instagram": {
        "articles": "article",
        "post_links": "a[href*='/p/']",
        "feed_images": "img[src*='cdninstagram.com'], img[src*='fbcdn.net']",
    },
    "threads": {
        "cards": "div[data-pressable-container='true'], article",
        "post_links": "a[href*='/post/']",
    },
    "facebook": {
        "articles": "div[role='article'], div[data-pagelet*='FeedUnit']",
        "cdn_images": "img[src*='fbcdn.net']",
    },
    "tiktok": {
        "video_elements": "video",
        "cards": "[data-e2e='recommend-list-item-container'], [data-e2e='user-post-item']",
        "cdn_images": "img[src*='tiktokcdn'], img[src*='byteimg']",
    },
    "lemon8": {
        "cards": "div[class*='card'], article",
        "images": "img[src*='ttwstatic.com'], img[src*='byteimg'], img",
    },
    "x": {
        "tweets": "article[data-testid='tweet']",
        "status_links": "a[href*='/status/']",
    },
    "strava": {
        "feed_entries": "div[data-testid='web-feed-entry'], div[data-testid='activity_feed']",
    },
}


def cdp_url() -> str:
    return os.getenv(
        "UC_CHROME_CDP_URL",
        f"http://127.0.0.1:{os.getenv('UC_CHROME_CDP_PORT', CDP_DEFAULT_PORT)}",
    ).rstrip("/")


def list_cdp_targets() -> list[dict[str, Any]]:
    try:
        url = f"{cdp_url()}/json/list"
        with urllib.request.urlopen(url, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data if isinstance(data, list) else []
    except Exception as exc:
        logger.debug("CDP target query failed: %s", exc)
        return []


def detect_platform_from_url(url: str) -> str | None:
    u = (url or "").lower()
    if "instagram.com" in u:
        return "instagram"
    if "threads.com" in u or "threads.net" in u:
        return "threads"
    if "facebook.com" in u:
        return "facebook"
    if "tiktok.com" in u:
        return "tiktok"
    if "lemon8-app.com" in u:
        return "lemon8"
    if "x.com" in u or "twitter.com" in u:
        return "x"
    if "strava.com" in u:
        return "strava"
    return None


def run_selector_canary() -> dict[str, Any]:
    """Inspect active CDP tabs and verify DOM selectors across platforms."""
    try:
        import websocket  # type: ignore
    except ImportError:
        return {"error": "websocket-client not installed; cannot connect to CDP"}

    targets = list_cdp_targets()
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        return {"ok": False, "reason": "no_cdp_pages_found", "targets_count": len(targets)}

    results: dict[str, Any] = {}
    for page in pages:
        url = page.get("url") or ""
        platform = detect_platform_from_url(url)
        if not platform or platform not in PLATFORM_SELECTOR_SPECS:
            continue
        ws_url = page.get("webSocketDebuggerUrl")
        if not ws_url:
            continue

        specs = PLATFORM_SELECTOR_SPECS[platform]
        page_result: dict[str, Any] = {
            "url": url,
            "target_id": page.get("id"),
            "selectors": {},
            "all_passed": True,
        }

        try:
            ws = websocket.create_connection(ws_url, timeout=5)
            # Evaluate selector counts in page DOM
            js_code = """
            (() => {
                const specs = %s;
                const out = {};
                for (const [name, sel] of Object.entries(specs)) {
                    try {
                        out[name] = document.querySelectorAll(sel).length;
                    } catch (e) {
                        out[name] = -1;
                    }
                }
                return out;
            })()
            """ % json.dumps(specs)

            mid = 1
            payload = json.dumps({
                "id": mid,
                "method": "Runtime.evaluate",
                "params": {"expression": js_code, "returnByValue": True},
            })
            ws.send(payload)
            reply = json.loads(ws.recv())
            ws.close()

            counts = (reply.get("result", {}).get("result", {}).get("value")) or {}
            for name, sel in specs.items():
                count = counts.get(name, 0)
                matched = count > 0
                page_result["selectors"][name] = {
                    "selector": sel,
                    "count": count,
                    "matched": matched,
                }
                if not matched:
                    page_result["all_passed"] = False
        except Exception as exc:
            page_result["error"] = str(exc)
            page_result["all_passed"] = False

        results[platform] = page_result

    return {
        "ok": True,
        "cdp_url": cdp_url(),
        "platforms_probed": list(results.keys()),
        "results": results,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    res = run_selector_canary()
    print(json.dumps(res, indent=2))
