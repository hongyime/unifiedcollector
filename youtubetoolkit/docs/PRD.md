# Product Requirements Document
**Project:** YouTube Toolkit  
**Version:** 2.0 (Post-Hardening)  
**Generated:** 2026-04-26  
**Status:** Production-Ready (Hardened)

---

## 1. Executive Summary

The YouTube Toolkit is a local, Windows-first Python application that implements a structured **scrape → queue → download** lifecycle for YouTube content. It enables users to collect video URLs from multiple sources (liked videos, subscriptions, target channels, custom playlists), persist them in a SQLite-backed queue, and batch-download either full videos or channel profile photos using `yt-dlp`.

The system is designed for personal, offline archival use. It requires no persistent server process — all operations are invoked interactively via a Windows batch launcher or directly via CLI. Authentication to the YouTube Data API v3 is handled through Google OAuth 2.0 with local credential caching.

**Value Proposition:**
- Unified queue across multiple YouTube sources with deduplication
- Interrupted-download recovery via startup cleanup and resume logic
- No API key required for target-channel and custom-URL scraping paths
- Fully local — no cloud dependency beyond the YouTube API itself

---

## 2. System Architecture

### 2.1 Component Map

```
┌─────────────────────────────────────────────────────────────┐
│                    start_toolkit.bat (Launcher)              │
│                    11-option interactive menu                │
└──────────────────────────┬──────────────────────────────────┘
                           │ invokes
        ┌──────────────────┼──────────────────────┐
        ▼                  ▼                       ▼
 ┌─────────────┐   ┌──────────────┐   ┌──────────────────────┐
 │  Scrapers   │   │  Batch       │   │  Utilities           │
 │             │   │  Downloader  │   │                      │
 │ liked_videos│   │  (scripts/   │   │ logout_account.py    │
 │ subs_proc.  │   │  batch_      │   │ validate_install.py  │
 │ scrape_tgts │   │  downloader) │   │ scrape_custom_       │
 │             │   │              │   │ playlist.py          │
 └──────┬──────┘   └──────┬───────┘   └──────────────────────┘
        │                 │
        ▼                 ▼
 ┌─────────────────────────────────────────────────────────────┐
 │              DatabaseManager (src/data_manager_streamlined) │
 │              SQLite: src/data/youtube_data.db               │
 │              JSON Backup: src/data/youtube_data.db.json     │
 └──────────────────────────┬──────────────────────────────────┘
                            │
                ┌───────────┴───────────┐
                ▼                       ▼
     ┌──────────────────┐   ┌──────────────────────┐
     │  video_processor │   │  auth_cache.py        │
     │  (yt-dlp engine) │   │  OAuth credential     │
     │  src/video_      │   │  pickle cache         │
     │  processor.py    │   └──────────────────────┘
     └──────────────────┘
```

### 2.2 Data Flow

1. **Scrape Phase** — One of four scrapers collects video URLs and metadata, then calls `DatabaseManager.batch_add_videos()`. Duplicates are silently skipped via `INSERT OR IGNORE`.
2. **Queue Phase** — All scraped URLs persist in the `videos` table with `download_status = 'pending'`.
3. **Download Phase** — `BatchDownloader` queries pending rows, calls `video_processor.download_youtube_video()` per URL, and updates the row to `completed` or `failed`.
4. **Recovery Phase** — On `DatabaseManager.__init__`, `cleanup_interrupted_downloads()` resets any rows stuck in `downloading` for more than one hour back to `pending`.
5. **Backup Phase** — After every write operation, `create_backup()` serializes the entire `videos` table to `src/data/youtube_data.db.json` for recovery.

### 2.3 Path Management

All mutable runtime state is isolated under `data/` (gitignored). The `src/app_paths.py` module is the single source of truth for all file paths. No hardcoded paths exist in any other module.

| Constant | Path | Purpose |
|---|---|---|
| `DATABASE_FILE` | `data/youtube_data.db` | Primary SQLite database |
| `DATABASE_BACKUP_FILE` | `data/youtube_data.db.json` | JSON recovery backup |
| `CONFIG_FILE` | `data/config.json` | Runtime configuration |
| `CLIENT_SECRET_FILE` | `data/client_secret.json` | OAuth app credentials |
| `OAUTH_CREDENTIALS_FILE` | `data/oauth_credentials.pickle` | Cached OAuth tokens |
| `SUBSCRIPTIONS_FILE` | `data/subscriptions.json` | 24-hour subscription cache |
| `TARGET_CHANNELS_FILE` | `data/target_channels.txt` | User-defined channel list |
| `SCRAPED_LINKS_FILE` | `data/all_scraped_links.txt` | Links extracted from descriptions |
| `DEFAULT_DOWNLOADS_DIR` | `data/downloads` | Default download destination |

