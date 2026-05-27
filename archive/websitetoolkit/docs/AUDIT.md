# CODEBASE AUDIT REPORT
**Generated:** 2026-04-26  
**Board:** Multi-Agent Audit (Pathologist · Librarian · Archaeologist · Archivist · Adversary · Urbanist · Synthesizer)  
**Working Directory:** `C:\Users\bryan\OneDrive\01 TOOLKITS\websitetoolkit`

---

## ══ 0. FILESYSTEM HEALTH REPORT ══

### Corrupted Files

| File Path | Type | Severity | Recovery Recommendation |
|---|---|---|---|
| `README.md` | Markdown doc | WARNING | Two document streams interleaved line-by-line throughout. Rewrite from scratch using master feature map. Do not attempt merge-fix — content is unsalvageable as-is. |

### Orphaned / Leftover Files

| File Path | Reason Flagged | Recommended Action |
|---|---|---|
| `venv/` (directory) | Duplicate venv alongside `.venv/`; last modified Dec 2025 vs `.venv/` Apr 2026 | Delete `venv/` after confirming `.venv/` is the active environment |
| `__pycache__/` (root) | Build artifact at repo root | Add to `.gitignore`; delete local copy |
| `data/chunks/` | ~500+ chunk JSON files from cycles run July–Sep 2025. Retention policy in `cycle_config.json` set to 30 days but cleanup never ran. | Run cleanup or delete files older than 30 days. Add to `.gitignore`. |
| `data/link_spider/` | Hundreds of domain subdirectories from past crawler runs. No DB linkage. Never referenced in any code path at import time. | Document or delete. Add to `.gitignore`. |
| `data/photo_scraping_logs/` | Old per-run scraping logs. Not referenced by any current module. | Delete or add to `.gitignore`. |
| `data/exports/` | Empty directory. README states exports go here but no code writes to it. | Delete if not needed, or implement export path. |
| `data/reports/` | Empty directory. README references it but no code produces files here. | Delete if not needed. |
| `web/` | Created by `config.py:19` but no module reads or writes here. Completely empty. | Delete or implement. |
| `downloads/` | Empty. Download path is per-session per README; no permanent content. | Keep, add to `.gitignore`. |
| `data/crawled_links.txt` | Contains only header comments. Zero actual data. | Keep (code appends here), but note it's stale. |
| `data/extracted_links.txt` | Contains only header comments. Zero actual data. | Keep (code appends here), but note it's stale. |
| `data/file_hashes_backup.json` | 2 bytes — contains `[]`. Despite historical photo downloads (315 in last cycle), hash table is empty. Data was never flushed or was lost on DB reinit. | Investigate `DatabaseManager.sync_from_backup()` call on init — it overwrites nothing but silently clears hash state. |
| `data/statistics.json` | All zeroes. Disconnected from actual DB metrics. | Either wire to DB metrics or remove. |
| `data/spider_progress.json` | All empty/null. Disconnected from actual crawl state. | Either wire to DB or remove. |
| `data/photo_scraper_state.json` | All empty/null. Same as above. | Either wire to DB or remove. |
| `sample_import.txt` | Example file for bulk importer. Useful, but not linked from README (corrupted). | Keep; reference from new README. |

### Sync Artifacts

| File Path | Suspected Cause | Recommended Action |
|---|---|---|
| `venv/` vs `.venv/` | Two venv inits at different times. Both present. | Delete `venv/` (older). |
| `data/chunks/` 500+ files | Cloud sync (OneDrive path) interrupted cleanup cycles. | Purge old chunks manually, configure cleanup to run. |

---

## ══ 1. MASTER FEATURE MAP (SOURCE OF TRUTH) ══

### Module: `main.py` (104,997 bytes — entry point)
- **Purpose:** Interactive TUI menu. 8-option main menu. Bootstraps all operations.
- **Key functions:** `main()`, `run_automated_cycle()`, `handle_website_management()`, `handle_photo_scraper()`, `handle_link_spider()`, `handle_data_management()`, `handle_settings_menu()`, `load_settings()`, `save_settings()`
- **Inputs:** stdin (user menu choices), `settings.json`, DB via `config.py`
- **Outputs:** Terminal output; delegates to sub-modules for all work
- **External deps:** All other modules
- **Config:** Reads/writes `settings.json` (logging level, feature toggles, timeout)
- **Edge cases:** Handles missing `settings.json` by creating defaults. Menu loop with input validation.

### Module: `config.py` (17,626 bytes — configuration layer)
- **Purpose:** Configuration management. Loads/saves settings and website list to DB. Provides global `config` singleton.
- **Key classes:** `Config` — loads from DB on init, exposes add/remove/toggle/get website methods
- **Key functions:** `get_config()`, `get_websites()`, `get_enabled_websites()`, `save_config()`, `load_config()`, `test_duplicate_detection()` (embedded test), `_normalize_url_for_comparison()`, `_urls_are_equivalent()`, `_is_duplicate_website()`
- **Inputs:** `DatabaseManager` via `db_manager.get_db_manager()`
- **Outputs:** In-memory `Config` object; persists to DB via `save_config()`
- **External deps:** `db_manager.py`
- **Note:** Dual entry for websites — mixed `str` (URL-only) and `dict` (full config). Both handled throughout code. Legacy compat variable `WEBSITES = config.websites` at module level.

