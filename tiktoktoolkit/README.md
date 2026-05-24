# Developer README
## Unified TikTok Download Toolkit

---

## 1. Project Overview

**Project Name:** Unified TikTok Download Toolkit  
**Type:** CLI-based download utility with multi-provider fallback chain  
**Core Function:** Download TikTok videos by username with idempotency, cookie management, and Windows ACL hardening  
**Python Version:** 3.12+

---

## 2. Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.12+ | Runtime |
| pip | Latest | Package installer |
| gallery-dl | 1.27.6+ | Primary downloader |
| yt-dlp | 2024.11.4+ | Secondary downloader |
| curl-cffi | 0.7.0+ | Browser impersonation for yt-dlp |
| playwright | 1.40.0+ | Tertiary (browser automation) fallback |
| rookiepy | 0.5.0+ | Windows cookie extraction |
| Git | Latest | Version control |

**Optional:**
- Chrome/Chromium browser (for browser automation fallback and cookie extraction)
- Microsoft Edge (alternative cookie source)
- Firefox (alternative cookie source)

---

## 3. Environment Configuration

Copy `.env.example` to `.env` and configure:

```bash
# Timeout seconds for provider HTTP requests
TIMEOUT=15

# Max retries per provider attempt
RETRIES=2

# Output root directory override (default: ./downloads)
OUTPUT_ROOT=downloads

# Logging level (DEBUG, INFO, WARNING, ERROR)
LOG_LEVEL=INFO

# TikTok specific tweaking
USER_AGENT=
ACCEPT_LANGUAGE=en-US,en;q=0.9

# Rate limiting / politeness
MIN_SLEEP=0.5
MAX_SLEEP=2.0
```

**Environment Variables Reference:**

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `TIMEOUT` | 15 | No | HTTP request timeout in seconds |
| `RETRIES` | 2 | No | Max retries per provider |
| `OUTPUT_ROOT` | downloads | No | Root output directory |
| `LOG_LEVEL` | INFO | No | Logging verbosity |
| `USER_AGENT` | (empty) | No | Custom User-Agent |
| `ACCEPT_LANGUAGE` | en-US,en;q=0.9 | No | Accept-Language header |
| `MIN_SLEEP` | 0.5 | No | Min delay between requests (seconds) |
| `MAX_SLEEP` | 2.0 | No | Max delay between requests (seconds) |

---

## 4. Installation & Setup

### 4.1 Clone & Virtual Environment

```bash
# Create virtual environment
python -m venv .venv

# Activate (Windows)
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 4.2 Cookie Setup

**Option A: Browser Cookie Export (recommended)**
```bash
python -m core.cli check-cookies --browser chrome
```

**Option B: Manual Cookie File**
1. Install TikTok cookie export extension (e.g., "Get cookies.txt LOCALLY")
2. Export cookies for tiktok.com
3. Place as `configs/tiktok_cookies.txt`

### 4.3 Run Setup Script

```bash
# Windows
setup.bat
```

---

## 5. Usage

### 5.1 Core Commands

```bash
# Download by username
python main.py download-user <username> [--limit N] [--output DIR]

# Batch download from file
python main.py batch-download --file data/usernames.txt

# Check cookie status
python main.py check-cookies [--browser chrome]

# Refresh cookies from browser
python main.py refresh-cookies [--browser chrome]

# Find duplicates
python main.py find-duplicates --dir downloads [--delete]

# Reset tracker
python main.py reset-tracker [--no-backup]

# Show status
python main.py status
```

### 5.2 Download Provider Chain

The system uses a three-tier fallback chain:

```
Tier 1: gallery-dl (primary)
  └── On failure → Tier 2: yt-dlp with Chrome impersonation
                        └── On failure → Tier 3: Playwright browser automation
```

### 5.3 Output Layout

```
downloads/
  username_<user>/
    <video_id>.mp4    # canonical flat layout
    <video_id>.jpg    # thumbnail (if available)