---

## 3. Feature Matrix

### 3.1 Scraping Features

| Feature | Module | API Required | Status |
|---|---|---|---|
| Liked videos ingestion | `scripts/scrape_liked_videos_enhanced.py` | Yes (OAuth) | Implemented |
| Subscription channel scraping | `scripts/subscription_processor.py` | Yes (OAuth) | Implemented |
| Target channel scraping | `scripts/scrape_targets.py` | No (yt-dlp) | Implemented |
| Custom URL / playlist scraping | `scripts/scrape_custom_playlist.py` | No (yt-dlp) | Implemented |
| Day-range filtering (`--days N`) | All scrapers | — | Implemented |
| Subscription 24-hour cache | `subscription_processor.py` | — | Implemented |

### 3.2 Download Features

| Feature | Module | Status |
|---|---|---|
| Batch video download from queue | `scripts/batch_downloader.py` | Implemented |
| Channel profile photo download | `src/video_processor.py` | Implemented |
| Browser cookie authentication | `src/video_processor.py` | Implemented |
| Chrome / Edge multi-profile detection | `src/video_processor.py` | Implemented |
| Cookie file fallback | `src/video_processor.py` | Implemented |
| Duplicate detection (DB + disk) | `src/video_processor.py` | Implemented |
| Partial file cleanup | `src/video_processor.py` | Implemented |
| Interrupted download recovery | `src/video_processor.py` | Implemented |
| Description link extraction | `src/video_processor.py` | Implemented |
| Channel-filtered batch download | `scripts/batch_downloader.py` | Implemented |
| Retry failed downloads | `scripts/batch_downloader.py` | Implemented |
| Photos-only mode (dedup by channel) | `scripts/batch_downloader.py` | Implemented |
| Progress tracking with ETA | `scripts/batch_downloader.py` | Implemented |

### 3.3 Configuration Features

| Feature | Module | Status |
|---|---|---|
| Max resolution setting | `src/config.py` | Implemented |
| Audio-only mode | `src/config.py` | Implemented |
| Batch size control | `src/config.py` | Implemented |
| Dot-notation config access | `src/config.py` | Implemented |
| Auto-create missing config | `src/config.py` | Implemented |

### 3.4 Authentication Features

| Feature | Module | Status |
|---|---|---|
| Google OAuth 2.0 flow | `scripts/scrape_liked_videos_enhanced.py`, `scripts/subscription_processor.py` | Implemented |
| Credential pickle caching | `src/auth_cache.py` | Implemented |
| Legacy `token.json` migration | `src/auth_cache.py` | Implemented |
| Account logout / cache clear | `scripts/logout_account.py` | Implemented |

### 3.5 Database Features

| Feature | Module | Status |
|---|---|---|
| SQLite queue with deduplication | `src/data_manager_streamlined.py` | Implemented |
| JSON backup on every write | `src/data_manager_streamlined.py` | Implemented |
| Backup restore on startup | `src/data_manager_streamlined.py` | Implemented |
| Interrupted download cleanup | `src/data_manager_streamlined.py` | Implemented |
| Exponential backoff on DB lock | `src/data_manager_streamlined.py` | Implemented |
| ANSI code stripping in errors | `src/data_manager_streamlined.py` | Implemented |
| Video statistics reporting | `src/data_manager_streamlined.py` | Implemented |
| Scraped link persistence | `src/data_manager_streamlined.py` | Implemented |

---

## 4. Data Model

### 4.1 `videos` Table Schema

```sql
CREATE TABLE videos (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    url               TEXT UNIQUE NOT NULL,
    video_id          TEXT,
    title             TEXT,
    channel           TEXT,
    channel_id        TEXT,
    duration          INTEGER,          -- seconds
    status            TEXT DEFAULT 'pending',
    download_status   TEXT DEFAULT 'pending',
    file_path         TEXT,
    file_size         INTEGER,          -- bytes
    download_started  TIMESTAMP,
    download_completed TIMESTAMP,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at      TIMESTAMP,
    error_message     TEXT,
    metadata          TEXT,             -- JSON blob
    scraped_links     TEXT              -- JSON blob
);
```

**Indexes:** `idx_videos_status`, `idx_videos_download_status`, `idx_videos_url`, `idx_videos_video_id`, `idx_videos_channel_id`

