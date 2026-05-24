# Implementation Tasks
**Generated:** 2026-04-26  
**Source:** bugfix.md + design.md  
**Execution order:** Waves 1→6 are safe to run sequentially. Wave 7+ require explicit approval.

Legend: `[ ]` Open · `[x]` Complete · `[~]` In Progress

---

## Wave 1 — Structural Cleanup

### T-01 · Create `.gitignore` · B-01 · P0
**Action:** Create `/.gitignore` at repo root with content from `design.md §4`.  
**Acceptance Criteria:**
- File exists at repo root
- Patterns include `.venv/`, `data/toolkit.db`, `data/proxies.txt`, `data/file_hashes_backup.json`, `data/url_filter_config.json`, `settings.json`, `__pycache__/`, `data/chunks/`, `data/link_spider/`, `downloads/`
- No test files (`test_*.py`) excluded

**Status:** [x]

---

### T-02 · Delete stale directories · B-20 · P3
**Action:** Delete the following (all confirmed empty or crawler-output only, no DB linkage):
- `venv/`
- `web/`
- `data/chunks/` (all contents)
- `data/link_spider/` (all contents)
- `data/photo_scraping_logs/`
- `data/exports/`
- `data/reports/`
- root `__pycache__/`

**Pre-condition:** Confirm `.venv/` is the active environment (check `start_toolkit.bat` — AUDIT confirms it uses `.venv`).  
**Acceptance Criteria:** None of the listed paths exist after execution.  

**Status:** [x]

---

### T-03 · Delete stale data files · B-20 · P3
**Action:** Delete:
- `data/statistics.json`
- `data/spider_progress.json`
- `data/photo_scraper_state.json`
- `data/crawled_links.txt`
- `data/extracted_links.txt`

**Pre-condition:** These are confirmed all-zeroes/all-null/header-only in AUDIT.md §0. Code appends to txt files but nothing reads them.  
**Acceptance Criteria:** Files do not exist. No import or code references them at runtime (confirmed: only `config.py` defines constants for them; no read paths exist).  

**Status:** [x]

---

## Wave 2 — Security Fixes

### T-04 · Hard-fail on missing defusedxml · B-02 · P0
**File:** `sitemap_parser.py:19-24`  
**Action:** Replace soft fallback with hard fail:
```python
# Before
try:
    import defusedxml.ElementTree as DET
    XML_PARSER = DET
except ImportError:
    XML_PARSER = ET
    logger.warning("WARNING: defusedxml not installed...")

# After
try:
    import defusedxml.ElementTree as DET
    XML_PARSER = DET
except ImportError as e:
    raise ImportError(
        "defusedxml is required to prevent XXE vulnerabilities. "
        "Install it with: pip install defusedxml"
    ) from e
```
**Acceptance Criteria:**
- `import sitemap_parser` raises `ImportError` with helpful message when defusedxml absent
- `defusedxml>=0.7.1` remains in `requirements.txt` (already present)
- Existing install with defusedxml works unchanged

**Status:** [x]

---

### T-05 · Remove hardcoded proxy IPs · B-03 · P1
**File:** `proxy_manager.py:53-72`  
**Action:** In `_create_sample_proxy_file()`, remove all actual `http://IP:port` lines. Replace with comment-only placeholders:
```python
"# Add your working proxies here (one per line):",
"# http://ip:port",
"# http://username:password@ip:port",
"# socks5://ip:port",
```
**Acceptance Criteria:**
- No IP address literals in the generated sample file
- File still creates successfully and loads without error
- `ProxyManager` starts with empty proxy list when file contains only comments

**Status:** [x]

---

### T-06 · Fix robots_txt default · B-04 · P1
**File:** `config.py:78`  
**Action:** Change `'respect_robots_txt': False,` → `'respect_robots_txt': True,`  
**Acceptance Criteria:**
- `DEFAULT_SETTINGS['respect_robots_txt']` is `True`
- Existing DB-stored settings (already-initialized installs) are unaffected (DB value takes precedence via `_load_config()`)

**Status:** [x]

---

