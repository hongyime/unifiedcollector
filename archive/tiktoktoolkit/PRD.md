# Product Requirements Document
## Unified TikTok Download Toolkit

---

### 1. Executive Summary

**Value Proposition:**  
A multi-provider, fallback-aware TikTok content download utility that prioritizes reliability through a tiered provider chain (gallery-dl → yt-dlp → browser automation), while maintaining download idempotency via SQLite + JSON tracking and protecting sensitive cookie data with Windows ACL hardening.

**Target Users:**  
- Developers and data engineers who need to archive TikTok content programmatically  
- Researchers requiring batch collection of public video data  
- Power users who want an offline-capable, privacy-respecting alternative to web scrapers

---

### 2. System Architecture

#### 2.1 Component Map

```
CLI Entry (main.py / click)
        │
        ▼
   Provider Layer
   ┌─────────────────────────────────────────┐
   │  GalleryDLProvider                      │
   │    ├── gallery-dl (primary)            │
   │    ├── yt-dlp fallback                 │
   │    └── BrowserDownloader fallback       │
   └─────────────────────────────────────────┘
        │
        ▼
   ┌──────────┐   ┌───────────────┐
   │ Tracker  │   │ CookieManager │
   │ (SQLite) │   │ (validation)  │
   └──────────┘   └───────────────┘
        │
        ▼
   Filesystem Output
   downloads/username_<user>/<video_id>.<ext>
```

#### 2.2 Data Flow

1. **CLI parse** → `create_provider()` selects `GalleryDLProvider`
2. **Precheck** → `_tracker_precheck()` queries SQLite; skips already-downloaded
3. **Gallery-dl** → `_run_gallery_dl()` with flat layout normalization
4. **Fallback chain** → If gallery-dl fails, `_download_with_ytdlp_fallback()` → `_download_with_browser_fallback()`
5. **Post-download** → `_normalize_downloaded_files()` flattens date subfolders; `_collect_media_files()` registers files in tracker
6. **Cookie setup** → `setup_browser_cookies()` tries gallery-dl → rookiepy sequentially

#### 2.3 Key Data Models

| Model | Fields | Purpose |
|-------|--------|---------|
| `DownloadResult` | `ok`, `url`, `status`, `filepath`, `reason`, `meta` | Per-video operation result |
| `AppConfig` | `output_root`, `log_level`, `cookies_file`, `cookies_browser`, `providers` | App-wide settings |
| SQLite `urls` table | `url`, `user`, `video_id`, `status`, `downloaded_at`, `file_size` | Download history |
| SQLite `files` table | `video_id`, `filepath`, `size`, `md5` | File registry |

#### 2.4 Output Layout

```
downloads/
  username_<user>/
    <video_id>.mp4      # flat layout (canonical)
    <video_id>.jpg      # thumbnail (if captured)
```

---

### 3. Feature Matrix

| Feature | Status | Implementation |
|---------|--------|----------------|
| Download by username | ✅ Production | `GalleryDLProvider.download_user()` |
| Idempotent downloads | ✅ Production | SQLite tracker + precheck |
| Flat output layout | ✅ Production | `_normalize_downloaded_files()` |
| Gallery-dl primary provider | ✅ Production | `_run_gallery_dl()` |
| yt-dlp fallback | ✅ Production | `_download_with_ytdlp_fallback()` |
| Browser automation fallback | ✅ Production | `BrowserDownloader` class |
| Cookie extraction (gallery-dl) | ✅ Production | `setup_browser_cookies()` |
| Cookie extraction (rookiepy) | ✅ Production | `setup_browser_cookies()` |
| Cookie validation | ✅ Production | `TikTokCookieManager.validate_cookies()` |
| Windows ACL hardening | ✅ Production | `secure_file_permissions()` via `icacls` |
| Backup before destructive ops | ✅ Production | `create_backup()` utility |
| Duplicate detection | ✅ Production | `find-duplicates` CLI command |
| Date subfolder flattening | ✅ Production | `find-duplicates` + `_normalize_downloaded_files()` |
| JSON + SQLite tracking | ✅ Production | `DownloadTracker` dual-backend |
| CLI with click | ✅ Production | `src/cli.py` with 10+ commands |
| Unit/Integration/Property tests | ✅ Production | 136 passing tests |
| Property-based testing | ✅ Production | Hypothesis + `@given` decorators |

---

### 4. Security & Performance

#### 4.1 Security