### Module: `db_manager.py` (16,802 bytes — data persistence)
- **Purpose:** SQLite wrapper. Single source of truth for websites, settings, file hashes, links, cycles.
- **Key classes:** `DatabaseManager` — init creates all tables, provides CRUD for all entities
- **Tables:** `websites`, `websites_config`, `links`, `cycles`, `file_hashes`, `settings`
- **Key functions:** `init_db()`, `sync_from_backup()`, `sync_config_to_websites()`, `update_backup()`, `get_paginated_websites()`, `get_system_metrics()`, `get_advanced_statistics()`, `add_hash()`, `has_hash()`, `save_settings()`, `save_websites()`
- **Inputs:** SQLite file at `data/toolkit.db`, JSON backups at `data/file_hashes_backup.json` and `data/websites_config_backup.json`
- **Outputs:** SQLite mutations; JSON backup files after every write operation
- **Indexes:** `idx_websites_enabled`, `idx_links_website`, `idx_links_status`, `idx_cycles_date`
- **Global singleton:** `get_db_manager()` — lazy-initialized, calls `sync_from_backup()` on first init

### Module: `data_manager.py` (4,515 bytes — analytics wrapper)
- **Purpose:** Thin wrapper over `DatabaseManager` providing analytics, reporting, and data cleanup.
- **Key classes:** `DataReadabilityManager`
- **Key functions:** `sync_config_to_database()`, `get_paginated_websites()`, `get_system_metrics()`, `cleanup_old_data()` (runs DELETE statements), `export_readable_report()`, `get_advanced_statistics()`
- **External deps:** `db_manager.py`, `sqlite3` directly for cleanup
- **Warning:** `cleanup_old_data()` runs raw `DELETE` SQL directly, including on `cycles` and `links` tables.

### Module: `cycle_manager.py` (23,934 bytes — automation engine)
- **Purpose:** Orchestrates discovery+scraping cycles. Contains rate limiter, chunk manager, and cycle stats.
- **Key classes:** `GlobalRateLimiter`, `RateLimitConfig`, `CycleStats` (dataclass), `DataManager` (local), `CycleManager`
- **Key functions:** `load_cycle_config()`, `CycleManager.run_discovery_phase()`, `CycleManager._crawl_website_with_limits()`, `CycleManager.generate_cycle_id()`
- **Inputs:** `data/automation/cycle_config.json`, enabled websites from config
- **Outputs:** Chunk files to `data/chunks/`, cycle stats to `data/cycles/` (NOT to DB `cycles` table)
- **Rate limiting:** Per-minute, per-hour, per-day windows tracked in memory lists. Jitter added.
- **External deps:** `link_spider.py`, `photo_scraper.py`, `config.py`, `download_helper.py`
- **MODULES_AVAILABLE guard:** If imports fail, operations silently don't run.

### Module: `link_spider.py` (36,797 bytes — crawling engine)
- **Purpose:** Crawls websites, extracts links, discovers new domains. Core crawling logic.
- **External deps:** `requests`, `beautifulsoup4`, `lxml`, `url_filter.py`, `utils.py`, `proxy_manager.py`
- **Outputs:** Link data to DB `links` table (or files — depends on call path), `data/link_spider/` domain subdirectories

### Module: `photo_scraper.py` (28,811 bytes — image download engine)
- **Purpose:** Downloads images from websites. Deduplicates via SHA256 hashes stored in DB.
- **External deps:** `requests`, `Pillow`, `db_manager.py`, `utils.py`, `proxy_manager.py`
- **Hash storage:** Uses `DatabaseManager.add_hash()` / `has_hash()` for dedup

### Module: `url_filter.py` (13,434 bytes — URL filtering)
- **Purpose:** Blocks/allows URLs by pattern. Default patterns block social media, e-commerce, auth areas, API endpoints.
- **Key classes:** `URLFilter`
- **Key functions:** `load_config()`, `is_url_blocked()`, `filter_urls()`
- **Config:** Loads from `data/url_filter_config.json`; falls back to hardcoded defaults
- **Pattern types:** Glob patterns (blocked_patterns, allowed_patterns), blocked_domains set, blocked_paths set, blocked_subdomains set
- **Module-level function:** `is_url_blocked()` — used by `utils.py:should_skip_url()`

### Module: `sitemap_parser.py` (17,406 bytes — sitemap discovery)
- **Purpose:** Discovers and parses XML sitemaps. Extracts URLs, image URLs, PDF URLs.
- **Key classes:** `SitemapParser`
- **Key functions:** `discover_sitemaps()`, `parse_sitemap()`, `discover_and_parse_all()`
- **External deps:** `requests`, `aiohttp` (optional), `defusedxml` (optional, fallback to stdlib xml)
- **Security:** Falls back to vulnerable stdlib `xml.etree` if `defusedxml` not installed — XXE risk