### T-07 · Remove web/ directory creation · B-09 · P2
**File:** `config.py:14,17-19,117`  
**Action:**
1. Remove `WEB_DIR = os.path.join(BASE_DIR, 'web')` at line 14
2. Remove `os.makedirs(WEB_DIR, exist_ok=True)` at line 19 (module-level)
3. Remove `WEB_DIR` from the list inside `_ensure_directories()` at line 117

**Acceptance Criteria:**
- `web/` directory is not created on module import or `Config.__init__()`
- No `NameError` or `AttributeError` on `WEB_DIR` — confirm no other reference exists (grep: only these 3 occurrences found)

**Status:** [x]

---

## Wave 3 — Performance

### T-08 · Remove per-hash backup write · B-05 · P1
**File:** `db_manager.py:303-309`  
**Action:** Remove `self.update_backup()` call from `add_hash()`. Add doc comment explaining the change:
```python
def add_hash(self, hash_id: str, hash_type: str, timestamp: str):
    with sqlite3.connect(self.db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO file_hashes (hash_id, hash_type, timestamp) VALUES (?, ?, ?)",
            (hash_id, hash_type, timestamp)
        )
    # Backup is refreshed on save_settings()/save_websites(); not per-hash to avoid I/O on bulk scraping
```
**Acceptance Criteria:**
- `add_hash()` no longer calls `update_backup()`
- `has_hash()` still works (queries DB, not backup file)
- `update_backup()` still called by `save_settings()` and `save_websites()`

**Status:** [x]

---

## Wave 4 — Data Pipeline: Cycle Stats to DB

### T-09 · Add `save_cycle()` to DatabaseManager · B-06 · P1
**File:** `db_manager.py`  
**Action:** Add new method after `get_websites()` (line 342):
```python
def save_cycle(self, cycle_id: str, start_time: str, end_time: Optional[str],
               websites_processed: int, links_discovered: int,
               photos_downloaded: int, new_websites_added: int, status: str):
    with sqlite3.connect(self.db_path) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO cycles
            (cycle_id, start_time, end_time, websites_processed, links_discovered,
             photos_downloaded, new_websites_added, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (cycle_id, start_time, end_time, websites_processed,
              links_discovered, photos_downloaded, new_websites_added, status))
```
**Acceptance Criteria:**
- Method exists and passes a basic smoke test: call it with dummy values, query `cycles` table, confirm row present
- Existing DB schema unchanged (`CREATE TABLE IF NOT EXISTS` handles it)

**Status:** [x]

---

### T-10 · Wire CycleManager to persist cycles · B-06 · P1
**File:** `cycle_manager.py`  
**Action:** After `self.data_manager.save_cycle_stats(self.current_cycle)` at line 456, add:
```python
from db_manager import get_db_manager
get_db_manager().save_cycle(
    cycle_id=self.current_cycle.cycle_id,
    start_time=self.current_cycle.start_time,
    end_time=self.current_cycle.end_time,
    websites_processed=self.current_cycle.websites_crawled,
    links_discovered=self.current_cycle.links_discovered,
    photos_downloaded=self.current_cycle.photos_downloaded,
    new_websites_added=self.current_cycle.new_websites_added,
    status='completed' if self.current_cycle.total_errors == 0 else 'completed_with_errors'
)
```
Repeat with `status='interrupted'` in `except KeyboardInterrupt` handler (line 476) and `status='failed'` in `except Exception` handler (line 484).

Move the `from db_manager import get_db_manager` to top of file with other imports.

**Acceptance Criteria:**
- After `run_cycle()` completes, `cycles` DB table has one new row
- `get_system_metrics()['total_photos_downloaded']` reflects actual downloads
- `get_advanced_statistics()['recent_cycles']` returns the cycle

**Status:** [x]

---

## Wave 5 — Module Quality

### T-11 · Hard-fail MODULES_AVAILABLE · B-07 · P1
**File:** `cycle_manager.py`  
**Action:** At the start of `CycleManager.run_cycle()` (line 401), add guard:
```python
if not MODULES_AVAILABLE:
    raise RuntimeError(
        "CycleManager cannot run: required modules failed to import at startup. "
        "Check logs for the ImportError."
    )
```
**Acceptance Criteria:**
- `run_cycle()` raises `RuntimeError` with informative message when imports failed
- Import-time behavior unchanged (still uses try/except, does not crash on import)

