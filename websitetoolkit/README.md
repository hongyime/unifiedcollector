# Website Scraping Toolkit

A local, Windows-native Python toolkit for systematic link discovery, image
downloading, and PDF processing across a managed list of websites. Operates
entirely locally — no API keys, no cloud services, no external dependencies
beyond Python packages.

---

## Prerequisites

| Requirement | Minimum Version | Notes |
|-------------|----------------|-------|
| Python | 3.10 | 3.12 verified |
| Windows | 10 / 11 | Batch launchers; core Python logic is cross-platform |
| pip | Bundled | Included with all Python 3.4+ installations |

---

## Installation

```bat
git clone <repository-url>
cd websitetoolkit
setup.bat
```

`setup.bat` will:
1. Detect Python 3 via the `py` launcher or `python` on `PATH`
2. Create a `.venv` virtual environment at the project root
3. Run `pip install -r requirements.txt`

If an existing `.venv` is broken, `setup.bat` detects and recreates it automatically.

---

## Quick Start

```bat
start_toolkit.bat
```

The launcher presents a six-option menu:

| # | Action |
|---|--------|
| 1 | Open main interactive menu (8 sub-options) |
| 2 | Run an automated discovery + scraping cycle |
| 3 | Bulk-import websites from a text file |
| 4 | Create a sample import file |
| 5 | View the current website configuration |
| 6 | Exit |

---

## Configuration

### Scraping Settings (stored in DB — change via Settings menu)

| Key | Default | Description |
|-----|---------|-------------|
| `max_depth` | `3` | Crawl depth per website |
| `max_images` | `1000` | Max images downloaded per site per cycle |
| `max_pages` | `100` | Max pages crawled per site |
| `respect_robots_txt` | `true` | Honour robots.txt directives |
| `delay_between_requests` | `1.0` s | Minimum inter-request delay |
| `timeout` | `30` s | HTTP request timeout |
| `max_retries` | `3` | Retry count on transient HTTP failure |
| `concurrent_websites` | `5` | Sites processed in parallel |
| `enable_sitemap_discovery` | `true` | Parse XML sitemaps before crawling |
| `enable_pdf_processing` | `true` | Download and convert PDFs to images |
| `enable_url_filtering` | `true` | Apply `url_filter_config.json` patterns |

Settings are stored in `data/toolkit.db` and persist across sessions. Change them
through the Settings menu or directly via SQL.

### UI Preferences (`settings.json`)

Edit this file directly to change display and startup behaviour. These keys are
**not** stored in the DB:

```json
{
  "logging_level": "INFO",
  "timeout": 30,
  "download_path": "downloads",
  "feature_toggles": {
    "photo_scraper": true,
    "link_spider": true,
    "proxy_management": true
  }
}
```

### Cycle Rate Limits (`data/automation/cycle_config.json`)

Controls per-cycle behaviour and safety ceilings. Key fields:

| Field | Default | Description |
|-------|---------|-------------|
| `requests_per_minute` | 20 | Rate limiter window |
| `requests_per_hour` | 800 | Rate limiter window |
| `min_delay_between_requests` | 3.0 s | Floor delay with jitter |
| `max_concurrent_websites` | 10 | Safety ceiling (configurable up to this) |
| `max_photos_per_cycle` | 50,000 | Hard stop per cycle |
| `emergency_stop_on_errors` | 10 | Abort cycle after N consecutive errors |

### URL Filter Patterns (`data/url_filter_config.json`)

Glob patterns applied at crawl time. `*` matches any characters.
~60 default patterns block social media, e-commerce checkout flows,
authentication pages, and API endpoints.

```json
{
  "blocked_patterns": ["*://*/login/*", "*://*facebook.com/*"],
  "blocked_domains": ["ads.example.com"]
}
```

### Proxies (`data/proxies.txt`)

One proxy per line. Supported formats:

```
http://ip:port
http://username:password@ip:port
socks5://ip:port
```

---

## Environment Variables

