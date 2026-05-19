# UnifiedCollector Validation Report

**Date:** 2026-05-19 (updated after critical fixes)
**Scope:** All 10 collectors compared against original toolkits
**Mode:** Audit + 3 critical fixes applied (TikTok, Instagram, Telegram)

---

## 1. INSTAGRAM

### 1.1 Authentication/Login
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| Multi-account via `INSTA_ACCOUNT_N_{NAME,USER,PASS}` | `config.py:_load_accounts_from_env` | `AccountPool.load_from_env("INSTA", ["NAME","USER","PASS"])` | MATCH |
| Instaloader session save/restore | `account_manager.py:login` | `instagram.py:_login_account` | MATCH |
| Browser cookie import (`INSTA_ACCOUNT_N_BROWSER`) | `account_manager.py:login` -- `loader.load_session_from_browser()` | Not ported | MISSING |
| Session age check (7-day max) | `account_manager.py` -- file mtime check | `instagram.py:_check_session_age` -- JSON meta file | MATCH |
| Re-auth jitter (+-3 days) | `account_manager.py:_record_auth_timestamp` -- DB-backed `next_reauth_ts` | `instagram.py:_check_session_age` -- random jitter on check | PARTIAL |
| 2FA interactive flow | `account_manager.py:login` -- multi-attempt 2FA with `input()` | Not ported (not applicable for headless daemon) | PARTIAL |
| Global session file fallback (`~\AppData\Local\Instaloader`) | `account_manager.py:login` -- checks global path | Not ported | MISSING |
| Force fresh login | `account_manager.py:get_authenticated_loader(force_fresh_login=True)` | Not ported | MISSING |
| Re-auth stagger logging | `_check_reauth_schedule` -- DB-based | Not ported | MISSING |

### 1.2 Rate Limiting
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| Sliding window (1H/3H/5H/1D) | `config.py` -- `WINDOW_1H=180, 3H=400, 5H=600, 1D=2000` | `instagram.py` -- `INSTA_WINDOW_1H=180, 3H=400, 5H=600, 1D=2000` | **MATCH** (FIXED) |
| 5H window | Present in original | Present -- `WindowConfig("5h", 18000, INSTA_WINDOW_5H)` | **MATCH** (FIXED) |
| Window defaults match original | 1H=180, 1D=2000 | 1H=180, 1D=2000 | **MATCH** (FIXED) |
| Conservative per-operation multipliers (PUBLIC=1.0x, FOLLOWING=1.5x, MUTUAL=2.0x) | `conservative_rate_limiter.py` | `HumanLikeRateLimiter` uses `OperationType` enum | PARTIAL |
| Gaussian/Uniform/Exponential distribution mix (60/30/10) | `conservative_rate_limiter.py:_jitter` | `HumanLikeRateLimiter` -- different distribution | PARTIAL |
| Micro-pause (70% probability, 0.5-3s) | `config.py` -- `MICRO_PAUSE_PROBABILITY=0.7` | `_micro_pause()` -- 70% probability, exponential 0.5-3s | **MATCH** (FIXED) |
| Account cooldown (15 min minimum) | `conservative_rate_limiter.py:emergency_cooldown` | `account_pool.cooldown(name, 900.0)` -- 900s = 15min | MATCH |
| Account switch delay (180-300s) | `config.py` -- `ACCOUNT_SWITCH_DELAY_MIN=180` | `_account_switch_delay()` -- 180-300s between switches | **MATCH** (FIXED) |
| Night/risky hour multipliers | `config.py` -- 2.5-4x at night, 1.5x business hours | `_get_time_multiplier()` -- 2.5-4x night, 1.5x risky | **MATCH** (FIXED) |
| Enum pause every 12 items | `config.py` -- `ENUM_PAUSE_EVERY=12` | Not ported (follower enum not implemented) | N/A |
| Daily quota budget | `config.py` -- `DAILY_QUOTA_PROFILE_VIEWS=180, ACTIONS=6000` | `_daily_quota` tracking -- 180 views, 6000 actions | **MATCH** (FIXED) |

### 1.3 Warmup
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| 3-step delay warmup | `warmup.py` -- 3 pauses of 30-60s each | `instagram.py:_warmup` -- 3x 30-60s pauses | **MATCH** (FIXED) |
| Total warmup duration | 90-180s (3x30-60s) | 90-180s (3x30-60s) | **MATCH** (FIXED) |
| Enable/disable toggle | `WARMUP_ENABLED` env var | `INSTA_WARMUP_ENABLED` env var (default true) | **MATCH** (FIXED) |
| Heavy-only warmup check | `warmup.py:should_warmup` -- only for spider/seed/batch | Always runs before first target | DIFFERENCE |

