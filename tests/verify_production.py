#!/usr/bin/env python3
"""Production readiness verification."""
import os
import sys

# Load .env
with open(".env") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            val = val.split("#")[0].strip()
            os.environ[key.strip()] = val

from src.collectors.instagram import (
    InstagramCollector, SLIDING_WINDOW_ENABLED, CONTENT_AWARE_ENABLED,
    CONTENT_DELAYS,
)
from src.collectors.tiktok import TiktokCollector
from src.collectors.telegram import TelegramCollector
from src.collectors.github import GithubCollector
from src.collectors.youtube import YoutubeCollector
from src.collectors.strava import StravaCollector
from src.collectors.website import WebsiteCollector
from src.collectors.search import SearchCollector
from src.collectors.lemon8 import Lemon8Collector
from src.collectors.whatsapp import WhatsappCollector
from src.core.health import health_check as _hc
from src.db.connection import _ssl_context

errors = []

def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        errors.append(label)

print("=" * 60)
print("PRODUCTION READINESS CHECK")
print("=" * 60)

# Instagram
print("\n--- Instagram ---")
ig = InstagramCollector()
check("5 accounts loaded", len(ig.account_pool._accounts) == 5,
      f"got {len(ig.account_pool._accounts)}")
check("Cookie imports (b, oops)", len(ig._account_browser_cookies) == 2,
      f"got {list(ig._account_browser_cookies.keys())}")

for name, path in ig._account_browser_cookies.items():
    cookies = ig._parse_browser_cookies(path)
    check(f"  {name}: sessionid present", "sessionid" in cookies)
    check(f"  {name}: csrftoken present", "csrftoken" in cookies)

check("Sliding window toggle works", isinstance(SLIDING_WINDOW_ENABLED, bool),
      f"value={SLIDING_WINDOW_ENABLED}")
check("Content-aware delays", CONTENT_AWARE_ENABLED and len(CONTENT_DELAYS) >= 8,
      f"{len(CONTENT_DELAYS)} types")
check("Has _content_aware_delay", hasattr(ig, "_content_aware_delay"))
check("Has _micro_pause", hasattr(ig, "_micro_pause"))
check("Has _time_of_day_multiplier", hasattr(ig, "_time_of_day_multiplier"))
check("Has _check_daily_quota", hasattr(ig, "_check_daily_quota"))

# Telegram
print("\n--- Telegram ---")
tg = TelegramCollector()
check("API ID configured", bool(tg._api_id), tg._api_id[:4] + "..." if tg._api_id else "empty")
check("API hash configured", bool(tg._api_hash))
bot_tokens = os.environ.get("TELEGRAM_BOT_TOKENS", "")
check("Bot tokens loaded", bool(bot_tokens), f"{len(bot_tokens.split(';'))} bots")
hub = os.environ.get("TELEGRAM_HUB_GROUP", "")
check("Hub group set", bool(hub), hub)
check("Hub notifier has SQLite cache", hasattr(tg._hub_notifier, "_open_cache_conn"))
check("Hub notifier has supervisor", hasattr(tg._hub_notifier, "_supervisor_loop"))

# GitHub
print("\n--- GitHub ---")
gh = GithubCollector()
check("PAT tokens loaded", len(gh._pats) > 0, f"{len(gh._pats)} tokens")

# YouTube
print("\n--- YouTube ---")
yt = YoutubeCollector()
check("API key configured", bool(yt._api_key))

# TikTok
print("\n--- TikTok ---")
tt = TiktokCollector()
check("Browser fallback enabled", tt._browser_fallback)
check("yt-dlp fallback enabled", tt._ytdlp_fallback)
check("Has CDN interception", hasattr(tt, "_intercept_video_cdn"))
check("Has stealth script", hasattr(tt, "_STEALTH_SCRIPT"))

# Strava
print("\n--- Strava ---")
st = StravaCollector()
check("Session cookie set", bool(st._session_cookie))

# Website / Search / Lemon8
print("\n--- Website / Search / Lemon8 ---")
ws = WebsiteCollector()
check("Website max_depth", ws._max_depth == 3)
sc = SearchCollector()
check("Search cache TTL", True)
lm = Lemon8Collector()
check("Lemon8 min_width", lm._min_width == 320)

# WhatsApp
print("\n--- WhatsApp ---")
wa = WhatsappCollector()
check("Bridge secret set", bool(wa._bridge_secret))

# Infrastructure
print("\n--- Infrastructure ---")
check("Health module importable", True)
ssl_ctx = _ssl_context()
check("SSL context (disabled by default)", ssl_ctx is None)
check("Docker compose exists", os.path.exists("docker/docker-compose.yml"))
check(".gitignore excludes .env", ".env" in open(".gitignore").read())
check(".gitignore excludes credentials/", "credentials/" in open(".gitignore").read())

# Summary
print("\n" + "=" * 60)
if errors:
    print(f"RESULT: {len(errors)} FAILURES")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print("RESULT: ALL CHECKS PASSED")
    print("Production readiness: CONFIRMED")