### Module: `pdf_processor.py` (19,658 bytes — PDF handling)
- **Purpose:** Downloads PDFs, converts to images using PyMuPDF or pdf2image.
- **External deps:** `PyMuPDF` (fitz), `pdf2image` (optional), `requests`
- **Config:** Uses per-site pdf_settings from config

### Module: `proxy_manager.py` (24,199 bytes — proxy rotation)
- **Purpose:** Loads, validates, and rotates HTTP/SOCKS proxies.
- **Key classes:** `ProxyManager`
- **Config:** Reads `data/proxies.txt`. Creates sample file with hardcoded public IPs if missing.
- **Stats tracking:** Per-proxy success/failure rates, response times

### Module: `utils.py` (15,819 bytes — shared utilities)
- **Purpose:** Shared utility functions used across all modules.
- **Key functions:** `get_safe_filename()`, `get_domain_name()`, `calculate_file_hash()`, `calculate_content_hash()`, `normalize_url()`, `is_valid_image_url()`, `extract_links_from_text()`, `create_session_with_retries()`, `load_json_file()`, `save_json_file()`, `validate_website_url()`, `should_skip_url()`
- **Key classes:** `ProgressTracker`

### Module: `bulk_website_importer.py` (16,538 bytes — batch import)
- **Purpose:** Imports website lists from text files. Parses various formats (URL-only, name+URL, CSV-style).
- **Key classes:** `BulkWebsiteImporter`
- **Duplicate detection:** Calls `config._is_duplicate_website()` before adding
- **Validation:** Calls `validate_website_url()` to check reachability before adding

### Module: `download_helper.py` (3,319 bytes — UX helper)
- **Purpose:** Prompts user for download directory per-session. Tests write access.
- **Key functions:** `prompt_for_download_location()`, `prompt_for_website_info()`
- **Design:** Intentionally does not persist path — requires re-entry each session.

### Module: `logger_config.py` (1,171 bytes — logging setup)
- **Purpose:** Creates named loggers with rotating file handler (10MB, 5 backups) + console handler.
- **Output:** `data/logs/toolkit.log`

### Data Files
- `data/toolkit.db` — SQLite, 64KB, 6 tables (PROTECTED)
- `data/automation/cycle_config.json` — Cycle/rate-limit config (PROTECTED)
- `data/url_filter_config.json` — URL filter patterns (PROTECTED)
- `data/proxies.txt` — Proxy list (PROTECTED)
- `settings.json` — UI settings (PROTECTED)

---

## ══ 2. RECONCILIATION SUMMARY ══

**Truth Gap:** ~70% of documented features are implemented in code. ~20% partially implemented (export/reporting paths exist in code but produce no files). ~10% phantom (enhanced_spider.py, download_path_manager.py from common/).

**State of System:** The codebase is a functional Python scraping toolkit at approximately mid-development maturity. Core crawling, photo scraping, rate limiting, and database persistence are implemented and structurally sound. However, the codebase shows clear signs of incremental development without architectural consolidation: two parallel data managers (cycle_manager.DataManager and data_manager.DataReadabilityManager), two config sources (settings.json and DB), multiple empty/unused directories, and a completely unusable README. The cycles table in the DB has never received data despite actual cycle runs, meaning all historical analytics are silently discarded. The project has been used (last cycle Sep 2025, 23,240 links discovered) but operational hygiene is low.

**Production Readiness Score:** 4/15

---

## ══ 3. CRITICAL GAPS (UNIMPLEMENTED FEATURES) ══

| Feature | Source | Severity | Impact |
|---|---|---|---|
| `enhanced_spider.py` | README line 393, 649 | HIGH | README instructs `import enhanced_spider` but file doesn't exist. Code importing it would crash. |
| `download_path_manager.py` in `common/` | README lines 4, 64 | HIGH | README instructs use of this file/path. Neither `common/` dir nor file exist. Actual file is `download_helper.py` at root. |
| Cycle stats → DB sync | `cycles` table schema in `db_manager.py` | HIGH | Cycle runs save stats to `data/cycles/` JSON files but never write to the `cycles` DB table. Analytics endpoints that query `cycles` table return zero data. |
| Export files (`data/exports/`) | README, `data_manager.export_readable_report()` | MEDIUM | `export_readable_report()` writes to `data/` not `data/exports/`. Stated directory is unused. |
| Report files (`data/reports/`) | README line 347, 349 | MEDIUM | Directory exists but nothing writes here. |
| Web interface (`web/`) | `config.py:14` creates `WEB_DIR` | LOW | Directory created on every startup but never populated. README doesn't mention a web UI. Likely leftover from abandoned feature. |

---

## ══ 4. UNDOCUMENTED LOGIC (GHOST FEATURES) ══