**None.** This system has no environment variables. All configuration lives in
`settings.json` (UI preferences) and `data/toolkit.db` (scraping settings and
website list).

---

## Bulk Website Import

Create a plain text file and import it via menu option 3 (Bulk Import) or
`start_toolkit.bat → option 3`. See `sample_import.txt` for format reference:

```
# Supported formats — one website per line:
https://example.com
My Site | https://mysite.com
site-name, https://mysite.com
```

Duplicate detection is applied before insertion. URLs are normalised
(protocol, www prefix, and trailing slash stripped) before comparison.

---

## Running Tests

```bat
.venv\Scripts\python.exe -m pytest tests\ -v
```

Expected output: **27 passed, 0 failed.**

| Test File | What it covers |
|-----------|---------------|
| `test_utils.py` | URL normalization, filename sanitization, domain extraction |
| `test_config.py` | URL equivalence, duplicate detection, website add/remove |
| `test_bulk_website_importer.py` | URL validation on bulk import |
| `test_cycle_manager.py` | Rate limiter windows, cycle ID generation |
| `test_db_manager.py` | Schema creation, hash CRUD, settings CRUD, cycle persistence, metrics |

---

## Data Directory Reference

```
data/
├── toolkit.db                    # Primary data store — SQLite (gitignored)
├── automation/
│   └── cycle_config.json         # Rate limits, concurrency, safety ceilings
├── cycles/                       # Per-cycle JSON backup files
├── logs/
│   └── toolkit.log               # Rotating log — 10 MB × 5 backups
├── file_hashes_backup.json       # Image hash table JSON mirror (gitignored)
├── websites_config_backup.json   # Website config JSON mirror (gitignored)
├── url_filter_config.json        # URL filter glob patterns
└── proxies.txt                   # Proxy list (gitignored)
```

`downloads/` is the default session download directory (gitignored, re-prompted
each session by `download_helper.py`).

---

## Repository Structure

```
websitetoolkit/
├── main.py                  # Entry point — TUI menu
├── setup.bat                # First-run environment setup
├── start_toolkit.bat        # Main launcher
├── sample_import.txt        # Bulk import format reference
├── settings.json            # UI preferences (gitignored)
├── requirements.txt         # Python dependencies
├── src/                     # All Python modules
│   ├── config.py            # Config singleton backed by DB
│   ├── db_manager.py        # SQLite wrapper — all persistence
│   ├── cycle_manager.py     # Cycle orchestrator + rate limiter
│   ├── link_spider.py       # Link crawling engine
│   ├── photo_scraper.py     # Image download + deduplication
│   ├── sitemap_parser.py    # XML sitemap discovery
│   ├── pdf_processor.py     # PDF download + image conversion
│   ├── url_filter.py        # URL glob-pattern filter
│   ├── proxy_manager.py     # Proxy rotation
│   ├── bulk_website_importer.py # Batch website import
│   ├── data_manager.py      # Analytics wrapper
│   ├── download_helper.py   # Session download path prompt
│   ├── utils.py             # Shared utilities
│   ├── logger_config.py     # Rotating logger setup
│   └── resilience.py        # Graceful shutdown + internet retry
├── data/                    # Runtime data (mostly gitignored)
├── tests/                   # pytest test suite
└── docs/                    # Development planning artifacts
```

---

## Developer Notes

**Adding a website programmatically:**
```python
import sys; sys.path.insert(0, "src")
from config import get_config
get_config().add_website("site-name", "https://target.com")
```

**Querying cycle history:**
```python
import sys; sys.path.insert(0, "src")
from db_manager import get_db_manager
stats = get_db_manager().get_advanced_statistics()
```

**All website entries are dicts.** The `str`-URL format is normalised to a full
`dict` at `add_website()` time. No `isinstance(site, str)` branches exist anywhere
in the codebase.

**Known limitation:** The `links` DB table is defined but never populated — the
link spider writes discovered URLs to `data/link_spider/` files only.
