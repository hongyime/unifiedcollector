# Design Document — Remediation Implementation
**Generated:** 2026-04-26  
**Source:** AUDIT.md + bugfix.md + direct codebase read

---

## 1. Architecture Overview

### Current Data Flow (Broken)
```
CycleManager.run_cycle()
  └─► DataManager.save_cycle_stats()  → data/cycles/*.json  ← file only, DB never touched
  
add_hash(hash_id)
  └─► DB INSERT
  └─► update_backup()  ← full JSON rewrite on EVERY hash (315× per session)
  
link_spider crawl output
  └─► data/link_spider/<domain>/  ← file only, DB links table always empty
```

### Target Data Flow (After Fixes)
```
CycleManager.run_cycle()
  └─► DataManager.save_cycle_stats()  → data/cycles/*.json  (kept as backup)
  └─► DatabaseManager.save_cycle()    → DB cycles table  ← NEW

add_hash(hash_id)
  └─► DB INSERT only
  └─► update_backup() removed  ← backup refreshed by save_settings()/save_websites() instead

link_spider  (Wave 8 — deferred, high complexity)
  └─► file output unchanged for now  ← document as known gap
```

---

## 2. Implementation Waves

### Wave 1 — Structural Cleanup (T-01 to T-03)
No code changes — file system operations only. Safe to execute first.

**Deletions:**
- `venv/` — stale duplicate venv (confirmed `.venv/` is active)
- `web/` — empty, no code uses it
- `data/chunks/` — 500+ stale cycle progress files
- `data/link_spider/` — crawler output dirs (not in DB, not referenced)
- `data/photo_scraping_logs/` — old run logs
- `data/exports/`, `data/reports/` — empty dirs
- `data/statistics.json`, `data/spider_progress.json`, `data/photo_scraper_state.json` — all-zeroes/null, stale
- `data/crawled_links.txt`, `data/extracted_links.txt` — header-only, stale
- Root `__pycache__/`

**Additions:**
- `.gitignore` — see Section 4

### Wave 2 — Security Fixes (T-04 to T-07)
Targeted 1-3 line changes per file.

- **defusedxml (B-02):** Change `sitemap_parser.py` try/except to `raise ImportError` if defusedxml missing. Already in `requirements.txt` — make it non-optional at runtime.
- **Proxy IPs (B-03):** Replace all real `http://IP:port` lines in `_create_sample_proxy_file()` with commented placeholder text.
- **robots_txt (B-04):** `config.py:78` change `False` → `True`.
- **web/ creation (B-09):** Remove `WEB_DIR` constant, `os.makedirs(WEB_DIR)` at module level, and reference in `_ensure_directories()`.

### Wave 3 — Performance (T-08)
- **Batch backup (B-05):** Remove `self.update_backup()` from `add_hash()`. The SQLite DB is the primary persistent store; backup is a secondary recovery mechanism. Backup is refreshed on `save_settings()` / `save_websites()` calls. Add doc comment explaining the intentional design.

### Wave 4 — Data Pipeline: Cycle Stats to DB (T-09, T-10)
New method + call site. Zero breaking changes.

**`DatabaseManager.save_cycle()` (new method):**
```python
def save_cycle(self, cycle_id, start_time, end_time, websites_processed,
               links_discovered, photos_downloaded, new_websites_added, status):
    with sqlite3.connect(self.db_path) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO cycles
            (cycle_id, start_time, end_time, websites_processed, links_discovered,
             photos_downloaded, new_websites_added, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (cycle_id, start_time, end_time, websites_processed,
              links_discovered, photos_downloaded, new_websites_added, status))
```

**`CycleManager.run_cycle()` call site** — after `self.data_manager.save_cycle_stats(...)` at line 456:
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
Same call added to the `except KeyboardInterrupt` and `except Exception` handlers with status `'interrupted'` / `'failed'`.

### Wave 5 — Module Quality (T-11 to T-14)
Small targeted fixes, no cascading changes.