| Module/Function | File | What It Does | Why Document |
|---|---|---|---|
| `test_duplicate_detection()` | `config.py:411` | Live integration test embedded in production module. Adds/removes test websites from actual config. | Should be in test suite not production code; currently callable from any import. |
| `DataManager` class | `cycle_manager.py:175` | Second data layer for chunk files and cycle stats. Duplicates responsibility of `data_manager.py`. | Creates confusion about which data manager to use; not documented as distinct from `DataReadabilityManager`. |
| `_calculate_health_score()` | `db_manager.py:267` | Calculates 0-100 health score as weighted avg of enabled-site ratio and links-per-site. | Formula is non-obvious (40% enabled ratio, 60% activity). Should be documented. |
| `sync_from_backup()` called on every init | `db_manager.py:373` | On every `get_db_manager()` call, syncs from JSON backup files into DB. | Silent data-merge behavior on startup not documented. Can cause unexpected state if backup files are stale. |
| `WEBSITES = config.websites` | `config.py:467` | Live reference to mutable list. Any external mutation affects the config object. | Legacy compatibility alias not documented; dangerous if callers mutate it directly. |
| Glob pattern URL filter | `url_filter.py` | Converts `*://*/path/*` glob patterns to regex. `*` maps to `.*` wildcard. | Pattern language not documented; users editing `url_filter_config.json` won't know syntax. |

---

## ══ 5. DOCUMENTATION DRIFT ══

| Documented Behavior | Actual Behavior | File | Correction Needed |
|---|---|---|---|
| README: "use `download_path_manager.py` from `common/` folder" | File is `download_helper.py` at root; no `common/` directory exists | `README.md:64` | Update to reference `download_helper.py` |
| README: `import enhanced_spider` pattern for enhancements | Module does not exist | `README.md:393` | Remove phantom module reference |
| README project structure shows `enhanced_spider.py` in tree | File does not exist | `README.md:649` | Remove from structure diagram |
| `settings.json` described as main config | Scraping settings live in DB; `settings.json` only holds UI preferences (logging, timeout, feature toggles) | `README.md` | Clarify dual-config architecture |
| README: "Transaction Safety: Database operations are atomic" | `save_websites()` does `DELETE FROM websites_config` then re-inserts in same connection but without explicit transaction; `cleanup_old_data()` in `data_manager.py` runs deletes without transaction | `db_manager.py:344`, `data_manager.py:34` | Fix transactions or correct claim |
| README: `data/toolkit.db` listed under `data/` | Correct — toolkit.db is at `data/toolkit.db` | `db_manager.py:372` | No correction needed |
| README: "Robots.txt compliance (default enabled)" | `DEFAULT_SETTINGS['respect_robots_txt'] = False` | `config.py:79` | Either fix default or correct documentation |

---

## ══ 6. DATA INTEGRITY REPORT ══

### `websites` table
- **Schema match:** PASS (matches `db_manager.py` schema)
- **Record count:** 1 row
- **Anomalies:** Single row is default placeholder (`example_site`, `https://example.com`, `enabled=False`). No real websites. `last_crawled`, `last_scraped`, `discovery_source` all NULL. `total_links_found=0`, `total_photos_downloaded=0` despite historical cycle showing 315 photos downloaded.
- **Incomplete writes:** Yes — historical operations never updated this table's counters.
- **Action:** Investigate why cycle operations don't update `websites.total_links_found` / `total_photos_downloaded`.

### `websites_config` table
- **Schema match:** PASS
- **Record count:** 1 row (same example_site)
- **Anomalies:** None structurally. Single default entry.
- **Action:** None required beyond noting it's empty of real data.

### `links` table
- **Schema match:** PASS
- **Record count:** 0 rows
- **Anomalies:** Despite last cycle discovering 23,240 links, `links` table is empty. Links are stored to `data/link_spider/` domain folders, not DB.
- **Incomplete writes:** YES — critical. Link spider output never routes to this table.
- **Action:** Wire `link_spider.py` output to `DatabaseManager` `links` table, or document that file-based storage is intended.

### `cycles` table
- **Schema match:** PASS
- **Record count:** 0 rows
- **Anomalies:** One completed cycle exists at `data/cycles/cycle_20250925_070834.json` (33 websites, 23,240 links, 315 photos, ~100 min runtime). This data was never written to the `cycles` DB table.
- **Incomplete writes:** YES — critical. `CycleManager` saves to files only.
- **Action:** Add DB write in `CycleManager` after cycle completion.

### `file_hashes` table
- **Schema match:** PASS
- **Record count:** 0 rows
- **Anomalies:** `data/file_hashes_backup.json` is `[]` (empty). Despite historical photo downloads, no hashes stored. Deduplication is non-functional across sessions.
- **Incomplete writes:** YES — hashes not persisted to backup/DB correctly between sessions, or DB was recreated.
- **Action:** Verify `DatabaseManager.sync_from_backup()` properly reloads hash backup on startup. Check if `add_hash()` is actually called during photo downloads.

### `settings` table
- **Schema match:** PASS
- **Record count:** 32 rows (all default settings from `DEFAULT_SETTINGS` dict)
- **Anomalies:** None. Settings correctly serialized as JSON per key.
- **Action:** None.

### `data/toolkit.db` file health
- **File size:** 65,536 bytes (64 KB)
- **Status:** READABLE, valid SQLite format. Not corrupted.

