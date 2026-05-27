# Product Requirements Document — Website Scraping Toolkit
**Version:** 2.0 · **Date:** 2026-04-26 · **Status:** Production-Hardened

---

## 1. Executive Summary

This system is a local, Windows-native Python scraping toolkit for systematically
discovering links, downloading images, and processing PDFs from a user-managed
list of websites. It operates entirely offline — no external services, no API keys,
no cloud dependencies. All data is persisted to a local SQLite database with JSON
backup files as a secondary recovery layer.

**Primary use case:** Bulk media acquisition and link discovery across a curated
site list, run on-demand or as recurring automated cycles from an interactive
terminal menu.

---

## 2. System Architecture

### Component Interaction

```
start_toolkit.bat
    └─► main.py (TUI menu)
            ├─► config.py ──────────────────► db_manager.py ──► data/toolkit.db
            ├─► cycle_manager.py             ├─► save_cycle()
            │       ├─► link_spider.py       ├─► add_hash()
            │       └─► photo_scraper.py     ├─► save_websites()
            ├─► bulk_website_importer.py     └─► get_system_metrics()
            ├─► data_manager.py (analytics)
            └─► download_helper.py (UX)
```

### Data Flow — Cycle Run

```
User triggers cycle
  └─► CycleManager.run_cycle()
        ├─► get_enabled_websites() from DB via Config
        ├─► DISCOVERY: LinkSpider.crawl_website_urls()
        │       └─► output: data/link_spider/ files  (DB links table not yet populated)
        ├─► SCRAPING: PhotoScraper.scrape_website_images()
        │       ├─► add_hash() → DB file_hashes table  (dedup — no per-hash JSON write)
        │       └─► images saved to session download_dir
        └─► PERSIST: _save_cycle_stats() → data/cycles/*.json (backup)
                   + DatabaseManager.save_cycle() → DB cycles table (primary)
```

### Dual Config System

| Authority | Store | Keys |
|-----------|-------|------|
| Scraping behaviour | `data/toolkit.db` → `settings` table | `max_depth`, `max_images`, `timeout`, `respect_robots_txt`, rate limits, etc. |
| UI preferences | `settings.json` | `logging_level`, `timeout`, `feature_toggles`, `download_path` |
| Website list | `data/toolkit.db` → `websites_config` table | Name, URL, enabled flag, custom headers, auth, PDF/sitemap settings |

---

## 3. Feature Matrix

| Feature | Module | Status | Notes |
|---------|--------|--------|-------|
| Interactive TUI menu | `main.py` | Implemented | 8 menu options |
| Website CRUD | `config.py` | Implemented | Add, remove, toggle, duplicate-detection |
| Bulk website import | `bulk_website_importer.py` | Implemented | Parses URL-only, name+URL, CSV-style |
| Automated cycle (discovery + scraping) | `cycle_manager.py` | Implemented | Rate limiter, chunking, DB persistence |
| Link discovery / crawling | `link_spider.py` | Implemented | Output to `data/link_spider/` files; DB `links` table not populated |
| Image deduplication | `db_manager.py` | Implemented | SHA-256 hash in `file_hashes` table |
| Photo scraping | `photo_scraper.py` | Implemented | JPEG/PNG/WebP/GIF; size/dimension limits |
| XML sitemap parsing | `sitemap_parser.py` | Implemented | Nested sitemap support; `defusedxml` hard-required |
| PDF download + conversion | `pdf_processor.py` | Implemented | PyMuPDF (fitz); `pdf2image` optional fallback |
| URL pattern filtering | `url_filter.py` | Implemented | Glob patterns; ~60 default blocked patterns |
| Proxy rotation | `proxy_manager.py` | Implemented | HTTP/SOCKS; per-proxy success/failure stats |
| Analytics / metrics | `db_manager.py`, `data_manager.py` | Implemented | Aggregates from DB; cycle history visible after first run |
| robots.txt compliance | `config.py` | Implemented (default: `true`) | Honour/ignore configurable per-installation |
| Report export | `data_manager.py` | Partial | Writes to `data/` root; no dedicated export directory |
| DB backup | `db_manager.py` | Implemented | JSON mirror refreshed on settings/website save |

---

## 4. Data Model

### SQLite Schema (`data/toolkit.db`)

**`websites_config`** — canonical website store (write source)
```
name TEXT PRIMARY KEY
config_json TEXT   -- full config dict: url, enabled, max_depth, custom_headers, auth, pdf_settings, sitemap_settings
```