- **MODULES_AVAILABLE (B-07):** Add explicit `RuntimeError` inside `CycleManager.run_cycle()` if `MODULES_AVAILABLE is False`. Keep the soft import guard at module level (don't break import-time behavior).
- **Logger in _load_config (B-08):** Add `import logging` + `logger = logging.getLogger(__name__)` at top of `config.py`. Change `print(f"WARNING: ...")` at line 135 to `logger.error(...)`. Change other `print(f"WARNING:")` / `print(f"ERROR:")` calls to logger calls.
- **Duplicate comment (B-19):** Remove line 27 of `sitemap_parser.py`.
- **Session guard (B-13):** Remove `if ASYNC_AVAILABLE:` guard in `SitemapParser.__init__()`; always create session.

### Wave 6 — Dead Code Removal (T-15 to T-18)
Requires verifying no callers before deleting.

- **WEBSITES alias (B-16):** Confirmed by grep: no external file imports `WEBSITES` from `config.py`. Safe to delete lines 466-467.
- **load_config() removal chain (B-17):**
  1. Update `link_spider.py:116,199`: replace `config = load_config()` + `config.get('websites')` with `cfg = get_config()` + `cfg.websites`
  2. Remove `load_config()` free function from `config.py:459-464`
- **test_duplicate_detection (B-10):** Create `tests/test_config.py`, move test body there as proper pytest functions. Remove from `config.py:411-457`.
- **Duplicate lxml (B-18):** Remove line 9 from `requirements.txt`.

### Wave 7 — Normalization Refactor (T-19) [Medium effort]
Normalize website entries to `dict` on `add_website()` call in simple mode (line 266) instead of appending raw string. This eliminates all `isinstance(site, str)` branches in consumers.

**Risk:** Any caller relying on str entries in `config.websites` will break. Mitigation: grep all callers first, update in one pass.

**Affected consumers to update:**
- `config.py:get_enabled_websites()` — remove str branch
- `config.py:remove_website()` — remove str branch  
- `config.py:toggle_website()` — remove str branch
- `config.py:get_website_config()` — remove str branch
- `config.py:_is_duplicate_website()` — remove str branch
- `db_manager.py:save_websites()` — remove str branch
- `link_spider.py:_load_website_config()` — remove str branch

### Wave 8 — DataManager Dedup (T-20) [Medium effort, deferred]
Once Wave 4 persists cycles to DB, `DataManager.get_recent_cycles()` in `cycle_manager.py` can be replaced with DB queries via `DatabaseManager.get_advanced_statistics()`. `DataManager.save_cycle_stats()` becomes a redundant JSON backup — keep it but remove the class responsibility to be the primary source.

Not a blocker. Defer to separate PR.

### Wave 9 — Documentation (T-21, T-22)
- Rewrite `README.md` from scratch using the Master Feature Map in AUDIT.md §1. Document dual-config split (settings.json = UI prefs; DB = scraping settings). Remove phantom module references.
- Expand test suite: `tests/test_db_manager.py`, `tests/test_config.py` (with moved duplicate-detection test).

---

## 3. Interface Contracts

### New: `DatabaseManager.save_cycle()`
```
Inputs:  cycle_id (str), start_time (ISO str), end_time (ISO str),
         websites_processed (int), links_discovered (int),
         photos_downloaded (int), new_websites_added (int),
         status ('completed'|'completed_with_errors'|'interrupted'|'failed')
Returns: None
Raises:  sqlite3.Error on DB failure (let caller handle)
Side effects: writes 1 row to cycles table via INSERT OR REPLACE
```

### Modified: `DatabaseManager.add_hash()`
```
Inputs:  hash_id (str), hash_type (str), timestamp (str)  — unchanged
Returns: None  — unchanged
Side effects: DB INSERT only; backup NOT updated  ← changed
Note: caller must call update_backup() explicitly if backup freshness needed
```

### Modified: `SitemapParser.__init__()`
```
self.session = create_session_with_retries()  — always created (no guard)
```

---

## 4. .gitignore Content Plan

```gitignore
# Virtual environments
.venv/
venv/

# Python bytecode
__pycache__/
*.pyc
*.pyo
*.pyd

# Test artifacts
.pytest_cache/
.coverage
htmlcov/

# User data — never commit
data/toolkit.db
data/file_hashes_backup.json
data/websites_config_backup.json
data/url_filter_config.json
data/proxies.txt
settings.json

# Runtime-generated data
data/chunks/
data/link_spider/
data/cycles/
data/logs/
data/photo_scraping_logs/
data/automation/

# User downloads
downloads/

# OS artifacts
.DS_Store
Thumbs.db

# IDE
.vscode/
.idea/
```

---

## 5. Dependency Compatibility

| Package | Current spec | Installed (from .venv) | Notes |
|---------|-------------|------------------------|-------|
| defusedxml | `>=0.7.1` | Present (soup sieve dist-info visible in .venv) | Make hard-required at runtime in sitemap_parser.py |
| lxml | `>=4.9.0` (×2) | Present | Remove duplicate line |
| aiohttp | `>=3.8.0` | Listed but optional at runtime | Keep soft — aiohttp enables async fetch but requests fallback works |

No version conflicts identified. No changes to pinned versions required.

---

## 6. Risk Register

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Deleting `data/link_spider/` removes useful crawl data | Medium | AUDIT confirms no DB linkage and no code reads it; verify manually before delete |
| Removing `update_backup()` from `add_hash()` means hash backup stale between scraping session and next settings save | Low | SQLite DB is crash-safe (WAL); backup is secondary; acceptable |
| `defusedxml` hard-fail breaks existing installs where defusedxml missing | Low | Already in requirements.txt; any `pip install -r requirements.txt` installs it |
| `respect_robots_txt = True` default changes behavior for existing users | Low | Setting stored in DB; existing users with DB already populated keep their saved value; only affects fresh installs |
| Wave 7 normalization refactor touches 7+ sites | Medium | Requires thorough grep before execution; treat as isolated PR |
