# Bug Fix Log
**Generated:** 2026-04-26  
**Source:** AUDIT.md + direct codebase read  
**Format:** Status | ID | Severity | Root Cause | Impact

---

## P0 — Must Fix Before Any Commit

| ID | Status | File | Bug | Root Cause | Impact |
|----|--------|------|-----|------------|--------|
| B-01 | Fixed | root | No `.gitignore` exists | File never created | `data/toolkit.db`, `data/proxies.txt`, `data/websites_config_backup.json`, `data/url_filter_config.json`, `data/*.log`, and hundreds of MB of crawler output would be committed to VCS on first `git add .` |
| B-02 | Fixed | `sitemap_parser.py:19-24` | `defusedxml` import failure silently falls back to vulnerable stdlib `xml.etree.ElementTree` | Optional import with warning-only fallback | XXE (XML External Entity) attack possible when parsing untrusted sitemaps — attacker-controlled sitemap.xml can read arbitrary local files |

---

## P1 — Critical Correctness / Security

| ID | Status | File | Bug | Root Cause | Impact |
|----|--------|------|-----|------------|--------|
| B-03 | Fixed | `proxy_manager.py:53-72` | Real public IP:port combinations hardcoded in `_create_sample_proxy_file()` | Copy-paste from public proxy list | Source-controlled IPs are a ToS / legal risk; proxies rotate and die within hours, making entries instantly stale; misleads users into thinking proxies work |
| B-04 | Fixed | `config.py:78` | `respect_robots_txt` defaults to `False` | Default never corrected after initial draft | Scraper runs without robots.txt compliance by default — ToS violation risk on every new installation |
| B-05 | Fixed | `db_manager.py:303-309` | `add_hash()` calls `update_backup()` (full JSON file rewrite) after every single hash insertion | Backup called synchronously per-operation instead of batched | During photo scraping: 315 photos = 315 full rewrites of `file_hashes_backup.json`. I/O bottleneck degrades scraping throughput; can cause file contention |
| B-06 | Fixed | `cycle_manager.py:456`, `db_manager.py` | `CycleManager.run_cycle()` never writes `CycleStats` to the `cycles` DB table | `save_cycle_stats()` saves to `data/cycles/*.json` only; no DB counterpart method exists | All analytics queries against `cycles` table return zero rows. `get_system_metrics()` reports 0 total photos downloaded. Historical cycle data invisible to reporting layer |
| B-07 | Fixed | `cycle_manager.py:19-28` | `MODULES_AVAILABLE = False` silently disables all cycle operations with no error to caller | Import errors swallowed into flag; flag never checked before doing actual work in `run_cycle()` | Cycle runs appear to start, do nothing, and succeed silently when any required module is missing |
| B-08 | Fixed | `config.py:134-136` | `Config._load_config()` catches all exceptions and uses `print()` instead of logger | `except Exception as e: print(...)` | DB errors silently swallowed; system starts on empty default config with no log record; log aggregators (e.g., tooling) never see the failure |
| B-09 | Fixed | `config.py:14,19,117` | `web/` directory created on every startup | `WEB_DIR` constant + `os.makedirs()` at module load + `_ensure_directories()` | Empty directory persists in repo; creates confusion; no feature uses it |

---

## P2 — Quality / Maintainability

| ID | Status | File | Bug | Root Cause | Impact |
|----|--------|------|-----|------------|--------|
| B-10 | Fixed | `config.py:411-457` | `test_duplicate_detection()` is a live integration test embedded in production module | Never moved to test suite | Callable at runtime; adds test websites to real config; `if __name__ == "__main__"` guard runs it when `config.py` is executed directly |
| B-11 | Fixed | `config.py:252-292` | `add_website()` in simple mode appends raw `str` URL to `self.websites` instead of normalizing to `dict` | Two code paths: simple (str) and full (dict) | Requires branching str/dict checks in every consumer: `get_enabled_websites()`, `save_websites()`, `_is_duplicate_website()`, `link_spider._load_website_config()`, etc. |
| B-12 | Fixed | `cycle_manager.py:175-229` | `DataManager` class inside `cycle_manager.py` duplicates responsibility with `data_manager.DataReadabilityManager` | Second data layer added without consolidating the first | Two separate data managers create confusion about authoritative data source; `DataManager` handles chunk files and cycle JSON while `DataReadabilityManager` handles DB analytics — no clear boundary |
| B-13 | Fixed | `sitemap_parser.py:52-55` | Session only created when `ASYNC_AVAILABLE=True` but `create_session_with_retries()` doesn't require aiohttp | `if ASYNC_AVAILABLE:` guard around session init | `SitemapParser` fails silently (no session) when `aiohttp` is not installed, even though `requests`-based session works fine without it |
| B-14 | Fixed | `data_manager.py:55` | `cleanup_old_data()` uses `except Exception: continue` inside chunk file loop | Bare-except on file deletion | Deletion errors silently ignored; no count of failed deletions returned; could mask permission errors |
| B-15 | Fixed | `link_spider.py:116,199` | `link_spider.py` calls free function `load_config()` which re-calls `_load_config()` on each invocation | Free function duplicates reload logic unnecessarily | Minor: each call re-reads DB into the same global config; negligible in practice but semantically incorrect |

---

## P3 — Cosmetic / Low Risk

| ID | Status | File | Bug | Root Cause | Impact |
|----|--------|------|-----|------------|--------|
| B-16 | Fixed | `config.py:466-467` | `WEBSITES = config.websites` legacy alias lives at module level | Never removed after refactor | Mutable reference; not imported by any file (confirmed by grep) — dead code |
| B-17 | Fixed | `config.py:459-464` | `load_config()` free function duplicates `Config._load_config()` and is the wrong abstraction | Kept for backward compat with `link_spider.py` | Should be replaced: `link_spider.py` should use `get_config()` instead; then free function deleted |
| B-18 | Fixed | `requirements.txt:5,9` | `lxml>=4.9.0` appears twice | Copy-paste during edit | Confusing; `pip install` handles it silently but creates false impression of two distinct packages |
| B-19 | Fixed | `sitemap_parser.py:26-28` | Duplicate `# Optional async dependencies` comment on consecutive lines | Copy-paste artifact | Minor readability issue |
| B-20 | Fixed | `data/` | 500+ stale chunk files, domain spider dirs, empty dirs, zero-data JSON files (see AUDIT.md §0) | No cleanup policy enforced | Pollutes working directory; misleads any reader who inspects `data/`; potential OneDrive sync cost |

---

## Audit Corrections (vs AUDIT.md claims)

| AUDIT.md Claim | Actual State | Verdict |
|----------------|--------------|---------|
| "`save_websites()` DELETE+INSERT without explicit transaction guard" | Code uses `with sqlite3.connect(...) as conn:` which IS a transaction context manager in Python's sqlite3 — DELETE and INSERT are atomic | Overstated. Transaction IS present via context manager. No code change required; add clarifying comment |
| "`cleanup_old_data()` runs DELETE statements without transaction" | Same as above — `with conn:` wraps both DELETEs atomically | Overstated. Add logging of affected row counts (B-14) but transaction is already safe |