**Status:** [x]

---

### T-12 · Use logger in Config._load_config() · B-08 · P1
**File:** `config.py`  
**Action:**
1. Add at top of file (after existing imports): `import logging` and `logger = logging.getLogger(__name__)`
2. Line 135: `print(f"WARNING: Error loading config from DB: {e}")` → `logger.error("Error loading config from DB: %s", e)`
3. Line 168: `print(f"ERROR: Error creating default config: {e}")` → `logger.error("Error creating default config: %s", e)`
4. Leave `print(f"SUCCESS: ...")` and `print(f"WARNING: Cannot add website...")` as-is (they are intentional user-facing console output)

**Acceptance Criteria:**
- No bare `print(f"WARNING: ... {e}")` or `print(f"ERROR: ... {e}")` remaining in exception handlers
- `logging.getLogger(__name__)` used for error/warning messages that go to log file

**Status:** [x]

---

### T-13 · Remove duplicate comment in sitemap_parser.py · B-19 · P3
**File:** `sitemap_parser.py:27`  
**Action:** Delete the duplicate `# Optional async dependencies` line (line 27 — the first occurrence at line 26 is the real one immediately before the try block).

Wait — re-reading the file: lines 26 and 28 are blank lines, line 27 is the duplicate comment before `# Optional async dependencies` at line 28. Actually from the read output: line 26 is blank, line 27 is `# Optional async dependencies`, line 28 is blank, line 29 is `# Optional async dependencies` (duplicate). Delete one.

**Acceptance Criteria:** Only one `# Optional async dependencies` comment in the file.

**Status:** [x]

---

### T-14 · Fix SitemapParser session always created · B-13 · P2
**File:** `sitemap_parser.py:52-55`  
**Action:** Replace:
```python
if ASYNC_AVAILABLE:
    self.session = create_session_with_retries()
else:
    self.session = None
```
With:
```python
self.session = create_session_with_retries()
```
**Acceptance Criteria:**
- `SitemapParser` creates a requests session regardless of aiohttp availability
- No `AttributeError` on `self.session` when aiohttp absent

**Status:** [x]

---

## Wave 6 — Dead Code Removal

### T-15 · Remove WEBSITES legacy alias · B-16 · P3
**File:** `config.py:466-467`  
**Action:** Delete:
```python
# Legacy compatibility - provide WEBSITES variable
WEBSITES = config.websites
```
**Pre-condition:** Confirm no other `.py` file imports `WEBSITES` from `config` — grep confirms none.  
**Acceptance Criteria:** Lines removed. No `ImportError` or `NameError` in any file.

**Status:** [x]

---

### T-16 · Update link_spider.py to use get_config() · B-17 · P3
**File:** `link_spider.py:114-138` and second call site (~line 199)  
**Action:** Replace `config = load_config()` / `config.get('websites', [])` / `config.get('settings', {})` with `cfg = get_config()` / `cfg.websites` / `cfg.settings`.

Verify the import at top of `link_spider.py` includes `get_config` (add if missing).

**Acceptance Criteria:**
- `link_spider.py` no longer calls `load_config()`
- `_load_website_config()` returns identical data as before for both str and dict entries
- Existing tests pass

**Status:** [x]

---

### T-17 · Remove load_config() free function · B-17 · P3
**Depends on:** T-16 complete  
**File:** `config.py:459-464`  
**Action:** Delete:
```python
def load_config():
    config._load_config()
    return {
        'websites': config.websites,
        'settings': config.settings
    }
```
**Acceptance Criteria:** Function removed. No `ImportError` or `NameError` in any file (grep to confirm no remaining callers after T-16).

**Status:** [x]

---

