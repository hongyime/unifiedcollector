# Website Toolkit — Validation & Repair Status
**Date:** 2026-05-13  **Tests:** 27/27 passing

---

## What the Toolkit Does
Local, Windows-native Python scraping toolkit. Maintains a SQLite-backed list of websites and runs systematic cycles: link discovery (LinkSpider) → image download (PhotoScraper) → PDF extraction. Entry: `start_toolkit.bat` → 6-option launcher → `main.py` (10-option TUI menu).

Architecture: `start_toolkit.bat` → `main.py` → `src/` modules → `data/toolkit.db`

---

## Issues Found & Fixed

| # | File | Bug | Fix |
|---|------|-----|-----|
| 1 | `tests/conftest.py` | Adds project root to sys.path; modules are in `src/` — all 5 test files fail collection (0/27 passing) | Added `src/` to sys.path in conftest |
| 2 | `start_toolkit.bat` | Options 3 (Bulk Import), 4 (Create Sample), 5 (View Config) invoke Python without `src/` in path — `ModuleNotFoundError` on every call | Added `set "PYTHONPATH=%~dp0src;%PYTHONPATH%"` before all invocations |
| 3 | `src/config.py` | `BASE_DIR` anchored to `src/` via `__file__` → `DATA_DIR=src/data/`, `DOWNLOADS_DIR=src/downloads/`. Created a split: two live databases (`src/data/toolkit.db` 86KB active vs `data/toolkit.db` 65KB stale) | Changed `BASE_DIR` to project root (one level above `src/`) |
| 4 | `src/logger_config.py` | Log dir resolved to `src/data/logs/` — logs written to wrong location | Fixed to project root → `data/logs/` |
| 5 | DB split | Active DB at `src/data/toolkit.db` (86 KB) while `data/toolkit.db` (65 KB) was stale | Migrated active DB to `data/toolkit.db`; removed `src/data/` |
| 6 | `main.py:1394` | `manager.db_path` AttributeError — `DataReadabilityManager` has `manager.db.db_path` not `manager.db_path` | Fixed attribute path |
| 7 | `main.py:1344` | `show_data_summary()` return value discarded — "Data Summary" menu shows blank | Captured return value and printed all metrics |
| 8 | Stale artifacts | Root `__pycache__/` (tombstones from old root-level layout), `src/downloads/` (created by old BASE_DIR on import) | Removed both |
| 9 | `README.md` | Repository structure showed modules at root — all are in `src/` | Updated structure tree and developer import examples |

---

## Features Verified Working
- 27/27 automated tests passing
- Module imports: config, db_manager, cycle_manager, bulk_website_importer, data_manager, resilience
- Path resolution: DATA_DIR, DOWNLOADS_DIR, log_dir all resolve to project root directories
- DB at `data/toolkit.db` (single source of truth, 86 KB, active)
- Cycle config read from `data/automation/cycle_config.json`
- Bat options 3, 4, 5 importable with PYTHONPATH set
- Data summary menu now displays metrics
- Search data no longer crashes with AttributeError

---

## Phase 2 Fixes (2026-05-13 — second pass)

| # | File | Bug | Fix |
|---|------|-----|-----|
| 10 | `src/db_manager.py` | `sync_config_to_websites()` used `INSERT OR REPLACE` → reset `total_links_found`/`total_photos_downloaded` to 0 on every save | Changed to upsert: `ON CONFLICT(name) DO UPDATE SET url=..., enabled=...` (stats preserved) |
| 11 | `src/db_manager.py` | `save_websites()` never called `sync_config_to_websites()` → `websites` table always stale → link spider can't find website_id → links table always empty | Added `self.sync_config_to_websites()` call in `save_websites()` |
| 12 | `src/link_spider.py` | `_update_website_stats()` treated `cfg.websites` (a list) as a dict → KeyError, silently did nothing | Removed the broken method |
| 13 | `src/utils.py` | `validate_website_url()` made live HTTP HEAD request → blocked by Cloudflare, hangs offline, fails for valid sites | Changed to format-only check (valid URL structure, valid netloc with `.`) |
| 14 | `main.py` | `.env` file present but `load_dotenv()` never called → `USE_TOR`, `SERPER_API_KEY`, `WEBSITE_LOG_LEVEL` silently unused | Added `load_dotenv()` call after sys.path setup; installed `python-dotenv` |
| 15 | `src/photo_scraper.py` | `total_photos_downloaded` per-site counter never updated in DB | Added `_update_site_stats_in_db()` called in `finally` block of `scrape_website_images()` |
| 16 | `src/photo_scraper.py` | `TOOLKIT_ROOT` resolved to `src/` → fallback paths wrong | Fixed to resolve to project root via `.parent.parent` |

## Known Gaps (Not Changed — By Design or Pre-Existing)

| Gap | Impact | Priority |
|-----|--------|----------|
| `data/websites_config_backup.json` has old nested settings format | Old settings (max_depth:100) not merged into active DB | Low (use Settings menu to re-apply) |

---

## Running Tests
```bat
.venv\Scripts\python.exe -m pytest tests\ -v
```
Expected: **27 passed, 0 failed**
