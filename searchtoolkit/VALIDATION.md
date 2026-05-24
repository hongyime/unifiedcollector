# Searchtoolkit — Validation Record
Last validated: 2026-05-13

## What the toolkit does
Multi-engine search + file download toolkit for Windows. Three modes:
1. **Search & Extract** — DDG/Bing/Serper waterfall → spider pages → download images/PDFs → convert to JPG
2. **Bing Image Downloader** — Bing image search with format/quality filters, per-keyword subfolders
3. **Dork Runner** — Run dorks across engines, export URL lists to .txt files

Supporting infrastructure: SQLite state persistence (resume), search result caching (TTL), adaptive per-domain rate limiting, optional Tor SOCKS5 proxy, graceful Ctrl+C shutdown.

Target use: Downloading Singapore secondary school yearbook photos from public sources.

## Entry points
- `start_toolkit.bat` (root) → `main.py` — primary Windows launcher
- `scripts/quick_actions.bat` → interactive menu → `main.py`
- `scripts/start_toolkit.bat` → direct CLI with args → `main.py`
- `scripts/setup.bat` → creates venv, installs deps

## Bugs fixed (2026-05-13)

| # | File | Bug | Fix |
|---|------|-----|-----|
| 1 | `main.py` | UnicodeEncodeError on Windows CP1252 — emoji in print() crashes at startup | `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` before any import |
| 2 | `scripts/quick_actions.bat` | `python -m searchtoolkit.app` — nonexistent module (should be `src.app`) | Changed all invocations to `.venv\Scripts\python.exe main.py` |
| 3 | `scripts/start_toolkit.bat` | Same wrong module path | Changed to `"%PYTHON_EXE%" main.py` |
| 4 | `src/download_path_manager.py` | Module-level `signal.signal(SIGINT)` overwrote `main.py`'s graceful-shutdown handler | Removed signal handlers; kept `atexit.register` only |
| 5 | `src/resilience.py` | Socket leak in `_is_internet_available` — socket never closed on success | Wrapped in `with socket.socket(...) as s:` |
| 6 | `src/rate_limiter.py` | `AdaptiveRateLimiter` mutated global `base_delay` — one domain's 429 raised delays for all domains | Per-domain `_domain_effective_delay` dict; `base_delay` is now a read-only floor |
| 7 | `src/tor_manager.py` | `rotate_circuit()` sent `CTRL_BREAK_EVENT` on Windows — terminates Tor process instead of rotating | Skip rotation on Windows with logged warning; SIGHUP on Linux/Mac only |
| 8 | `scripts/setup.bat` | Pillow import check used `import Pillow` (always fails — correct is `from PIL import Image`) | Fixed to `from PIL import Image` |
| 9 | `src/search_cache.py` | `set()` re-raised `IOError` making cache write failures fatal | Caught and logged as warning; cache errors are now non-fatal |
| 10 | `src/app.py`, `README.md`, `API_SETUP_GUIDE.md` | All examples referenced `python -m searchtoolkit.app` (nonexistent) | Updated to `python main.py` |
| 11 | `state/state.db.backup` | Stale backup from March 2026 | Deleted |

## Verified working
- Startup + menu render (no Unicode crash)
- `--help`, `--stats`, `--reset-state` CLI flags
- StateManager CRUD (SQLite + JSON backup)
- SearchCache get/set/expire
- AdaptiveRateLimiter per-domain isolation
- Internet availability check (no socket leak)
- Signal handler order preserved (main.py wins)
- All module imports clean

## Remaining risks / future work

| Risk | Severity | Notes |
|------|----------|-------|
| Tor circuit rotation on Windows requires `stem` + control port | Medium | Currently skips with warning; add `stem` for full support |
| `AdaptiveRateLimiter.wait()` holds `_lock` during sleep | Medium | Lock is held while sleeping if another thread needs it; refactor to release before sleep |
| `state_manager.py` opens a new DB connection per operation | Low | Fine at current load; could use connection pool if high-frequency use |
| No tests exist | Medium | All validation is manual; add pytest suite if toolkit grows |
| `data/search.txt` and `DEFAULT_DORKS` in `app.py` contain identical Singapore school dorks | Low | Duplication — pick one source of truth |