### 4.2 Status State Machine

```
pending → downloading → completed
                     ↘ failed → pending (on retry)
```

`cleanup_interrupted_downloads()` resets rows stuck in `downloading` for more than one hour back to `pending` on every `DatabaseManager` initialization.

### 4.3 Configuration Schema (`data/config.json`)

```json
{
  "processing": {
    "batch_size": 10,
    "max_retries": 3,
    "timeout_seconds": 300
  },
  "output": {
    "base_folder": "",
    "organize_by_date": true
  },
  "download": {
    "max_resolution": "1080",
    "audio_only": false
  },
  "ui": {
    "show_progress": true,
    "colorful_output": true,
    "auto_cleanup": false
  },
  "api": {
    "youtube_api_key": "",
    "rate_limit_delay": 1.0
  }
}
```

---

## 5. Security & Authentication

### 5.1 OAuth 2.0 Flow

- Scope: `https://www.googleapis.com/auth/youtube.readonly` (read-only)
- Credentials stored as pickle at `src/data/oauth_credentials.pickle`
- Legacy `token.json` files are automatically migrated and deleted on first load
- Credentials are refreshed automatically when expired; full re-auth triggered if refresh fails
- `logout_account.py` deletes both the credential pickle and the subscription cache

### 5.2 Cookie Authentication

- Browser cookies are extracted at download time via `yt-dlp`'s `cookiesfrombrowser` option
- Supported browsers: Chrome (all profiles), Edge (all profiles), Firefox, Safari, Opera, Brave, Chromium
- Cookie files (`cookies.txt`, `youtube_cookies.txt`, `yt_cookies.txt`) are supported as fallback
- Cookie usage is opt-in per download invocation (`use_cookies=True`)

### 5.3 Data Safety

- All user data (`data/`) is gitignored — credentials, database, and downloads are never committed
- The JSON backup (`youtube_data.db.json`) provides recovery if the SQLite file is corrupted or deleted
- No network calls are made outside of YouTube API requests and `yt-dlp` download operations

---

## 6. Non-Functional Requirements

### 6.1 Error Handling

- All database operations are wrapped in `try/except` with printed error messages
- Database lock contention is handled by the `@with_exponential_backoff` decorator (5 retries, exponential delay with jitter)
- Download failures update the row to `failed` with a sanitized (ANSI-stripped) error message
- Partial download files (`.part`, `.temp`, `.tmp`) are cleaned up before each download attempt

### 6.2 Logging

- All output is printed to stdout using emoji-prefixed status lines
- No file-based logging is implemented; the `logs/` directory is a placeholder
- Error messages stored in the database have ANSI escape codes stripped before persistence

### 6.3 Performance Characteristics

- `batch_add_videos()` pre-fetches existing URLs in batches of 999 to avoid per-row round-trips
- `sync_with_backup()` on startup loads the full JSON backup into memory — at 28k+ records this is a known startup cost
- `create_backup()` is called on every individual write operation — at scale this is a performance bottleneck (known issue, not yet debounced)
- `get_pending_videos()` uses `LIMIT 9999` — loads up to 9999 rows into memory before processing

### 6.4 Platform

- Primary platform: Windows 10/11
- Python 3.10+ required
- `ffmpeg` on PATH is recommended for best `yt-dlp` format merging results
- All path handling uses `pathlib.Path` with `os.path.expanduser` for cross-platform compatibility, though the launcher (`start_toolkit.bat`) is Windows-only

---

## 7. Known Limitations & Open Issues

| Issue | Severity | Description |
|---|---|---|
| Backup written on every write | P2 | `create_backup()` dumps all rows to JSON on each individual add/update — degrades at scale |
| Startup sync loads full backup | P2 | `sync_with_backup()` materializes entire JSON backup into memory |
| No YouTube API retry logic | P2 | Quota errors or transient 5xx abort the entire scrape session |
| No yt-dlp socket timeout | P2 | A hung download blocks indefinitely |
| `sys.exit(0)` on path cancel | P2 | `download_path_manager.py` terminates the process on user cancellation instead of raising an exception |
| Config shallow merge | P2 | `config.load_config()` uses `dict.update()` — nested sections from file fully replace defaults |
| Duplicate `extract_video_id` | P3 | Function exists in both `video_processor.py` and `data_manager_streamlined.py` |
| Duplicate auth/duration code | P3 | `get_authenticated_service()` and `parse_duration()` duplicated across both OAuth scrapers |
| Large playlist memory usage | P3 | `scrape_custom_playlist.py` materializes entire playlist generator into memory |
