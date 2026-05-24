# YouTube Toolkit

> A local Python toolkit for scraping, queuing, and downloading YouTube content with SQLite-backed state management.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-14%2F14%20passing-brightgreen.svg)]()

## Overview

The YouTube Toolkit is a Windows-first command-line application that implements a **scrape → queue → download** lifecycle for YouTube content. It collects video URLs from multiple sources, persists them in a SQLite queue with automatic deduplication, and batch-downloads either full videos or channel profile photos using `yt-dlp`.

**Core capabilities:**
- OAuth-backed scraping of liked videos and subscriptions via YouTube Data API v3
- API-free scraping of target channels and custom playlists via `yt-dlp`
- SQLite queue with JSON backup/restore for interrupted-download recovery
- Browser cookie authentication (Chrome, Edge, Firefox, and others)
- Interactive Windows launcher with 11 workflow options

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Project Structure](#project-structure)
- [Configuration](#configuration)
- [Usage](#usage)
- [Authentication Setup](#authentication-setup)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Windows | 10 or 11 | Primary supported platform |
| Python | 3.10+ | Must be on PATH |
| ffmpeg | Any recent | Recommended; required for format merging |

**Python dependencies** (installed automatically by `setup.bat`):

| Package | Version Constraint | Purpose |
|---|---|---|
| `yt-dlp` | `~=2023.7.6` | Video downloading and metadata extraction |
| `google-auth-oauthlib` | `~=1.0.0` | OAuth 2.0 authentication flow |
| `google-api-python-client` | `~=2.0.0` | YouTube Data API v3 client |
| `google-auth-httplib2` | `~=0.4.0` | HTTP transport for Google auth |
| `tqdm` | `~=4.66.0` | Progress bar rendering |

---

## Installation

### Option 1: Automated Setup (Recommended)

```bat
setup.bat
```

This script:
1. Detects your Python 3 interpreter (`py` or `python`)
2. Creates a `.venv` virtual environment
3. Upgrades pip
4. Installs all required packages

### Option 2: Manual Setup

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Verify Installation

```bat
.venv\Scripts\python.exe scripts\validate_installation.py
```

Expected output: `7/7 checks passed`.

---

## Project Structure

```
youtubetoolkit/
├── src/                              # Core library modules
│   ├── app_paths.py                  # Centralized path constants
│   ├── auth_cache.py                 # OAuth credential caching
│   ├── config.py                     # Configuration management
│   ├── data_manager_streamlined.py   # SQLite database layer
│   ├── download_path_manager.py      # Session-scoped path prompting
│   └── video_processor.py            # yt-dlp download engine
│
├── scripts/                          # Executable entry points
│   ├── batch_downloader.py           # Batch download from queue
│   ├── scrape_liked_videos_enhanced.py  # OAuth liked videos scraper
│   ├── subscription_processor.py     # OAuth subscription scraper
│   ├── scrape_targets.py             # Target channel scraper (no API)
│   ├── scrape_custom_playlist.py     # Custom URL scraper (no API)
│   ├── logout_account.py             # Clear OAuth credentials
│   └── validate_installation.py      # Installation checker
│
├── tests/                            # Test suite
│   ├── conftest.py                   # Path setup and shared fixtures
│   ├── test_auth_cache.py            # OAuth credential migration tests
│   ├── test_db.py                    # Database operation tests
│   ├── test_download_flows.py        # Download workflow tests
│   └── test_parsers.py               # URL/duration parser tests
│
├── docs/
│   ├── PRD.md                        # Product requirements document
│   └── YOUTUBE_API_SETUP.md          # OAuth credentials guide
│
├── data/                             # Runtime data (gitignored)
│   ├── youtube_data.db               # SQLite queue database
│   ├── youtube_data.db.json          # JSON recovery backup
│   ├── config.json                   # User configuration
│   ├── client_secret.json            # OAuth app credentials (user-provided)
│   ├── oauth_credentials.pickle      # Cached OAuth tokens
│   ├── subscriptions.json            # 24-hour subscription cache
│   ├── target_channels.txt           # User-defined channel list
│   ├── all_scraped_links.txt         # Links extracted from descriptions
│   └── downloads/                    # Default download directory
│
├── start_toolkit.bat                 # Interactive Windows launcher
├── setup.bat                         # Automated setup script
├── requirements.txt                  # Pinned Python dependencies
└── README.md                         # This file
```

---

## Configuration

Configuration is stored at `data/config.json` and is auto-created with defaults on first run.

| Key | Default | Description |
|---|---|---|
| `processing.batch_size` | `10` | Videos processed per batch |
| `processing.max_retries` | `3` | Retry attempts per failed operation |
| `processing.timeout_seconds` | `300` | Operation timeout in seconds |
| `output.base_folder` | `""` | Default download directory (empty = prompt each session) |
| `output.organize_by_date` | `true` | Organize downloads into date subdirectories |
| `download.max_resolution` | `"1080"` | Maximum video resolution (`"720"`, `"1080"`, `"best"`) |
| `download.audio_only` | `false` | Download audio stream only |
| `ui.show_progress` | `true` | Show progress indicators |
| `ui.colorful_output` | `true` | Enable emoji/color output |
| `ui.auto_cleanup` | `false` | Auto-clean partial files |
| `api.youtube_api_key` | `""` | Optional API key (OAuth is used instead) |
| `api.rate_limit_delay` | `1.0` | Delay in seconds between API requests |

**Note:** `output.base_folder` is intentionally left empty. The toolkit prompts for a download path at the start of each session and does not persist it between runs.

## Usage

### Interactive Launcher (Recommended)

```bat
start_toolkit.bat
```

Menu options:

**Scraping (Add to Queue):**
| Option | Action | OAuth Required |
|--------|--------|----------------|
| `[1]` | Scrape: Liked videos | ✅ Yes |
| `[2]` | Scrape: Subscriptions | ✅ Yes |
| `[3]` | Scrape: Target channels (from target_channels.txt) | ❌ No |
| `[4]` | Scrape: Custom URL/playlist | ❌ No |

**Downloading:**
| Option | Action |
|--------|--------|
| `[5]` | Download: All pending videos |
| `[6]` | Download: All pending profile photos |
| `[7]` | Download: Videos + Photos (everything pending) |
| `[8]` | Download: Retry failed videos |
| `[9]` | Download: Retry failed photos |

**Management:**
| Option | Action |
|--------|--------|
| `[10]` | View database statistics |
| `[11]` | Manage target channels (add/remove from terminal) |
| `[12]` | Clear OAuth credentials (re-authenticate) |
| `[13]` | Exit |

**Note:** Option 11 now provides an interactive terminal interface to add/remove channels, or you can still open Notepad for advanced editing.

### CLI — Individual Scripts

All scripts must be run from the project root with the virtual environment active, or using the full venv path.

**Scrape target channels (no API key required):**
```bat
.venv\Scripts\python.exe scripts\scrape_targets.py
```

**Scrape a custom playlist or channel URL:**
```bat
.venv\Scripts\python.exe scripts\scrape_custom_playlist.py https://www.youtube.com/@channelname
```

**Scrape liked videos (OAuth required):**
```bat
.venv\Scripts\python.exe scripts\scrape_liked_videos_enhanced.py
.venv\Scripts\python.exe scripts\scrape_liked_videos_enhanced.py --days 30
```

**Scrape subscriptions (OAuth required):**
```bat
.venv\Scripts\python.exe scripts\subscription_processor.py
.venv\Scripts\python.exe scripts\subscription_processor.py --days 7 --max-videos 50
```

**Batch download all pending videos:**
```bat
.venv\Scripts\python.exe scripts\batch_downloader.py
.venv\Scripts\python.exe scripts\batch_downloader.py --days 14
.venv\Scripts\python.exe scripts\batch_downloader.py --photos-only
.venv\Scripts\python.exe scripts\batch_downloader.py --retry-failed
```

**View database statistics:**
```bat
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'src'); from data_manager_streamlined import DatabaseManager; db = DatabaseManager(); stats = db.get_video_statistics(); print(stats)"
```

**Clear OAuth credentials:**
```bat
.venv\Scripts\python.exe scripts\logout_account.py
```

### CLI Argument Reference

**`batch_downloader.py`**

| Argument | Default | Description |
|---|---|---|
| `--download-folder PATH` | Prompted | Output directory for downloads |
| `--channels-file PATH` | None | Filter to channels listed in file |
| `--retry-failed` | False | Retry failed downloads instead of pending |
| `--days N` | None | Restrict to videos added in last N days |
| `--photos-only` | False | Download profile photos instead of videos |

**`subscription_processor.py`**

| Argument | Default | Description |
|---|---|---|
| `--max-videos N` | 20 | Max videos to fetch per channel |
| `--max-channels N` | 999 | Max channels to process |
| `--days N` | None | Restrict to videos published in last N days |
| `--no-auto-add` | False | Scrape without adding to database |

**`scrape_custom_playlist.py`**

| Argument | Default | Description |
|---|---|---|
| `url` (positional) | Prompted | YouTube playlist, channel, or video URL |
| `--days N` | None | Restrict to videos published in last N days |
| `--no-auto-add` | False | Scrape without adding to database |

---

## Authentication Setup

OAuth authentication is required for scraping liked videos and subscriptions. It is **not** required for target channel or custom URL scraping.

### Quick Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project and enable **YouTube Data API v3**
3. Create **OAuth 2.0 credentials** → Application type: **Desktop app**
4. Download the JSON file, rename it to `client_secret.json`
5. Place it at `data/client_secret.json`
6. Run any OAuth scraper — your browser will open for one-time authorization

Full step-by-step instructions: `docs/YOUTUBE_API_SETUP.md`

### Credential Storage

| File | Contents | Auto-managed |
|---|---|---|
| `data/client_secret.json` | OAuth app credentials | No — user must provide |
| `data/oauth_credentials.pickle` | Cached access/refresh tokens | Yes — created on first auth |
| `data/subscriptions.json` | Subscription list cache (24h TTL) | Yes — refreshed automatically |

### Re-authentication

```bat
.venv\Scripts\python.exe scripts\logout_account.py
```

This deletes `oauth_credentials.pickle` and `subscriptions.json`. The next scraper run will trigger a fresh browser-based authorization.

---

## Testing

Run the full test suite:

```bat
.venv\Scripts\python.exe -m pytest tests/ -v
```

Run a specific test file:

```bat
.venv\Scripts\python.exe -m pytest tests/test_db.py -v
```

**Test coverage:**

| File | What It Tests |
|---|---|
| `test_auth_cache.py` | Legacy `token.json` → `oauth_credentials.pickle` migration |
| `test_db.py` | DB initialization, `add_video`, `batch_add_videos`, `update_download_status`, backup restore |
| `test_download_flows.py` | `resume_interrupted_downloads` (mocked yt-dlp), batch download success, retry failure state |
| `test_parsers.py` | `extract_video_id` (valid/invalid URLs), `parse_duration` (ISO 8601), `resolve_cookie_choice` |

Expected result: **14/14 tests passing**.

---

## Troubleshooting

**"Module not found" errors**

Ensure you are using the virtual environment interpreter:
```bat
.venv\Scripts\python.exe scripts\batch_downloader.py
```
Or activate the environment first: `.venv\Scripts\activate`

**"OAuth credentials not found" / browser does not open**

- Confirm `data/client_secret.json` exists
- Confirm the file contains valid OAuth 2.0 Desktop app credentials
- See `docs/YOUTUBE_API_SETUP.md` for credential creation steps

**"Database is locked"**

Another process is accessing the database. Close it and retry. The toolkit has built-in exponential backoff (5 retries) for transient lock contention.

**"Quota exceeded"**

The YouTube Data API has a default quota of 10,000 units/day. Mitigation options:
- Use `--days N` to limit the scrape scope
- Use `scrape_targets.py` or `scrape_custom_playlist.py` — these use `yt-dlp` and consume no API quota
- Request a quota increase in Google Cloud Console

**Downloads stuck / not completing**

On next startup, `DatabaseManager` automatically resets any rows stuck in `downloading` for more than one hour back to `pending`. You can also manually trigger a retry:
```bat
.venv\Scripts\python.exe scripts\batch_downloader.py --retry-failed
```

**Videos download as `.m4a` or `.opus` but show as failed**

This occurs when `yt-dlp` selects an audio-only format. Set `download.audio_only = true` in `data/config.json` to explicitly request audio, or set `download.max_resolution` to `"best"` to allow any format.

**"Access blocked" during OAuth**

- Ensure you selected **Desktop app** (not Web application) when creating credentials
- Add your Google account as a test user in the OAuth consent screen

---

## Target Channels File Format

`data/target_channels.txt` — one entry per line:

```
# Lines starting with # are comments
UCxxxxxxxxxxxxxxxxxxxxxx
https://www.youtube.com/@channelname
https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx
```

Edit via the launcher (option `[7]`) or directly with any text editor.