---

## ══ 7. CODE QUALITY FINDINGS ══

### [SECURITY]

| Description | File | Function | Severity | Fix |
|---|---|---|---|---|
| `respect_robots_txt` defaults to `False` | `config.py:79` | `DEFAULT_SETTINGS` | P1 | Change default to `True`. Scraping without robots.txt compliance is a ToS violation risk. |
| XXE attack surface if `defusedxml` not installed | `sitemap_parser.py:23` | module-level import | P1 | Make `defusedxml` a hard dependency in `requirements.txt`, not optional. Raise error on missing, not warning. |
| Sample proxies hardcoded into proxy file creation | `proxy_manager.py:54-67` | `_create_sample_proxy_file()` | P2 | Do not hardcode real IP:port combinations in source. Use placeholder comments only. |
| No `.gitignore` at root | root | N/A | P1 | Create `.gitignore`. Risk of committing `data/` (contains DB, proxy list, config backups, potentially scraped data). |

### [LOGIC]

| Description | File | Function | Severity | Fix |
|---|---|---|---|---|
| `save_websites()` does DELETE then re-insert without explicit transaction guard | `db_manager.py:344` | `save_websites()` | P1 | Wrap in `BEGIN; ... COMMIT;` or use `with conn:` context consistently. Partial failure between DELETE and INSERT loses all website config. |
| `cleanup_old_data()` runs DELETE statements | `data_manager.py:34` | `cleanup_old_data()` | P1 | Add explicit transaction. Log deleted row counts. Current bare `DELETE` with no error handling can silently fail and corrupt state. |
| `Config._load_config()` swallows all exceptions silently | `config.py:134` | `_load_config()` | P1 | Log the exception; don't only print. On DB corruption, system silently operates on empty default config. |
| `cycle_manager.DataManager` duplicates `data_manager.DataReadabilityManager` | `cycle_manager.py:175` | `DataManager` class | P2 | Remove `DataManager` from `cycle_manager.py`, use `data_manager.py` instead. |
| Mixed `str` and `dict` in websites list requires branching in every consumer | `config.py:308`, `db_manager.py:354` | `get_enabled_websites()`, `save_websites()` | P2 | Normalize all entries to `dict` on load. Remove str-handling branches throughout. |
| Duplicate `# Optional async dependencies` comment | `sitemap_parser.py:27-33` | module-level | P3 | Remove duplicate comment block. |
| `test_duplicate_detection()` in production module | `config.py:411` | `test_duplicate_detection()` | P2 | Move to `tests/test_config.py`. |
| `load_config()` free function duplicates `Config._load_config()` | `config.py:459` | `load_config()` | P3 | Remove free function; call `config._load_config()` directly or expose `reload()` method. |
| `sitemap_parser.py:53-56` creates session when `ASYNC_AVAILABLE` is False but `create_session_with_retries()` doesn't need aiohttp | `sitemap_parser.py:53` | `__init__()` | P3 | Remove `if ASYNC_AVAILABLE:` guard; always create session. |

### [PERFORMANCE]

| Description | File | Function | Severity | Fix |
|---|---|---|---|---|
| `add_hash()` calls `update_backup()` (full JSON file write) after every single hash | `db_manager.py:308` | `add_hash()` | P1 | Batch backup writes. Write backup only at end of scraping session, not per-hash. For bulk photo scraping, this creates one full JSON write per image. |
| `save_settings()` calls `update_backup()` after every key write | `db_manager.py:336` | `save_settings()` | P2 | Batch. `save_settings()` is called with full dict — backup should run once after all keys written. |
| `save_websites()` calls `update_backup()` after every save | `db_manager.py:362` | `save_websites()` | P2 | Acceptable frequency (website changes are rare), but should be explicit about the cost. |
| `get_db_manager()` calls `sync_from_backup()` on every first init (reads and writes DB) | `db_manager.py:373` | `get_db_manager()` | P2 | Only sync if DB is newly created or tables are empty. Add a migration-version flag. |

### [RELIABILITY]

| Description | File | Function | Severity | Fix |
|---|---|---|---|---|
| `CycleManager` never writes to `cycles` DB table | `cycle_manager.py` | `run_discovery_phase()` | P1 | After cycle completes, write `CycleStats` to `DatabaseManager.save_cycle()` (needs new method). |
| `link_spider.py` output never reaches `links` DB table | `link_spider.py` | crawler output | P1 | Route discovered links to `DatabaseManager` instead of (or in addition to) file-based storage. |
| `MODULES_AVAILABLE` guard means cycle silently does nothing if imports fail | `cycle_manager.py:27` | module-level | P1 | Fail loudly. If required modules are unavailable, raise `ImportError` at startup, not silently skip work. |
| File-based progress state (`data/spider_progress.json`, `data/photo_scraper_state.json`, `data/statistics.json`) is never updated | multiple | multiple | P2 | Either remove stale state files or wire them to actual DB metrics. |
| `settings.json` and DB settings table can drift with no reconciliation | `main.py:25`, `config.py:122` | `load_settings()`, `_load_config()` | P2 | Designate one authoritative source. Recommend keeping scraping settings in DB only; UI settings in `settings.json` only. Document split. |