```

---

## 6. Testing

### 6.1 Run All Tests

```bash
python -m pytest tests/ -q
```

### 6.2 Test Categories

| Suite | Location | Count | Purpose |
|-------|---------|-------|---------|
| Unit | `tests/unit/` | ~30 | Individual function/module tests |
| Integration | `tests/integration/` | ~30 | Provider and CLI integration tests |
| Property | `tests/property/` | ~70 | Hypothesis-based property tests |

### 6.3 Specific Test Runs

```bash
# Unit tests only
python -m pytest tests/unit/ -q

# Integration tests only
python -m pytest tests/integration/ -q

# Property-based tests only
python -m pytest tests/property/ -q

# Single test file
python -m pytest tests/integration/test_provider.py -v

# With verbose output
python -m pytest tests/ -v --tb=short
```

### 6.4 Test Requirements

- `playwright` tests are skipped if Playwright is not installed
- `rookiepy` tests are mocked if the package is not available
- All 136 tests should pass on a clean installation with all optional dependencies

---

## 7. Architecture Notes

### 7.1 Core Modules

| Module | Responsibility |
|--------|-----------------|
| `src/provider.py` | `GalleryDLProvider` — main download logic with fallback chain |
| `src/cli.py` | Click-based CLI with 10+ commands |
| `src/tracker.py` | SQLite + JSON dual-backend download tracker |
| `src/cookie_manager.py` | Cookie validation (`TikTokCookieManager`) |
| `src/browser_downloader.py` | Playwright-based browser automation fallback |
| `src/ytdlp_downloader.py` | yt-dlp wrapper with Chrome impersonation |
| `src/utils.py` | Shared utilities including `secure_file_permissions()` |
| `src/config.py` | YAML config loader |

### 7.2 Key Design Decisions

1. **Flat output layout** — All videos stored as `<video_id>.<ext>` directly in the per-user folder (no date subfolders)
2. **SQLite WAL mode** — Enables concurrent reads; tracker is safe for multi-process use
3. **Backup-first destructive commands** — `reset-tracker` and `find-duplicates --delete` auto-backup before operating
4. **Windows ACL hardening** — Sensitive files (cookies) get restrictive Windows ACL via `icacls`

### 7.3 Dependency Fallback Behavior

| gallery-dl | yt-dlp | Playwright | Behavior |
|-------------|--------|------------|-----------|
| ✅ Success | — | — | Done |
| ❌ Timeout | ✅ Success | — | Done |
| ❌ Auth error | ✅ Success | — | Done |
| ❌ Not installed | ❌ Fail | ✅ Success | Done (slower) |
| ❌ Fail | ❌ Fail | ❌ Fail | Final error |

---

## 8. Troubleshooting

### 8.1 Common Issues

| Issue | Solution |
|-------|----------|
| "gallery-dl not found" | `pip install gallery-dl` |
| "Authentication failed" | Refresh cookies: `python main.py refresh-cookies` |
| "Browser automation failed" | Install Playwright: `playwright install chromium` |
| "Cookies extracted but videos still fail" | Clear and re-extract cookies; check TikTok login status |
| Duplicate downloads | Run tracker maintenance: `python main.py reset-tracker` |

### 8.2 Diagnostic Scripts

```bash
# Run diagnostics
scripts\diagnostics\diagnose.bat

# Verify cookie extraction
python scripts/diagnostics/check_cookies.py

# Debug gallery-dl
python scripts/diagnostics/debug_gallery_dl.py
```

### 8.3 Log Locations

| Log | Path |
|-----|------|
| Application log | `logs/uttk.log` |
| Log archive | `logs/uttk.log.1`, `.2`, `.3` |

---

## 9. Scripts Taxonomy

| Category | Scripts | Purpose |
|----------|---------|---------|
| `scripts/diagnostics/` | `check_cookies.py`, `debug_gallery_dl.py`, `diagnose.bat` | Troubleshooting |
| `scripts/ops/` | `refresh_cookies.bat`, `test_fallback.bat`, `test_idempotency.bat` | Operations |
| `scripts/migrations/` | `migrate_downloads.py` | Legacy layout migration |

---

## 10. Contributing

1. **Test coverage** — All new features require corresponding tests
2. **pytest pass** — `python -m pytest tests/ -q` must pass before submitting
3. **No hardcoded secrets** — Use `.env` for all sensitive configuration
4. **Backup before destructive changes** — Any operation that deletes files must create a backup first