| Mechanism | Details |
|-----------|---------|
| Cookie file permissions | `icacls`-based ACL (Windows); `chmod 0o600` (Unix) |
| `.env` handling | Loaded via `python-dotenv`; never hardcoded |
| Backup before destructive ops | Auto-creates `configs/backup_YYYYMMDD_HHMMSS/` |
| SQLite WAL mode | Enables concurrent reads; safe for multi-process |

#### 4.2 Performance

| Metric | Target | Implementation |
|--------|--------|----------------|
| Precheck speed | <100ms per user | Indexed SQLite query on `user` column |
| Cookie extraction | <120s timeout | `timeout=120` for gallery-dl |
| Rate limiting | Configurable | `MIN_SLEEP` / `MAX_SLEEP` env vars |

---

### 5. Non-Functional Requirements

#### 5.1 Error Handling
- Gallery-dl non-zero exit codes → preserved in `ProviderError` message
- TimeoutExpired → logged as warning, triggers fallback chain
- Browser redirect detection → returns failed `DownloadResult` with reason
- Missing cookie file → logged at WARNING level, continues without auth

#### 5.2 Logging
- Module-level loggers (`uttk.provider`, `uttk.utils`, `uttk.tracker`)
- Configurable via `LOG_LEVEL` env var (default: INFO)
- Log rotation: `logs/uttk.log` + `.1`, `.2`, `.3` archives

#### 5.3 Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `TIMEOUT` | 15 | HTTP request timeout (seconds) |
| `RETRIES` | 2 | Max retries per provider attempt |
| `OUTPUT_ROOT` | downloads | Root directory for downloads |
| `LOG_LEVEL` | INFO | Logging verbosity |
| `USER_AGENT` | (empty) | Custom User-Agent string |
| `ACCEPT_LANGUAGE` | en-US,en;q=0.9 | Accept-Language header |
| `MIN_SLEEP` | 0.5 | Minimum delay between requests |
| `MAX_SLEEP` | 2.0 | Maximum delay between requests |

---

### 6. Project Tree

```
tiktoktoolkit/
├── main.py                 # CLI entry point
├── requirements.txt       # Python dependencies
├── setup.py               # Package setup
├── setup.bat              # Environment setup script
├── start_toolkit.bat      # Toolkit launcher
├── pytest.ini             # Pytest configuration
├── pyrightconfig.json     # Pyright static analysis config
├── .env                   # Environment variables (gitignored)
├── .gitignore
├── AUDIT.md               # Audit report and execution log
├── PRD.md                  # This document
├── README.md               # Developer documentation
├── configs/
│   ├── config.yaml         # App configuration
│   ├── providers.yaml     # Provider-specific settings
│   └── tiktok_cookies.txt # Browser cookie file
├── src/
│   ├── __init__.py
│   ├── browser_downloader.py   # Playwright fallback
│   ├── cli.py                  # Click CLI commands
│   ├── config.py               # YAML config loader
│   ├── cookie_manager.py       # Cookie validation
│   ├── download_path_manager.py
│   ├── downloader.py           # Base downloader interface
│   ├── errors.py               # Custom exceptions
│   ├── logging_setup.py        # Log rotation
│   ├── models.py               # DownloadResult dataclass
│   ├── provider.py             # GalleryDLProvider (main logic)
│   ├── tracker.py              # SQLite + JSON tracker
│   ├── utils.py                # Shared utilities
│   ├── validation.py           # URL/username validation
│   └── ytdlp_downloader.py     # yt-dlp wrapper
├── scripts/
│   ├── diagnostics/
│   │   ├── check_cookies.py
│   │   ├── debug_gallery_dl.py
│   │   ├── diagnose.bat
│   │   ├── verify_dedup.py
│   │   └── verify_production.bat
│   ├── migrations/
│   │   └── migrate_downloads.py
│   └── ops/
│       ├── refresh_cookies.bat
│       ├── test_fallback.bat
│       └── test_idempotency.bat
├── tests/
│   ├── integration/
│   │   ├── test_antibot_fallback.py
│   │   ├── test_browser_downloader.py
│   │   ├── test_cookie_manager.py
│   │   └── test_provider.py
│   ├── property/
│   │   ├── test_gallery_dl_timeout_fix.py
│   │   ├── test_tiktok_cookie_bug_condition.py
│   │   ├── test_tiktok_cookie_preservation.py
│   │   ├── test_ytdlp_fallback_bug_condition.py
│   │   └── test_ytdlp_fallback_preservation.py
│   └── unit/
│       ├── test_cli.py
│       ├── test_config.py
│       ├── test_tracker.py
│       └── test_utils.py
├── data/
│   └── usernames.txt      # Batch username list
└── downloads/             # Output directory (gitignored)
```