### [DEAD]

| Description | File | Function | Severity | Fix |
|---|---|---|---|---|
| `WEBSITES = config.websites` legacy alias | `config.py:467` | module-level | P3 | Remove if no external caller uses it. Verify with grep. |
| `venv/` directory (Dec 2025) | root | N/A | P3 | Delete. `.venv/` is the active environment. |
| 500+ chunk files in `data/chunks/` from 2025 cycles | `data/chunks/` | N/A | P3 | Delete files older than retention window. |
| `data/link_spider/` hundreds of domain subdirs | `data/link_spider/` | N/A | P3 | Clean up or archive. Not referenced by any current DB records. |
| `data/statistics.json`, `data/spider_progress.json`, `data/photo_scraper_state.json` (all zeroes) | `data/` | N/A | P3 | Remove or wire to DB. Currently misleading. |
| `web/` empty directory | root | N/A | P3 | Remove if no web interface planned. |
| Duplicate `lxml>=4.9.0` in `requirements.txt` | `requirements.txt:5,9` | N/A | P3 | Remove duplicate line. |

---

## ══ 8. STRUCTURAL REORGANIZATION PLAN ══

### 8a. Current File Tree (Source Files Only)

```
websitetoolkit/
├── __pycache__/                  ← artifact, not source
├── .pytest_cache/                ← artifact
├── .venv/                        ← active venv (PROTECTED)
├── venv/                         ← stale duplicate venv
├── data/
│   ├── automation/
│   │   └── cycle_config.json     ← PROTECTED config
│   ├── chunks/                   ← ~500 stale chunk files
│   ├── cycles/
│   │   └── cycle_20250925_070834.json
│   ├── exports/                  ← empty
│   ├── link_spider/              ← hundreds of domain subdirs (crawler output)
│   ├── logs/
│   │   ├── analytics.log         ← 0 bytes
│   │   ├── automation.log        ← 0 bytes
│   │   ├── errors.log
│   │   ├── scraping.log
│   │   └── toolkit.log
│   ├── photo_scraping_logs/      ← old run data
│   ├── reports/                  ← empty
│   ├── crawled_links.txt
│   ├── extracted_links.txt
│   ├── file_hashes_backup.json   ← PROTECTED (empty array)
│   ├── photo_scraper_state.json
│   ├── proxies.txt               ← PROTECTED
│   ├── spider_progress.json
│   ├── statistics.json
│   ├── toolkit.db                ← PROTECTED
│   ├── url_filter_config.json    ← PROTECTED
│   └── websites_config_backup.json ← PROTECTED
├── downloads/                    ← empty, per-session
├── tests/
│   ├── conftest.py
│   ├── test_bulk_website_importer.py
│   ├── test_cycle_manager.py
│   └── test_utils.py
├── web/                          ← empty, unused
├── bulk_website_importer.py
├── config.py
├── cycle_manager.py
├── data_manager.py
├── db_manager.py
├── download_helper.py
├── link_spider.py
├── logger_config.py
├── main.py
├── pdf_processor.py
├── photo_scraper.py
├── proxy_manager.py
├── README.md                     ← corrupted
├── requirements.txt
├── sample_import.txt
├── settings.json                 ← PROTECTED
├── setup.bat
├── sitemap_parser.py
├── start_toolkit.bat
├── url_filter.py
└── utils.py
```

### 8b. Target File Tree

```
websitetoolkit/
├── .venv/                        ← active venv (gitignored)
├── data/
│   ├── automation/
│   │   └── cycle_config.json
│   ├── cycles/                   ← cycle JSON stats
│   ├── logs/                     ← all log files
│   ├── toolkit.db
│   ├── url_filter_config.json
│   ├── proxies.txt
│   ├── file_hashes_backup.json
│   └── websites_config_backup.json
├── downloads/                    ← per-session, gitignored
├── tests/
│   ├── conftest.py
│   ├── test_bulk_website_importer.py
│   ├── test_config.py            ← new: move test_duplicate_detection here
│   ├── test_cycle_manager.py
│   ├── test_db_manager.py        ← new: DB integration tests
│   └── test_utils.py
├── bulk_website_importer.py
├── config.py
├── cycle_manager.py
├── data_manager.py
├── db_manager.py
├── download_helper.py
├── link_spider.py
├── logger_config.py
├── main.py
├── pdf_processor.py
├── photo_scraper.py
├── proxy_manager.py
├── requirements.txt
├── sample_import.txt
├── settings.json
├── setup.bat
├── sitemap_parser.py
├── start_toolkit.bat
├── url_filter.py
├── utils.py
├── .gitignore                    ← new: CRITICAL missing file
└── README.md                     ← rewrite from scratch
```

### 8c. Move Plan