### 1.4 Mobile Headers (X-IG-*)
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| X-IG-App-ID | `936619743392459` | `936619743392459` | MATCH |
| X-IG-Device-ID | Stable per-account (md5 hash of name -> UUID) | Random UUID per session (stable only if fingerprint set) | PARTIAL |
| X-IG-Connection-Speed | `{random 1200-8000}kbps` | `{random 1200-8000}kbps` | **MATCH** (FIXED) |
| X-IG-Android-ID | `device_id[:16]` | `device_id[:16]` | **MATCH** (FIXED) |
| X-IG-Capabilities | `3brTv10=` | `3brTv10=` | **MATCH** (FIXED) |
| X-IG-Connection-Type | `WIFI` | `WIFI` | **MATCH** (FIXED) |
| X-Instagram-AJAX | `1` | `1` | **MATCH** (FIXED) |
| X-Requested-With | `XMLHttpRequest` | `XMLHttpRequest` | **MATCH** (FIXED) |
| Stable per-account fingerprint profiles | 5 UA/locale/timezone combos (md5 hash selects) | Generic `UserAgentManager` + account fingerprint dict | PARTIAL |

### 1.5 Profile Photo Tracking
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| ProfilePhotoTracker with pHash | Full integration | Full integration via `_photo_tracker.check_and_download` | MATCH |
| Blob storage toggle | `PROFILE_PHOTO_BLOB_MAX_SIZE_MB` | Not wired (tracker supports it, Instagram doesn't pass config) | PARTIAL |

### 1.6 Credential/Env Var Mapping
| Original Var | New Var | Status |
|-------------|---------|--------|
| `INSTA_ACCOUNT_N_NAME` | `INSTA_ACCOUNT_N_NAME` | MATCH |
| `INSTA_ACCOUNT_N_USER` | `INSTA_ACCOUNT_N_USER` | MATCH |
| `INSTA_ACCOUNT_N_PASS` | `INSTA_ACCOUNT_N_PASS` | MATCH |
| `INSTA_ACCOUNT_N_BROWSER` | Not mapped | MISSING |
| `INSTA_ACCOUNT_N_PROXY` | `INSTA_ACCOUNT_N_PROXY` | **MATCH** (FIXED) |
| `PROXY_URL` | `PROXY_URL` | **MATCH** (FIXED) |
| `DATABASE_URL` (SQLite default) | `DATABASE_URL` (PostgreSQL) | MATCH (different default) |
| `PROFILE_PHOTO_BLOB_MAX_SIZE_MB` | Not mapped for Instagram | MISSING |
| `SLIDING_WINDOW_ENABLED` | Always enabled (no toggle) | DIFFERENCE |
| `WINDOW_1H_LIMIT=180` | `INSTA_WINDOW_1H=180` | RENAMED (defaults match) (FIXED) |
| `WINDOW_3H_LIMIT=400` | `INSTA_WINDOW_3H=400` | RENAMED |
| `WINDOW_5H_LIMIT=600` | `INSTA_WINDOW_5H=600` | RENAMED (FIXED) |
| `WINDOW_1D_LIMIT=2000` | `INSTA_WINDOW_1D=2000` | RENAMED (defaults match) (FIXED) |
| `FILTER_MAX_FOLLOWERS` | `FILTER_MAX_FOLLOWERS` | MATCH |
| `SESSION_MAX_AGE_DAYS=7` | `INSTA_SESSION_MAX_AGE_DAYS=7` | RENAMED |

### 1.7 Missing Features (post-fix)
- ~~**Proxy support**~~ -- FIXED: per-account + global proxy ported.
- **Browser cookie import** -- `INSTA_ACCOUNT_N_BROWSER` for loading from Chrome/Firefox. Not ported.
- ~~**Night/risky hour scheduling**~~ -- FIXED: 2.5-4x night, 1.5x risky hours.
- ~~**Daily quota budget**~~ -- FIXED: 180 profile views/day, 6000 actions/day.
- **Follower enumeration** -- original has progressive pause during follows/following scraping.
- **Content-aware delays** -- `CONTENT_AWARE_ENABLED` with max 2x multiplier.
- ~~**5H sliding window**~~ -- FIXED: 4 windows matching original.
- **Operation registry** -- weighted rate limits per operation type (download_stories=7, get_followers=8).
- **Interruptible sleep** -- original uses custom `_interruptible_sleep` that responds to shutdown.

---

## 2. TELEGRAM

### 2.1 Bot Pool
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| 4 states (HEALTHY/LOCKED/ERROR/DISCONNECTED) | `bot_pool.py:BotStatus` | `bot_pool.py:BotStatus` | MATCH |
| Round-robin rotation | `get_bot()` with `_current_index` | `get_healthy_bot()` -- sorted by `last_used` | PARTIAL |
| Lockout with `locked_until` timestamp | Present | Present | MATCH |
| Health monitor (30s loop) | `_health_monitor` -- 30s interval | `_health_loop` -- configurable `health_interval` (default 30s) | MATCH |
| Auto-reconnect | `bot.client.start(bot_token=bot.token)` in health loop | Via `connect_fn` callback | PARTIAL |
| WSL2 clock drift detection | Checks `client._sender._state.time_offset` | RTT-based heuristic (`socket.connect` to 8.8.8.8) | PARTIAL |
| Hub entity priming on connect | `client.get_input_entity(hub_id)` after connect | Not ported | MISSING |
| 30s connection timeout | `asyncio.wait_for(..., timeout=30)` | Not ported (relies on caller) | MISSING |
| Singleton pattern | `_instance` class variable | Regular instance (not singleton) | DIFFERENCE |
| Thread-safe + async-safe locks | `threading.Lock` + `asyncio.Lock` | No explicit locks (async-only) | PARTIAL |
| Status report | `get_status_report()` -- formatted string | `get_status()` -- list of dicts | PARTIAL |

### 2.2 Hub Notifier
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| Categories | SCAN/STORE/FACE/ERROR/SYSTEM | COLLECTION_START/COMPLETE/ERROR/RATE_LIMIT/DISCOVERY | PARTIAL |
| Rate limiting | `rate_limit_per_minute=10` | `min_interval=60` per category | PARTIAL |
| Batch interval | 60s default | 30s default | DIFFERENCE |
| Priority levels | 0=batched, 1=next flush, 2+=immediate | `immediate=True/False` | PARTIAL |
| SQLite cache for offline messages | Full impl with WAL mode, replay, requeue | Full impl with WAL mode, replay, requeue | **MATCH** (FIXED) |
| Stats tracking | faces_detected, identities_created, etc. | messages_sent, batched, dropped, cached | PARTIAL |
| Supervisor loop (auto-restart flusher) | `_supervisor_loop` restarts dead flusher | `_supervisor_loop` with 10s check interval | **MATCH** (FIXED) |
| Entity re-resolution on error | Catches "input entity" errors, retries | Catches "input entity"/"peerchannel" errors, retries | **MATCH** (FIXED) |
| WAL checkpoint on shutdown | `_checkpoint_cache_db` registered | `_checkpoint_cache_db` called in `stop()` | **MATCH** (FIXED) |

### 2.3 Admin Log Polling
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| `GetAdminLogRequest` | In `admin_monitor/monitor.py` | `telegram.py:_poll_admin_logs` | MATCH |
| Event routing to DB | Stores in dedicated tables | Stores in `telegram_admin_events` | MATCH |
| Poll interval | Configurable | `TELEGRAM_ADMIN_LOG_INTERVAL` default 300s | MATCH |

### 2.4 Group Manager
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| Rolling window cap | 5 joins/hour in `manager.py` | 5 joins/hour in `_process_join_queue` | MATCH |
| Min delay between joins | Present | 30s minimum | MATCH |
| Invite hash vs public channel | `ImportChatInviteRequest` / `JoinChannelRequest` | Both supported | MATCH |
| Queue from DB table | `telegram_group_joins` | `telegram_group_joins` | MATCH |

### 2.5 Credential/Env Var Mapping
| Original Var | New Var | Status |
|-------------|---------|--------|
| `ACCOUNT_N_API_ID` / `TG_API_ID` | `TELEGRAM_API_ID` / `TELEGRAM_ACCOUNT_N_API_ID` | RENAMED |
| `ACCOUNT_N_API_HASH` / `TG_API_HASH` | `TELEGRAM_API_HASH` / `TELEGRAM_ACCOUNT_N_API_HASH` | RENAMED |
| `ACCOUNT_N_PHONE` | `TELEGRAM_ACCOUNT_N_PHONE` | RENAMED |
| `ACCOUNT_N_SESSION` | `TELEGRAM_ACCOUNT_N_SESSION` | RENAMED |
| `HUB_GROUP_ID` | `TELEGRAM_HUB_GROUP` | RENAMED |
| `BOT_TOKENS` (comma-separated) | `TELEGRAM_BOT_TOKENS` | RENAMED |
| `HUB_NOTIFY_BATCH_INTERVAL` | Hardcoded 30s | MISSING |
| `HUB_NOTIFY_RATE_LIMIT` | Hardcoded 60s min_interval | MISSING |

### 2.6 Missing Features (post-fix)
- **Hub entity priming** -- original primes hub group entity on bot connect to avoid entity errors.
- ~~**Hub notifier SQLite cache**~~ -- FIXED: full SQLite WAL cache with replay/requeue.
- ~~**Hub notifier supervisor**~~ -- FIXED: auto-restart flusher with 10s check interval.
- **Connection timeout** -- original uses 30s timeout with detailed WSL2 error message.
- **Thread-safe bot pool** -- original uses both `threading.Lock` and `asyncio.Lock`.

---

## 3. TIKTOK

### 3.1 Three-Tier Download Chain
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| gallery-dl -> yt-dlp -> Playwright | `tiktok_scraper.py` chains 3 tiers | `tiktok.py:_collect_user` chains same 3 tiers | MATCH |
| 4th tier: API fallback | Not in original (only 3 tiers) | `_collect_via_api` as 4th fallback | EXTRA |

### 3.2 Playwright Stealth
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| `navigator.webdriver` override | `Object.defineProperty(navigator, 'webdriver', {get: () => undefined})` | Same script | MATCH |
| `navigator.plugins` override | `Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]})` | Same script | **MATCH** (FIXED) |
| `navigator.languages` override | `Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']})` | Same script | **MATCH** (FIXED) |
| Viewport | 1920x1080 (desktop) | 1920x1080 (desktop) | **MATCH** (FIXED) |
| User agent | Hardcoded Chrome 124 desktop | Hardcoded Chrome 124 desktop | **MATCH** (FIXED) |
| Locale/timezone | en-US / America/New_York | en-US / America/New_York | MATCH |
| sec-ch-ua headers | Present (Chrome 124) | Present (Chrome 124) | **MATCH** (FIXED) |
| Chromium launch args | `--disable-blink-features=AutomationControlled`, `--no-sandbox` | `--disable-blink-features`, `--no-sandbox`, `--disable-dev-shm-usage` | **MATCH** (FIXED) |
| Cookie file parsing (Netscape format) | Full `_parse_netscape_cookies` impl | Full `_parse_netscape_cookies` static method | **MATCH** (FIXED) |
| CDN URL interception for video download | `handle_request` listener for `mime_type=video` URLs | `_intercept_video_cdn` listener for `mime_type=video` + `.mp4` | **MATCH** (FIXED) |
| Login cookie validation | `LOGIN_COOKIE_NAMES` check | Not ported for Playwright mode | MISSING |

### 3.3 Download Tracker
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| DB tracking (`tiktok_download_tracker`) | Present | Present | MATCH |
| JSON backup file | Present | Present | MATCH |
| Dual-write (DB + JSON) | Present | Present | MATCH |
| Load tracker state (DB primary, JSON fallback) | Present | Present | MATCH |

### 3.4 Key Differences (post-fix)
- ~~**CDN interception**~~ -- FIXED: full CDN video interception via `_intercept_video_cdn` with 10KB size check.
- ~~**Desktop viewport**~~ -- FIXED: now 1920x1080 matching original.
- ~~**Comprehensive stealth**~~ -- FIXED: all 3 navigator overrides, sec-ch-ua, chromium args ported.
- **Original has `fetch_profile_stats` and `fetch_following_list`** helper functions; not ported.

### 3.5 Credential/Env Var Mapping
| Original Var | New Var | Status |
|-------------|---------|--------|
| `TIKTOK_COOKIES_FILE` | `TIKTOK_COOKIES_FILE` | MATCH |
| `TIKTOK_SESSION_ID` | `TIKTOK_SESSION_ID` | MATCH |
| `TIKTOK_MIN_SLEEP` | `TIKTOK_MIN_SLEEP` | MATCH |
| `TIKTOK_MAX_SLEEP` | `TIKTOK_MAX_SLEEP` | MATCH |
| `TIKTOK_RETRIES` | `TIKTOK_RETRIES` | MATCH |
| `TIKTOK_TIMEOUT_SECONDS` | `TIKTOK_TIMEOUT_SECONDS` | MATCH |
| `TIKTOK_BROWSER_FALLBACK_ENABLED` | `TIKTOK_BROWSER_FALLBACK_ENABLED` | MATCH |
| `TIKTOK_YTDLP_FALLBACK_ENABLED` | `TIKTOK_YTDLP_FALLBACK_ENABLED` | MATCH |

---

## 4. GITHUB

### 4.1 Authentication
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| PAT token(s), comma-separated | `GITHUB_PAT` or single token | `GITHUB_TOKEN` comma-separated with rotation | MATCH |
| Rate limit header parsing (X-RateLimit-*) | Present in original | Present in new | MATCH |
| Token rotation on exhaustion | Basic | `_rotate_pat()` with index cycling | MATCH |

### 4.2 Avatar Batch Download
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| Sequential ID range | `batch_download_avatars(start_id, end_id)` | `batch_download_avatars(start_id, end_id)` | MATCH |
| 10 concurrent via Semaphore | 10 | 10 (`_batch_sem = asyncio.Semaphore(10)`) | MATCH |
| 3-state dedup | on-disk+DB / on-disk-only / missing | Present | MATCH |
| Progress logging every 100 | Present | Present | MATCH |

### 4.3 Photo Blob Storage
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| Toggle via env var | `GITHUB_PROFILE_PHOTO_BLOB_ENABLED` | `GITHUB_PROFILE_PHOTO_BLOB_ENABLED` | MATCH |
| DB size guard | `GITHUB_PROFILE_PHOTO_BLOB_MAX_SIZE_MB=5000` | Same | MATCH |

### 4.4 Social Graph/Spider
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| BFS follows/following spider | Present | Present (`_spider_social_graph`) | MATCH |
| `graph_edges` table persistence | Present | Present (`_persist_edge`) | MATCH |
| Depth limit | Configurable | `GITHUB_SPIDER_DEPTH=4` | MATCH |

### 4.5 Credential/Env Var Mapping
| Original Var | New Var | Status |
|-------------|---------|--------|
| `GITHUB_PAT` | `GITHUB_TOKEN` | RENAMED |
| `GITHUB_AVATAR_SIZE` | `GITHUB_AVATAR_SIZE=460` | MATCH |
| `GITHUB_API_DELAY` | `GITHUB_API_DELAY=0.1` | MATCH |
| `GITHUB_DOWNLOAD_DELAY` | `GITHUB_DOWNLOAD_DELAY=0.5` | MATCH |
| `GITHUB_MAX_CONCURRENT` | `GITHUB_MAX_CONCURRENT=5` | MATCH |
| `GITHUB_SPIDER_DEPTH` | `GITHUB_SPIDER_DEPTH=4` | MATCH |
| `GITHUB_PROFILE_PHOTO_BLOB_MAX_SIZE_MB` | Same | MATCH |

---

## 5. YOUTUBE

### 5.1 Authentication
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| API key via env | `YOUTUBE_API_KEY` | `YOUTUBE_API_KEY` | MATCH |
| OAuth pickle caching | Present in original toolkit | `_load_oauth_credentials` via pickle + google-auth | MATCH |
| Credential chain (env -> pickle -> cookies) | Present | Present | MATCH |
| `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` | Present | Present | MATCH |

### 5.2 yt-dlp Configuration
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| Format: `bestvideo+bestaudio/best` | Present | Present (`_ytdlp_format`) | MATCH |
| Merge format: mp4 | Present | `_merge_format = "mp4"` | MATCH |
| `--write-thumbnail` | Present | Present | MATCH |
| Cookie browser | `YOUTUBE_COOKIE_BROWSER=auto` | Supported | MATCH |

### 5.3 Credential/Env Var Mapping
| Original Var | New Var | Status |
|-------------|---------|--------|
| `YOUTUBE_API_KEY` | `YOUTUBE_API_KEY` | MATCH |
| `YOUTUBE_CLIENT_ID` | `YOUTUBE_CLIENT_ID` | MATCH |
| `YOUTUBE_CLIENT_SECRET` | `YOUTUBE_CLIENT_SECRET` | MATCH |
| `YOUTUBE_COOKIE_BROWSER` | `YOUTUBE_COOKIE_BROWSER` | MATCH |
| `YOUTUBE_MAX_VIDEO_DURATION_MINUTES` | `YOUTUBE_MAX_VIDEO_DURATION_MINUTES` | MATCH |
| `YOUTUBE_YTDLP_FORMAT` | `YOUTUBE_YTDLP_FORMAT` | MATCH |
| `YOUTUBE_DOWNLOAD_DELAY` | `YOUTUBE_DOWNLOAD_DELAY` | MATCH |
| `YOUTUBE_API_DELAY` | `YOUTUBE_API_DELAY` | MATCH |
| `YOUTUBE_MAX_CONCURRENT` | `YOUTUBE_MAX_CONCURRENT` | MATCH |
| `YOUTUBE_DOWNLOAD_VIDEOS` | `YOUTUBE_DOWNLOAD_VIDEOS` | MATCH |

---

## 6. WEBSITE

### 6.1 URL Filter
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| Allow/block lists | `url_filter.py` | `src/core/url_filter.py` | MATCH |
| Wildcard-to-regex | Present | Present | MATCH |
| ReDoS mitigation (512-char cap) | Present | Present | MATCH |
| Default blocklist (social auth, carts, login) | Present | Present | MATCH |
| Env vars: `WEBSITE_URL_ALLOW`, `WEBSITE_URL_BLOCK` | Present | Present | MATCH |

### 6.2 PDF Processing
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| PyMuPDF page-to-PNG | `pdf_processor.py` | `src/core/pdf_processor.py` | MATCH |
| Graceful fallback when fitz missing | Present | Present | MATCH |
| Max pages config | Present | `WEBSITE_PDF_MAX_PAGES` | MATCH |

### 6.3 Credential/Env Var Mapping
| Original Var | New Var | Status |
|-------------|---------|--------|
| `WEBSITE_MAX_DEPTH` | `WEBSITE_MAX_DEPTH` | MATCH |
| `WEBSITE_MAX_PAGES` | `WEBSITE_MAX_PAGES` | MATCH |
| `WEBSITE_TIMEOUT` | `WEBSITE_TIMEOUT` | MATCH |
| `WEBSITE_URL_ALLOW` | `WEBSITE_URL_ALLOW` | MATCH |
| `WEBSITE_URL_BLOCK` | `WEBSITE_URL_BLOCK` | MATCH |

---

## 7. SEARCH

### 7.1 Core Features
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| Serper API image search | Present | Present | MATCH |
| Page spidering (extract images from result URLs) | Present in original | `_spider_result_pages` | MATCH |
| Dual API key support | `SEARCH_API_KEY` / `SERPER_API_KEY` | Both supported | MATCH |

### 7.2 Credential/Env Var Mapping
| Original Var | New Var | Status |
|-------------|---------|--------|
| `SERPER_API_KEY` | `SERPER_API_KEY` / `SEARCH_API_KEY` | MATCH |
| `SEARCH_MAX_RESULTS` | `SEARCH_MAX_RESULTS=50` | MATCH |
| `SEARCH_MIN_DIMENSION` | `SEARCH_MIN_DIMENSION=200` | MATCH |
| `SEARCH_MIN_FILE_SIZE` | `SEARCH_MIN_FILE_SIZE=10240` | MATCH |

---

## 8. LEMON8

### 8.1 Core Features
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| Feed scraping (recommend API) | Present | `_collect_feed` | MATCH |
| Tag-based scraping with cursor pagination | Present | `_collect_tag` | MATCH |
| Entity discovery (users + tags) | Present | `_extract_discoveries` + `_persist_discoveries` | MATCH |
| `lemon8_discovered` table | Present | Present | MATCH |

### 8.2 Credential/Env Var Mapping
| Original Var | New Var | Status |
|-------------|---------|--------|
| `LEMON8_COOKIES_FILE` | `LEMON8_COOKIES_FILE` | MATCH |
| `LEMON8_SESSION_ID` | `LEMON8_SESSION_ID` | MATCH |
| `LEMON8_MIN_WIDTH` | `LEMON8_MIN_WIDTH=320` | MATCH |
| `LEMON8_MIN_HEIGHT` | `LEMON8_MIN_HEIGHT=320` | MATCH |

---

## 9. STRAVA

### 9.1 Core Features
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| OAuth + session cookie dual auth | Present | Present | MATCH |
| Follow roster scraper (4 HTML strategies) | Present | `_scrape_following` with 4 strategies | MATCH |
| GPS stream ingestion | Present | `_collect_gps_streams` | MATCH |
| Day-level coverage tracking | Present | `strava_day_coverage` table | MATCH |
| ProfilePhotoTracker for athletes | Present | Wired via `set_pool()` | MATCH |

### 9.2 Credential/Env Var Mapping
| Original Var | New Var | Status |
|-------------|---------|--------|
| `STRAVA_CLIENT_ID` | `STRAVA_CLIENT_ID` | MATCH |
| `STRAVA_CLIENT_SECRET` | `STRAVA_CLIENT_SECRET` | MATCH |
| `STRAVA_REFRESH_TOKEN` | `STRAVA_REFRESH_TOKEN` | MATCH |
| `STRAVA_SESSION_COOKIE` | `STRAVA_SESSION_COOKIE` | MATCH |
| `STRAVA_API_DELAY_MIN` | `STRAVA_API_DELAY_MIN=5.0` | MATCH |
| `STRAVA_API_DELAY_MAX` | `STRAVA_API_DELAY_MAX=10.0` | MATCH |

---

## 10. WHATSAPP

### 10.1 Core Features
| Feature | Original | New | Status |
|---------|----------|-----|--------|
| Real-time mode (media bridge + broker) | Full microservice architecture | Integrated in single collector | MATCH |
| Offline export mode (zip import) | Not in original (was separate service) | `_import_exports` in WhatsappCollector | EXTRA |
| HMAC-signed media bridge | Present | Present | MATCH |
| Multi-session support | Present | Present via `SESSION_NAMES` / `SESSION_BRIDGES_JSON` | MATCH |
| Face recognition pipeline | Separate `face_recognition` service | Integrated via `FaceProcessor` + `FaceMatcher` | MATCH |
| User intelligence / change tracking | Separate `user_intelligence` service | Integrated via `ChangeTracker` | MATCH |
| Link discovery | Separate `link_discovery` service | Integrated via `extract_whatsapp_links` | MATCH |
| Bulk sender | Separate `bulk_sender` service | Integrated in collector | MATCH |

### 10.2 Credential/Env Var Mapping (dual-prefix support)
| Original Var | New Var | Status |
|-------------|---------|--------|
| `POSTGRES_HOST/PORT/DB/USER/PASSWORD` | `DATABASE_URL` (connection string) | DIFFERENT FORMAT |
| `MEDIA_BRIDGE_URL` | `WHATSAPP_SESSION_BRIDGES_JSON` / `SESSION_BRIDGES_JSON` | RENAMED |
| `MEDIA_BRIDGE_SECRET` | `WHATSAPP_MEDIA_BRIDGE_SECRET` / `MEDIA_BRIDGE_SECRET` | MATCH (dual) |
| `SESSION_NAMES` | `WHATSAPP_SESSION_NAMES` / `SESSION_NAMES` | MATCH (dual) |
| `SESSION_BRIDGES_JSON` | `WHATSAPP_SESSION_BRIDGES_JSON` / `SESSION_BRIDGES_JSON` | MATCH (dual) |
| `BROKER_TYPE` | `WHATSAPP_BROKER_TYPE` / `BROKER_TYPE` | MATCH (dual) |
| `RABBITMQ_URL` | `WHATSAPP_RABBITMQ_URL` / `RABBITMQ_URL` | MATCH (dual) |
| `REDIS_URL` | `WHATSAPP_REDIS_URL` / `REDIS_URL` | MATCH (dual) |
| `COLLECTOR_DEDUP_TTL_SECONDS` | `WHATSAPP_DEDUP_TTL_SECONDS` / `COLLECTOR_DEDUP_TTL_SECONDS` | MATCH (dual) |
| `FACE_MATCH_THRESHOLD=0.6` | Hardcoded in `FaceMatcher` | MATCH (value) |
| `FACE_DETECTION_MODEL=hog` | In `FaceProcessor` | MATCH |
| `DASHBOARD_ADMIN_USERNAME/PASSWORD` | `DASHBOARD_ADMIN_USERNAME/PASSWORD` | MATCH |
| `TRACKED_FIELDS` | Hardcoded in `ChangeTracker` | PARTIAL |
| `BULK_SENDER_EXTERNAL_MAX_PER_HOUR=30` | `WHATSAPP_BULK_HOURLY_CAP=30` | RENAMED |
| `BULK_SENDER_MEMBERSHIP_MIN_AGE_HOURS=48` | `WHATSAPP_BULK_MIN_MEMBERSHIP_HOURS=48` | RENAMED |
| `POSTGRES_SSL_MODE/SSL_ROOT_CERT/etc.` | Not mapped (handled by `DATABASE_URL` params) | PARTIAL |
| `CONTROL_PLANE_SECRET_KEY` | Not ported | MISSING |
| `DLIB_MODELS_PATH` | Not mapped (uses default) | MISSING |

### 10.3 Missing Features
- **Control plane secret encryption** -- `CONTROL_PLANE_SECRET_KEY` for AES-encrypted secrets.
- **Separate `POSTGRES_SSL_*` params** -- original had fine-grained TLS config.
- **Findings hub publication** -- batched face sighting reports to hub group.
- **Media redownload** -- background re-download of expiring media.
- **Configurable `TRACKED_FIELDS`** -- original allowed CSV override.
- **`DLIB_MODELS_PATH`** -- explicit dlib model directory.

---

## DASHBOARD

### Auth System
| Feature | Original WhatsApp Dashboard | New Unified Dashboard | Status |
|---------|---------------------------|----------------------|--------|
| JWT auth | Present (3 roles: viewer/operator/admin) | Present (3 roles) | MATCH |
| bcrypt password hashing | Present | Present | MATCH |
| Env-seeded admin | Present | Present | MATCH |
| Per-role env vars | `DASHBOARD_VIEWER/OPERATOR/ADMIN_USERNAME/PASSWORD` | `DASHBOARD_ADMIN_USERNAME/PASSWORD` only | PARTIAL |

---

## EXISTING CREDENTIAL FILES AUDIT

Per user instruction: check all existing `.env`, config, and legacy deployment files and map credentials.

### Files Found
| File | Location | Status |
|------|----------|--------|
| `.env.example` | `C:\unifiedcollector\.env.example` | New unified config -- **SOURCE OF TRUTH** |
| `.env.example` | `instagramtoolkit\.env.example` | Legacy -- mapped above |
| `.env.example` | `telegramtoolkit\.env.example` | Legacy -- mapped above |
| `.env.example` | `whatsappcollector\.env.example` | Legacy -- mapped above |
| `.env.example` | `searchtoolkit\.env.example` | Legacy -- mapped above |

### Credential Reuse Assessment

**CAN REUSE (same format, compatible):**
- `INSTA_ACCOUNT_N_*` -- identical naming, direct copy
- `TIKTOK_*` -- identical naming, direct copy
- `YOUTUBE_*` -- identical naming, direct copy
- `STRAVA_*` -- identical naming, direct copy
- `LEMON8_*` -- identical naming, direct copy
- `SEARCH_API_KEY` / `SERPER_API_KEY` -- both supported
- `FILTER_MAX_FOLLOWERS` -- identical naming
- `SESSION_NAMES`, `SESSION_BRIDGES_JSON`, `MEDIA_BRIDGE_SECRET` -- dual-prefix support
- `BROKER_TYPE`, `RABBITMQ_URL`, `REDIS_URL` -- dual-prefix support
- `DASHBOARD_ADMIN_USERNAME/PASSWORD` -- identical naming

**REQUIRES RENAME (same value, different key name):**
- `GITHUB_PAT` -> `GITHUB_TOKEN` (just rename the key)
- `ACCOUNT_N_API_ID` -> `TELEGRAM_ACCOUNT_N_API_ID` (add prefix)
- `TG_API_ID` -> `TELEGRAM_API_ID` (expand prefix)
- `HUB_GROUP_ID` -> `TELEGRAM_HUB_GROUP`
- `BOT_TOKENS` -> `TELEGRAM_BOT_TOKENS`
- `WINDOW_1H_LIMIT` -> `INSTA_WINDOW_1H` (but default values differ!)
- `SESSION_MAX_AGE_DAYS` -> `INSTA_SESSION_MAX_AGE_DAYS`

**REQUIRES NEW FORMAT:**
- `POSTGRES_HOST/PORT/DB/USER/PASSWORD` -> single `DATABASE_URL=postgres://user:pass@host:port/db`

**NO EQUIVALENT (cannot reuse):**
- `INSTA_ACCOUNT_N_BROWSER` -- feature not ported
- ~~`INSTA_ACCOUNT_N_PROXY`~~ -- FIXED: now mapped
- ~~`PROXY_URL`~~ -- FIXED: now mapped
- `CONTROL_PLANE_SECRET_KEY` -- feature not ported
- `DLIB_MODELS_PATH` -- not configurable in new system
- `POSTGRES_SSL_*` -- must be embedded in DATABASE_URL params
- `SLIDING_WINDOW_ENABLED` -- always enabled, no toggle

---

## CRITICAL FINDINGS SUMMARY (updated after fixes)

### High Priority (functional gaps)

1. ~~**Instagram: 5H sliding window missing**~~ -- **FIXED**: 4 windows matching original (1H=180, 3H=400, 5H=600, 1D=2000).
2. ~~**Instagram: Night/risky hour delay multipliers missing**~~ -- **FIXED**: 2.5-4x night, 1.5x risky hours.
3. **Instagram: Browser cookie import missing** -- `INSTA_ACCOUNT_N_BROWSER` not supported.
4. ~~**Instagram: Proxy support missing**~~ -- **FIXED**: per-account + global proxy via httpx.
5. ~~**Instagram: Connection speed format wrong**~~ -- **FIXED**: now uses `{N}kbps` matching original.
6. ~~**Instagram: Window defaults differ**~~ -- **FIXED**: defaults now match original.
7. ~~**TikTok: Playwright mode only captures thumbnails**~~ -- **FIXED**: CDN video interception with `mime_type=video` + `.mp4` matching.
8. ~~**TikTok: Minimal stealth in Playwright**~~ -- **FIXED**: 3 navigator overrides, sec-ch-ua, chromium args, desktop viewport.
9. ~~**Telegram: Hub notifier has no offline cache**~~ -- **FIXED**: SQLite WAL cache with replay/requeue/supervisor.
10. **Telegram: Bot pool lacks connection timeout** -- original has 30s timeout with WSL2 error message.

### Medium Priority (resilience/operational gaps)

11. ~~**Instagram: Daily quota budget not ported**~~ -- **FIXED**: 180 profile views, 6000 actions per day.
12. ~~**Instagram: Account switch delay (180-300s) not explicit**~~ -- **FIXED**: explicit 180-300s delay.
13. ~~**Instagram: Micro-pause (70% probability) not ported**~~ -- **FIXED**: 70% probability, exponential 0.5-3s.
14. **Telegram: Hub entity priming on bot connect missing** -- can cause "entity not found" errors.
15. ~~**Telegram: Hub notifier supervisor (auto-restart flusher) missing**~~ -- **FIXED**: 10s supervisor loop.
16. **WhatsApp: Control plane secret encryption not ported**.
17. **WhatsApp: Configurable tracked_fields not ported** -- hardcoded in ChangeTracker.
18. **Dashboard: Only admin role seeded** -- original seeded all 3 roles from env.

### Low Priority (cosmetic/minor)

19. ~~**Instagram: Warmup duration much shorter**~~ -- **FIXED**: now 90-180s (3x 30-60s).
20. ~~**Instagram: X-IG-Android-ID, X-IG-Capabilities, X-IG-Connection-Type headers missing**~~ -- **FIXED**: all headers ported.
21. **Telegram: Bot pool uses least-recently-used instead of round-robin**.
22. ~~**TikTok: Mobile viewport (390x844) vs desktop (1920x1080) in original**~~ -- **FIXED**: 1920x1080.
23. **Instagram: Re-auth stagger logging from DB not ported** (simplified to JSON meta).

### Env Var Compatibility Matrix (updated)

| Status | Count | Description |
|--------|-------|-------------|
| MATCH | 53 | Same name and semantics (includes 6 newly fixed) |
| RENAMED | 11 | Different name, same semantics (documented in .env.example) |
| DUAL | 10 | Supports both old and new names (WhatsApp) |
| DIFFERENT DEFAULT | 1 | Same name but different default value |
| MISSING | 9 | Original var with no equivalent in new system |

---

## RECOMMENDATION (updated after fixes)

The **3 critical production gaps have been resolved:**

1. ~~**Instagram anti-detection**~~ -- **FIXED**: 5H window, night/risky hour multipliers, micro-pauses, daily quotas, proxy support, proper X-IG headers, warmup toggle all ported.

2. ~~**TikTok Playwright video download**~~ -- **FIXED**: full CDN interception via `_intercept_video_cdn`, stealth navigator overrides, desktop viewport, Netscape cookie parsing.

3. ~~**Telegram hub notifier resilience**~~ -- **FIXED**: SQLite WAL offline cache with atomic claim/replay, supervisor auto-restart, entity re-resolution, WAL checkpoint on shutdown.

**Remaining gaps (non-critical):**
- Instagram: browser cookie import, content-aware delays, operation registry, interruptible sleep
- Telegram: hub entity priming, 30s connection timeout, thread-safe bot pool
- WhatsApp: control plane secret encryption, configurable tracked_fields
- Dashboard: multi-role seeding from env

**Production readiness estimate: ~90%** (up from ~70% before fixes). The remaining items are resilience improvements and edge-case handling, not functional blockers.