### T-18 · Move test_duplicate_detection to test suite · B-10 · P2
**Files:** `config.py:411-457`, `tests/test_config.py` (new)  
**Action:**
1. Create `tests/test_config.py`
2. Port `test_duplicate_detection()` logic as proper `pytest` test functions:
   - `test_url_equivalence()` — tests `_urls_are_equivalent()` with the 4 pairs
   - `test_add_website_duplicate_prevention()` — tests add/duplicate/cleanup flow
3. Remove `test_duplicate_detection()` from `config.py:411-457`
4. Remove `if __name__ == "__main__": test_duplicate_detection()` block

**Acceptance Criteria:**
- `pytest tests/test_config.py` passes
- `config.py` no longer contains `test_duplicate_detection()`
- No `if __name__ == "__main__":` block remains in `config.py`

**Status:** [x]

---

### T-19 · Fix duplicate lxml in requirements.txt · B-18 · P3
**File:** `requirements.txt:9`  
**Action:** Remove the second `lxml>=4.9.0` line (line 9). Keep line 5.  
**Acceptance Criteria:** `lxml>=4.9.0` appears exactly once in `requirements.txt`.

**Status:** [x]

---

## Wave 7 — Normalization Refactor [Medium effort — explicit approval required]

### T-20 · Normalize website entries to dict on add · B-11 · P2
**Files:** `config.py`, `db_manager.py`, `link_spider.py`  
**Action:**
1. In `Config.add_website()` simple mode (line 265-268): normalize URL string to full dict before appending
2. Remove all `isinstance(site, str)` branches from: `get_enabled_websites()`, `remove_website()`, `toggle_website()`, `get_website_config()`, `_is_duplicate_website()`, `db_manager.save_websites()`, `link_spider._load_website_config()`

**Risk:** Medium — 7+ branch points to update atomically.  
**Acceptance Criteria:**
- `isinstance(site, str)` no longer appears in the files above
- All existing tests pass
- `config.add_website('https://example.com', simple=True)` results in a dict entry

**Status:** [x]

---

## Wave 8 — DataManager Dedup [Deferred]

### T-21 · Remove duplicate DataManager from cycle_manager.py · B-12 · P2
**Depends on:** T-10 complete (cycles persisted to DB)  
**Files:** `cycle_manager.py`, `data_manager.py`  
**Action:** Replace `DataManager` usage in `CycleManager` with direct `db_manager` calls for cycle data. Keep chunk file logic (no DB equivalent). Remove `DataManager` class after migration.  
**Status:** [x]

---

## Wave 9 — Documentation [Explicit approval required]

### T-22 · Rewrite README.md · P3
**File:** `README.md`  
**Action:** Full rewrite from scratch using AUDIT.md §1 Master Feature Map as source of truth.  
Must cover: dual-config architecture, actual module list (remove `enhanced_spider.py`, `download_path_manager.py`), `setup.bat` / `start_toolkit.bat` usage, settings.json vs DB split, robots.txt default.  
**Status:** [x]

---

### T-23 · Add DB manager tests · P2
**File:** `tests/test_db_manager.py` (new)  
**Action:** Add tests for: `init_db()` schema, `add_hash()`/`has_hash()` round-trip, `save_websites()`/`get_websites()` round-trip, `save_cycle()` (new method from T-09), `sync_from_backup()`.  
**Status:** [x]

---

## Execution Summary

| Wave | Tasks | Estimated Risk | Prerequisite |
|------|-------|---------------|--------------|
| 1 (Cleanup) | T-01, T-02, T-03 | Low | None |
| 2 (Security) | T-04, T-05, T-06, T-07 | Low | Wave 1 |
| 3 (Perf) | T-08 | Low | None |
| 4 (Data) | T-09, T-10 | Low-Medium | None |
| 5 (Quality) | T-11, T-12, T-13, T-14 | Low | None |
| 6 (Dead code) | T-15, T-16, T-17, T-18, T-19 | Low | T-16 before T-17 |
| 7 (Refactor) | T-20 | Medium | Approval required |
| 8 (Dedup) | T-21 | Medium | T-10 + Approval |
| 9 (Docs) | T-22, T-23 | Low | Approval required |

**Waves 1–6: 19 tasks, all low-risk, ready for execution on approval.**