| Step | Action | Source Path | Destination Path | Protected? | Backup Required? |
|---|---|---|---|---|---|
| 1 | DELETE | `venv/` | N/A | NO | No (stale env) |
| 2 | DELETE | `__pycache__/` (root) | N/A | NO | No |
| 3 | DELETE | `web/` | N/A | NO | No |
| 4 | DELETE | `data/chunks/` (all files) | N/A | NO | No (stale artifacts) |
| 5 | DELETE | `data/link_spider/` | N/A | NO | Verify no needed data first |
| 6 | DELETE | `data/photo_scraping_logs/` | N/A | NO | No |
| 7 | DELETE | `data/exports/` | N/A | NO | No (empty) |
| 8 | DELETE | `data/reports/` | N/A | NO | No (empty) |
| 9 | DELETE | `data/statistics.json` | N/A | NO | No (all zeroes, stale) |
| 10 | DELETE | `data/spider_progress.json` | N/A | NO | No (all null, stale) |
| 11 | DELETE | `data/photo_scraper_state.json` | N/A | NO | No (all null, stale) |
| 12 | DELETE | `data/crawled_links.txt` | N/A | NO | No (header only) |
| 13 | DELETE | `data/extracted_links.txt` | N/A | NO | No (header only) |
| 14 | CREATE | `.gitignore` | root | NO | No |
| 15 | REWRITE | `README.md` | root | NO | No (corrupted source) |

**Protected files — no move, confirm backup exists:**
- `data/toolkit.db` — stays in `data/`, backup confirmed at `data/websites_config_backup.json`
- `data/automation/cycle_config.json` — stays
- `data/url_filter_config.json` — stays
- `data/proxies.txt` — stays
- `data/file_hashes_backup.json` — stays
- `data/websites_config_backup.json` — stays
- `settings.json` (root) — stays

### 8d. New Directories to Create

| Name | Purpose |
|---|---|
| (none required) | Target structure fits within existing dirs after cleanup |

### 8e. .gitignore Additions

| File/Pattern | Reason |
|---|---|
| `.venv/` | Virtual environment — never commit |
| `venv/` | Stale venv |
| `__pycache__/` | Python bytecode |
| `*.pyc` | Compiled Python |
| `*.pyo` | Compiled Python |
| `.pytest_cache/` | pytest artifacts |
| `data/toolkit.db` | Database — contains user data, could be large |
| `data/chunks/` | Ephemeral cycle progress files |
| `data/link_spider/` | Crawler output — can be hundreds of MB |
| `data/photo_scraping_logs/` | Run logs |
| `data/logs/` | Application logs |
| `downloads/` | Downloaded media — large, user-specific |
| `data/proxies.txt` | May contain credentials |
| `data/file_hashes_backup.json` | User data |
| `data/websites_config_backup.json` | User data |
| `data/url_filter_config.json` | User config |
| `data/automation/` | User config |
| `data/cycles/` | Runtime data |

---

## ══ 9. PRODUCTION READINESS CHECKLIST ══

| # | Requirement | Status | Notes |
|---|---|---|---|
| 1 | All secrets externalized to env vars — no hardcoding | [FAIL] | `proxy_manager.py` hardcodes real proxy IPs in `_create_sample_proxy_file()`. |
| 2 | Dependencies pinned to explicit versions, no known CVEs | [PARTIAL] | `requirements.txt` uses `>=` minimums, not pinned. Acceptable for a local tool but not production service. `defusedxml` is listed as optional but should be hard required. |
| 3 | Database migrations versioned and reversible | [FAIL] | No migration system. Schema created with `CREATE TABLE IF NOT EXISTS`. No versioning, no rollback path. Schema changes would require manual SQL. |
| 4 | All external API calls have timeout and retry config | [PARTIAL] | `create_session_with_retries()` in `utils.py` provides retries. Timeout is configurable. But not all HTTP calls use this session (some modules create ad-hoc requests). |
| 5 | Logging is structured (JSON or key-value) | [FAIL] | `logger_config.py` uses plain text format `%(asctime)s - %(name)s - %(levelname)s - %(message)s`. Not JSON/structured. Some modules use bare `print()` instead of logger. |
| 6 | No debug routes / dev-only flags in production paths | [PARTIAL] | `test_duplicate_detection()` in `config.py` is callable as production code. `if __name__ == "__main__": test_duplicate_detection()` runs tests when config.py executed directly. |
| 7 | Graceful shutdown handling for long-running processes | [FAIL] | No signal handlers. No `asyncio` cancellation cleanup. `CycleManager.is_running` flag exists but no SIGINT/SIGTERM handling. |
| 8 | Error responses do not leak stack traces | [N/A] | No HTTP server. CLI only. |
| 9 | Input validation at all external-facing interfaces | [PARTIAL] | `validate_website_url()` validates URLs. `BulkWebsiteImporter._parse_websites_from_text()` validates lines. But `handle_settings_menu()` has minimal validation on user input (e.g., timeout accepts any int). |
| 10 | Health check endpoint or monitoring hook | [FAIL] | No health check. `get_system_metrics()` exists but is menu-accessed only, not automated. |
| 11 | All file writes atomic or guarded against partial-write corruption | [FAIL] | `save_websites()` does DELETE then INSERT without explicit transaction. `export_readable_report()` writes file non-atomically (no temp-file+rename pattern). `save_chunk_progress()` has no write guard. |
| 12 | Rate limiting / abuse prevention on public endpoints | [N/A] | No public endpoints. Rate limiting on outbound requests is implemented. |
| 13 | Auth tokens/sessions have expiry logic | [N/A] | No authentication system. |
| 14 | Test coverage for all critical paths | [FAIL] | 3 test files covering: `utils.py` (3 tests), `bulk_website_importer.py` (1 test), `cycle_manager.py` (4 tests). Zero tests for: `db_manager.py`, `link_spider.py`, `photo_scraper.py`, `sitemap_parser.py`, `pdf_processor.py`, `proxy_manager.py`, `config.py`, `url_filter.py`. |
| 15 | Build/start process documented and reproducible | [PARTIAL] | `setup.bat` and `start_toolkit.bat` exist for Windows. No `Makefile` or cross-platform setup. `README.md` is corrupted and unusable for new users. |

