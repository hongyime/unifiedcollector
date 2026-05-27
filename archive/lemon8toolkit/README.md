# Social Media Media Scraping Toolkit

A comprehensive Python toolkit for scraping and downloading media from social media platforms. Features smart deduplication via SQLite, crash-proof progress tracking, and multiple scraping modes.

## Table of Contents

1. [Project Overview](#project-overview)
2. [Prerequisites](#prerequisites)
3. [Environment Configuration](#environment-configuration)
4. [Installation & Setup](#installation--setup)
5. [Usage](#usage)
6. [Testing](#testing)
7. [Architecture](#architecture)

---

## Project Overview

### What This Toolkit Does

- **Scrapes media** from user profiles, trending feeds, and topic pages
- **Downloads images and videos** with automatic format conversion (WebP → JPG)
- **Prevents duplicate downloads** using SQLite-backed deduplication
- **Tracks progress** across sessions with crash-resilient storage

### Core Components

| File | Purpose |
|------|---------|
| `src/main.py` | CLI entry point with argument parsing |
| `src/config.py` | Centralized configuration constants |
| `src/scraper.py` | Web scraping engine with HTML/JSON parsing |
| `src/downloader.py` | Download manager with deduplication |
| `src/tracking.py` | Account and tag tracking (SQLite + JSON) |
| `src/progress.py` | Session lifecycle management |
| `src/path_manager.py` | Download path validation and session caching |

---

## Project Structure

```
lemon8toolkit/
├── src/                      # Python source modules
│   ├── __init__.py
│   ├── main.py               # CLI entry point
│   ├── config.py             # Configuration constants
│   ├── scraper.py            # Web scraping engine
│   ├── downloader.py         # Media download manager
│   ├── tracking.py           # Account/tag tracking
│   ├── progress.py           # Session progress
│   └── path_manager.py       # Download path management
├── scripts/                  # Windows batch scripts
│   ├── setup.bat             # Dependency installer
│   └── start_toolkit.bat     # Interactive menu launcher
├── tests/                    # Test suite
├── data/                     # Runtime data (not in git)
├── downloads/                # Downloaded media (not in git)
├── requirements.txt
├── README.md
└── .gitignore
```

---

## Prerequisites

### System Requirements

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.8+ | Tested on 3.8-3.11 |
| Operating System | Windows 10+ | Batch scripts Windows-specific |
| Disk Space | 500MB+ | Plus downloaded media |

### Required Tools

| Tool | Purpose |
|------|---------|
| Python 3 | Runtime interpreter |
| pip | Package installer |
| Git (optional) | Version control |

### Optional: Cookie Authentication

For improved scraping reliability, export browser cookies as Netscape format:

1. Install Chrome extension: "Get cookies.txt LOCALLY"
2. Navigate to the target platform
3. Export cookies as Netscape format
4. Save as `cookies.txt` in the toolkit root

---

## Environment Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LEMON8_COOKIE_FILE` | No | Custom path to cookies.txt |

### Configuration Table (config.py)

| Key | Default | Description |
|-----|---------|-------------|
| `MIN_DELAY` | `1` | Minimum delay between requests (seconds) |
| `MAX_DELAY` | `3` | Maximum delay between requests (seconds) |
| `REQUESTS_PER_MINUTE` | `30` | Rate limit target |
| `HIGH_QUALITY_IMAGE_WIDTH` | `2160` | Target image width |
| `HIGH_QUALITY_IMAGE_HEIGHT` | `2160` | Target image height |
| `HIGH_QUALITY_IMAGE_QUALITY` | `100` | JPG quality (0-100) |
| `MIN_IMAGE_WIDTH` | `320` | Minimum accepted image width |
| `MIN_IMAGE_HEIGHT` | `320` | Minimum accepted image height |
| `USERNAME_PREFIX_ENABLED` | `True` | Prefix filenames with username |
| `PROFILE_PHOTO_DOWNLOAD_ENABLED` | `True` | Include profile photos |

### Path Configuration

| Path | Default | Description |
|------|---------|-------------|
| `DATA_DIR` | `data/` | Runtime data directory |
| `DOWNLOADS_DIR` | `downloads/` | Media download directory |

---

## Installation & Setup

### Method 1: Interactive Setup (Recommended)

```bash
# 1. Navigate to toolkit directory
cd lemon8toolkit

# 2. Run setup script
scripts\setup.bat
```

This will:
- Create a Python virtual environment (`.venv/`)
- Install dependencies from `requirements.txt`
- Verify installation

### Method 2: Manual Setup

```bash
# Create virtual environment
python -m venv .venv

# Activate virtual environment
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Method 3: Using Python Launcher

```bash
# Automatically uses py -3 if available
scripts\setup.bat
```

### Post-Setup Verification

```bash
# Check Python version
.venv\Scripts\python.exe --version

# List installed packages
.venv\Scripts\pip list
```

---

## Usage

### Quick Start

Launch the interactive menu:

```bash
scripts\start_toolkit.bat
```

This provides a numbered menu for:
1. Scrape User Profile
2. Scrape For You Feed
3. Scrape Tag/Topic
4. View Statistics
5. Change External Download Folder
6. Clear Toolkit Data
7. Exit

### Command-Line Usage

#### Scrape User Profile

```bash
# Basic usage
python -m src.main user <username>

# With download
python -m src.main user walshdelaney --download

# Force re-scrape (all tracked users)
python -m src.main user --force

# Include profile photos
python -m src.main user walshdelaney --download --include-profile-photos

# Custom output path
python -m src.main user walshdelaney --download --out "D:\Downloads\SocialMedia"
```

#### Scrape For You Feed

```bash
# Basic usage (10 pages)
python -m src.main feed

# With download
python -m src.main feed --download

# Custom page count
python -m src.main feed --pages 5 --download

# Custom output path
python -m src.main feed --download --out "D:\Downloads\Trending"
```

#### Scrape Tag/Topic

```bash
# By numeric ID
python -m src.main tag 7549513626407780359 --download

# By keyword
python -m src.main tag singapore --download

# Custom page count
python -m src.main tag travel --pages 5 --download

# Force rescrape
python -m src.main tag singapore --force
```

#### View Statistics

```bash
python -m src.main stats
```

#### Clear All Data

```bash
python -m src.main clear
```

### Common Options

| Option | Applies To | Description |
|--------|------------|-------------|
| `--download` | user, feed, tag | Enable media downloading |
| `--out <path>` | user, feed, tag | Set download directory |
| `--force` | user, tag | Re-scrape even if visited |
| `--pages <n>` | feed, tag | Number of pages to scrape |
| `--include-profile-photos` | user, feed | Download profile images |
| `--exclude-profile-photos` | user, feed | Skip profile images |

---

## Testing

### Run All Tests

```bash
.venv\Scripts\python.exe -m pytest tests/ -v
```

### Run Specific Test Files

```bash
# Test scraper only
.venv\Scripts\python.exe -m pytest tests/test_scraper.py -v

# Test downloader only
.venv\Scripts\python.exe -m pytest tests/test_downloader.py -v

# Test main module
.venv\Scripts\python.exe -m pytest tests/test_main.py -v

# Integration tests
.venv\Scripts\python.exe -m pytest tests/test_integration.py -v
```

### Run Tests with Coverage

```bash
.venv\Scripts\python.exe -m pytest tests/ --cov=. --cov-report=term-missing
```

### Run Specific Test Cases

```bash
# Test by name
.venv\Scripts\python.exe -m pytest tests/ -k "test_scrape_user" -v

# Test by marker
.venv\Scripts\python.exe -m pytest tests/ -m "not slow" -v
```

---

## Architecture

### Data Flow

```
User Input → CLI Parser → Scraper → Media URLs
                                    ↓
                              Downloader
                                    ↓
                    ┌───────────────┼───────────────┐
                    ↓               ↓               ↓
              SQLite DB      JSON Backup      Downloaded Files
              (dedup)        (backup)         (disk)
```

### Deduplication Strategy

1. URL hashes stored in SQLite for O(1) lookup
2. JSON backup provides portability
3. On startup, JSON is merged into SQLite if missing
4. Downloads use `.tmp` staging; renamed on success

### Session Management

- Each scrape operation creates a session
- Sessions track: scraped media, downloaded media, failures
- Sessions persist in `data/download_progress.json`
- Sessions pruned to last 100 after 100 exist

### Cookie Loading

1. Check `LEMON8_COOKIE_FILE` environment variable
2. Check `./cookies.txt` in current directory
3. Check `cookies.txt` in toolkit directory
4. Falls back to anonymous scraping if not found

### Rate Limiting

- Configurable delays: `MIN_DELAY` to `MAX_DELAY` seconds
- Retry logic: 3 attempts with exponential backoff
- HTTP status handling: 429, 500-504 trigger retry

---

## Troubleshooting

### Common Issues

| Issue | Solution |
|-------|----------|
| "Module not found" | Run `setup.bat` to create .venv |
| "Permission denied" | Use a directory you have write access to |
| Empty results | Try adding cookies.txt for authentication |
| 403 Forbidden | Cookies may be expired; refresh them |
| Slow downloads | Reduce `--pages` or check network |

### Debug Mode

```bash
# Enable verbose output
python -u -m src.main user <username> --download
```

### Clear Cache

```bash
# Reset all data
python -m src.main clear

# Manually delete data files
del data\*.json data\*.db
```

---

## File Reference

### Runtime Files (in `data/`)

| File | Purpose |
|------|---------|
| `lemon8_toolkit.db` | SQLite database |
| `downloaded_media.json` | Downloaded URL hashes |
| `visited_users.json` | Tracked user accounts |
| `processed_tags.json` | Tracked tag IDs |
| `download_progress.json` | Session history |
| `download_verification.log` | Download audit log |

### Generated Directories

| Directory | Purpose |
|-----------|---------|
| `downloads/` | Downloaded media files |
| `downloads/user_<name>/` | Per-user downloads |
| `downloads/foryou_feed/` | Feed downloads |
| `downloads/tag_<id>/` | Tag-specific downloads |

---

## License

MIT License

## Contributing

This toolkit is designed for educational and personal use. Please respect platform terms of service.