**`websites`** — structured query table (synced from `websites_config` on startup)
```
id INTEGER PRIMARY KEY
name TEXT UNIQUE, url TEXT, enabled BOOLEAN
added_date TEXT, last_crawled TEXT, last_scraped TEXT
total_links_found INTEGER DEFAULT 0
total_photos_downloaded INTEGER DEFAULT 0
discovery_source TEXT
```

**`settings`** — scraping configuration key-value store
```
key TEXT PRIMARY KEY
value_json TEXT   -- JSON-encoded value (int, float, bool, string, list)
```

**`file_hashes`** — image deduplication registry
```
hash_id TEXT PRIMARY KEY   -- SHA-256 hex digest
hash_type TEXT             -- e.g. "sha256"
timestamp TEXT             -- ISO 8601
```

**`links`** — discovered URL store *(schema defined; not yet populated by link spider)*
```
id INTEGER PRIMARY KEY
website_id INTEGER REFERENCES websites(id)
url TEXT, link_type TEXT, discovered_date TEXT
status TEXT DEFAULT 'pending'
```

**`cycles`** — cycle run history
```
id INTEGER PRIMARY KEY
cycle_id TEXT UNIQUE       -- format: YYYYMMDD_HHMMSS
start_time TEXT, end_time TEXT
websites_processed INTEGER, links_discovered INTEGER
photos_downloaded INTEGER, new_websites_added INTEGER
status TEXT                -- 'completed' | 'completed_with_errors' | 'interrupted' | 'failed'
```

---

## 5. Security & Compliance

| Area | Status | Detail |
|------|--------|--------|
| No hardcoded secrets | Pass | Proxy file contains comment placeholders only |
| XXE protection | Pass | `defusedxml` hard-required; `ImportError` raised at import time if missing |
| robots.txt compliance | Pass | Defaults `true`; stored per-installation in DB |
| Input validation | Pass | URL reachability check before add; glob-pattern URL filter at crawl time |
| File write atomicity | Partial | DB writes atomic (SQLite WAL); JSON backup writes not temp-file+rename |
| No environment secrets | N/A | System has no API keys, tokens, or external service credentials |

---

## 6. Performance Characteristics

| Constraint | Default Value | Source |
|-----------|---------------|--------|
| Requests per minute | 20 | `data/automation/cycle_config.json` |
| Requests per hour | 800 | `data/automation/cycle_config.json` |
| Requests per day | 5,000 | `data/automation/cycle_config.json` |
| Inter-request delay | 3–10 s with jitter | `GlobalRateLimiter` |
| Max concurrent websites | 10 (safety ceiling) | `cycle_config.json` `safety_limits` |
| Max photos per cycle | 50,000 | `cycle_config.json` `safety_limits` |
| Hash dedup I/O | Per-image DB INSERT only | `db_manager.add_hash()` — no per-hash JSON write |
| Log rotation | 10 MB × 5 backups | `logger_config.py` `RotatingFileHandler` |

---

## 7. Non-Functional Requirements

### Error Handling

- All DB exceptions logged via `logger.error()` with `%s` interpolation
- System falls back to default config on DB failure at startup
- `KeyboardInterrupt` and unhandled exceptions each persist a partial cycle row to DB with status `interrupted` or `failed`
- `CycleManager.run_cycle()` raises `RuntimeError` with diagnostic message if any required module failed to import

### Logging

- Logger: `logger_config.setup_logger()` — `RotatingFileHandler` → `data/logs/toolkit.log`
- Format: `YYYY-MM-DD HH:MM:SS - module - LEVEL - message` (plaintext)
- Console handler active on all loggers

### Testing

- 27 automated tests across 5 test files
- Framework: `pytest` + `pytest-asyncio`
- Coverage: DB schema, hash dedup, website CRUD, duplicate detection, URL normalization, rate limiter, cycle ID generation, bulk import validation

---

## 8. Known Gaps

| Gap | Impact | Priority |
|----|--------|----------|
| `links` DB table always empty | Link spider writes to files only; DB link analytics always show 0 | Medium |
| `export_readable_report()` target path | Writes to `data/` root, not a dedicated directory | Low |
| Log format not structured (JSON/KV) | No machine-readable log aggregation | Low |
| No SIGTERM / signal handler in cycle | Abrupt termination may leave partial cycle without DB record | Medium |
| `websites.total_links_found` / `total_photos_downloaded` never updated | Per-site counters always 0; aggregate cycle stats are accurate | Low |