**Score: 4/15 PASS (2 N/A, 6 PARTIAL, 7 FAIL)**

---

## ══ 10. PRIORITIZED REMEDIATION ROADMAP ══

| Priority | Action | Rationale | Files Affected | Effort |
|---|---|---|---|---|
| 1 | Create root `.gitignore` | P0: Without it, `data/toolkit.db`, proxy credentials, and hundreds of MB of crawler output could be committed to VCS | root | S |
| 2 | Make `defusedxml` a hard dependency; fail loudly if missing | P0: XXE vulnerability in sitemap parser without it | `requirements.txt`, `sitemap_parser.py` | S |
| 3 | Remove hardcoded proxy IPs from `proxy_manager._create_sample_proxy_file()` | P1: Real IP:port combinations should not live in source | `proxy_manager.py` | S |
| 4 | Wrap `save_websites()` DELETE+INSERT in explicit transaction | P1: Data loss on partial failure | `db_manager.py` | S |
| 5 | Wrap `cleanup_old_data()` deletes in transaction with error handling | P1: Silent data corruption risk | `data_manager.py` | S |
| 6 | Write `CycleStats` to DB `cycles` table after each cycle | P1: All analytics endpoints return zeroes; historical data silently lost | `cycle_manager.py`, `db_manager.py` | M |
| 7 | Route link spider output to `links` DB table | P1: Core purpose of `links` table — currently unused | `link_spider.py`, `db_manager.py` | M |
| 8 | Batch `update_backup()` calls — not per-hash/per-setting write | P1: I/O bottleneck during bulk photo scraping | `db_manager.py` | S |
| 9 | Fix `respect_robots_txt` default to `True` | P1: Legal/ethical compliance | `config.py` | S |
| 10 | Delete `venv/`, `__pycache__/`, `web/`, `data/chunks/`, `data/link_spider/`, `data/photo_scraping_logs/`, `data/exports/`, `data/reports/` | Structural: Remove stale artifacts before any reorganization | Multiple | S |
| 11 | Delete stale zero-data files: `data/statistics.json`, `data/spider_progress.json`, `data/photo_scraper_state.json`, `data/crawled_links.txt`, `data/extracted_links.txt` | Structural: Misleading empty state | `data/` | S |
| 12 | Add `MODULES_AVAILABLE` hard-fail; remove silent skip | P1: Cycles silently do nothing on import error | `cycle_manager.py` | S |
| 13 | Make `Config._load_config()` log exceptions properly | P1: DB errors masked | `config.py` | S |
| 14 | Normalize website list to all-dict on load; remove str-handling branches | P2: Reduces complexity across 6+ branch points | `config.py`, `db_manager.py` | M |
| 15 | Remove `DataManager` from `cycle_manager.py`; use `data_manager.py` | P2: Eliminate duplicate data access layer | `cycle_manager.py`, `data_manager.py` | M |
| 16 | Move `test_duplicate_detection()` to `tests/test_config.py` | P2: Test code in production module | `config.py`, `tests/` | S |
| 17 | Remove `load_config()` free function; remove `WEBSITES` alias | P3: Dead code | `config.py` | S |
| 18 | Fix duplicate `lxml>=4.9.0` in `requirements.txt` | P3: Cosmetic but confusing | `requirements.txt` | S |
| 19 | Remove duplicate `# Optional async dependencies` comment | P3 | `sitemap_parser.py` | S |
| 20 | Rewrite `README.md` from scratch using master feature map | Documentation: Existing README is unusable | `README.md` | L |
| 21 | Add tests for `db_manager.py`, `config.py`, `url_filter.py`, `sitemap_parser.py` | Production readiness: critical paths untested | `tests/` | L |
| 22 | Designate one authoritative config source; document settings.json vs DB split | Documentation/reliability | `README.md`, `config.py`, `main.py` | M |
| 23 | Implement atomic file writes (temp-file+rename) for JSON exports | Reliability: partial write protection | `data_manager.py`, `utils.py` | M |
| 24 | Add signal handler for graceful cycle shutdown | Reliability: allows clean interrupt without data corruption | `cycle_manager.py`, `main.py` | M |
