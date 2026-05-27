# Local Activity Archive and Playback Toolkit

## Overview

This repository contains a local-first toolkit that archives activity data from a fitness platform, stores normalized route streams in SQLite, exposes a local FastAPI read API, and serves a React-based playback viewer.

Current capabilities:
- synchronize daily activity data for a target date,
- backfill historical activities by athlete-month cursor,
- persist athletes, activities, streams, run state, and media references,
- download tracked profile images and discovered activity media,
- run a local viewer for date playback and athlete route review.

---

## Repository Structure

- `app/` — FastAPI application and runner control endpoints
- `ingestion/` — session handling, scraping, ingestion, transforms, DB logic, media download, venv tooling
- `frontend/` — React + Vite viewer
- `tests/` — pytest test suite
- `data/` — local database and app-managed env file
- `downloads/` — downloaded media output
- `logs/` — sync, backfill, and smoke logs
- `toolkit.bat` — Windows operator entrypoint

---

## Prerequisites

### Required Tools
- Python on `PATH`
- Node.js on `PATH`
- npm on `PATH`

### Python Packages
Installed from `requirements.txt`:
- `fastapi==0.115.5`
- `uvicorn[standard]==0.30.6`
- `pydantic==2.7.4`
- `python-dotenv==1.0.1`
- `requests>=2.33.0`
- `playwright==1.50.0`
- `pytest==8.3.2`
- `tzdata>=2024.1`
- `httpx>=0.27.0`

### Frontend Packages
Installed from `frontend/package.json`:
- React 18
- Vite 6
- MapLibre GL 5

### Additional Runtime Dependency
Install the Playwright browser runtime:
```bash
python -m playwright install chromium
```

### Version Note
The repository does not pin an exact Python, Node.js, or npm version. Use a current Python 3 runtime and a current Node.js/npm runtime compatible with Vite 6 and Playwright 1.50.

On Windows batch entry points, the toolkit resolves Python in this order:
1. `py -3`
2. `python`

If `.venv` exists but its interpreter is no longer runnable, setup automatically recreates `.venv`.

---

## Environment Configuration

### Runtime-Read Environment Variables

| Variable | Type | Default | Description |
|---|---|---|---|
| `DB_PATH` | path string | `data/strava_sync.db` | SQLite database path |
| `ENV_PATH` | path string | `data/.env` | App-managed session store file |
| `BACKFILL_STEPS` | integer | `25` | Default backfill step budget |
| `BACKFILL_BUDGET_MINUTES` | integer | `25` | Deprecated alias retained for compatibility |
| `BACKFILL_PARALLELISM` | integer | `3` | Maximum concurrent athlete backfill workers |
| `BACKFILL_YEAR_CAP` | integer | `25` | Historical year stop boundary |
| `DEBUG_HTTP` | boolean | `false` | Enables verbose auth/request diagnostics |
| `DEBUG_DELAYS` | boolean | `false` | Logs random delays and backoff values |
| `RATE_LIMIT_RETRIES` | integer | `2` | Retry count for upstream HTTP 429 responses |
| `RATE_LIMIT_BACKOFF_SECONDS` | integer | `60` | Base delay for rate-limit backoff |
| `REQUEST_TIMEOUT_SECONDS` | integer | `20` | HTTP request timeout |
| `AUTH_RECOVERY_BACKOFF_SECONDS` | integer | `30` | Base cooldown before auth recovery attempts |
| `AUTH_RECOVERY_BACKOFF_CAP_SECONDS` | integer | `300` | Max cooldown for auth recovery attempts |
| `API_DELAY_MIN_SECONDS` | float | `5.0` | Min delay for general API requests |
| `API_DELAY_MAX_SECONDS` | float | `10.0` | Max delay for general API requests |
| `FEED_DELAY_MIN_SECONDS` | float | `5.0` | Min delay for feed fetches |
| `FEED_DELAY_MAX_SECONDS` | float | `12.0` | Max delay for feed fetches |
| `BACKFILL_DELAY_MIN_SECONDS` | float | `5.0` | Min delay for historical scraping |
| `BACKFILL_DELAY_MAX_SECONDS` | float | `15.0` | Max delay for historical scraping |
| `STREAM_DELAY_MIN_SECONDS` | float | `5.0` | Min delay for activity stream fetches |
| `STREAM_DELAY_MAX_SECONDS` | float | `8.0` | Max delay for activity stream fetches |
| `ROSTER_DELAY_MIN_SECONDS` | float | `5.0` | Min delay for follow-roster requests |
| `ROSTER_DELAY_MAX_SECONDS` | float | `10.0` | Max delay for follow-roster requests |
| `DOWNLOADS_DIR` | path string | `downloads/` | Default output directory for media downloads |
| `STRAVA_USER_AGENT` | string | built-in desktop browser UA | Override for upstream request user-agent |

### App-Managed Session File

The application writes sensitive session data to `data/.env` by default. Do not commit real session values.

Expected keys written by the application:
- `STRAVA_SESSION_COOKIE`
- `CAPTURED_AT`

Example template only:
```env
STRAVA_SESSION_COOKIE=<session-cookie>
CAPTURED_AT=2026-01-01T00:00:00+00:00
```

### Authentication Inputs

The tooling supports these auth sources:
- explicit `--cookie-value`
- `--cookies-file <path>`
- persisted session cookie in `ENV_PATH`
- interactive Playwright fallback when enabled by CLI options

Recommended local file:
- `cookies.txt` in repository root, Netscape cookie format

---

## Installation and Setup

### Option A: Windows Guided Setup
Run:
```bat
toolkit.bat setup
```

This does the following:
1. creates `.venv/` if missing,
2. validates `.venv` and auto-recreates it if broken,
3. activates the virtual environment,
4. upgrades `pip`,
5. installs Python dependencies,
6. installs Playwright Chromium,
7. installs frontend dependencies.

### Option B: Manual Setup

#### 1. Create and activate a virtual environment
Windows:
```bat
python -m venv .venv
.venv\Scripts\activate
```

#### 2. Install Python dependencies
```bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

#### 3. Install Playwright browser runtime
```bat
python -m playwright install chromium
```

#### 4. Install frontend dependencies
```bat
npm --prefix frontend install
```

#### 5. Build the frontend
```bat
npm --prefix frontend run build
```

#### 6. Provide session credentials
Use one of:
- place a valid Netscape-format `cookies.txt` in the repository root,
- supply `--cookie-value` at runtime,
- or allow Playwright-based interactive session capture.

---

## Local Development Workflows

### Run the API + Viewer
After building the frontend:
```bat
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open:
- `http://127.0.0.1:8000`

If `frontend/dist` is missing, the root route returns a backend status payload instead of the SPA.

### Rebuild the Frontend
```bat
npm --prefix frontend run build
```

### Guided Viewer Start on Windows
```bat
toolkit.bat backend
```

---

## Usage

### Ingestion CLI

#### Sync one date
```bat
python -m ingestion.main --date 2026-04-12 --auth-mode cookiestxt --cookies-file cookies.txt
```

#### Sync one date and refresh followed roster
```bat
python -m ingestion.main --date 2026-04-12 --refresh-following-roster --auth-mode cookiestxt --cookies-file cookies.txt
```

#### Run backfill only
```bat
python -m ingestion.main --backfill-only --backfill-steps 10 --auth-mode cookiestxt --cookies-file cookies.txt
```

#### Run sync only
```bat
python -m ingestion.main --date 2026-04-12 --sync-only --auth-mode cookiestxt --cookies-file cookies.txt
```

#### Override backfill concurrency and year cap
```bat
python -m ingestion.main --backfill-only --backfill-steps 20 --backfill-parallelism 2 --backfill-year-cap 10 --auth-mode cookiestxt --cookies-file cookies.txt
```

#### Inspect venv health
```bat
python -m ingestion.main --check-venv
```

#### Auto-heal venv issues
```bat
python -m ingestion.main --heal-venv
```

### Advanced CLI Options

#### Database Integrity Check
Validates foreign key constraints, orphaned records, and NULL violations:
```bat
python -m ingestion.main --check-db-integrity
```

#### Rescrape Activities
Resets stream_status to pending for re-scraping activities:
```bat
# Rescrape all activities
python -m ingestion.main --rescrape-activities

# Rescrape activities for specific athlete
python -m ingestion.main --rescrape-activities --athlete-id 12345
```

#### Virtual Environment Management
Check and repair virtual environment issues:
```bat
# Check venv health
python -m ingestion.main --check-venv

# Auto-heal venv issues
python -m ingestion.main --heal-venv

# Control upgrade behavior
python -m ingestion.main --heal-venv --upgrade-mode prompt
python -m ingestion.main --heal-venv --upgrade-mode never

# Force venv recreation
python -m ingestion.main --heal-venv --force-reinstall

# Skip venv check during normal runs
python -m ingestion.main --date 2026-04-12 --skip-venv-check
```

### Media Download CLI

#### Download tracked profile photos
```bat
python -m ingestion.photo_downloader --mode profiles --output-dir downloads --auth-mode cookiestxt --cookies-file cookies.txt
```

#### Download discovered activity media
```bat
python -m ingestion.photo_downloader --mode activities --output-dir downloads --auth-mode cookiestxt --cookies-file cookies.txt
```

#### Download activity media for one date
```bat
python -m ingestion.photo_downloader --mode activities --date 2026-04-12 --output-dir downloads --auth-mode cookiestxt --cookies-file cookies.txt
```

#### Download both profile and activity media
```bat
python -m ingestion.photo_downloader --mode all --output-dir downloads --auth-mode cookiestxt --cookies-file cookies.txt
```

### Windows Batch Shortcuts

#### Launch menu
```bat
toolkit.bat
```

#### Direct commands
```bat
toolkit.bat sync-today
toolkit.bat sync-date 2026-04-12
toolkit.bat watch 15 2 1 25
toolkit.bat backfill 1 25
toolkit.bat photos-profiles
toolkit.bat photos-activities
toolkit.bat photos-date 2026-04-12
toolkit.bat photos-all
toolkit.bat backend
toolkit.bat backend-stop
toolkit.bat backend-status
toolkit.bat build
toolkit.bat status
```

---

## API Usage Examples

### Status
```bash
curl http://127.0.0.1:8000/api/v1/status
```

### Dates
```bash
curl http://127.0.0.1:8000/api/v1/dates
```

### Day playback
```bash
curl "http://127.0.0.1:8000/api/v1/activities?date=2026-04-12"
```

### Athlete roster for a date
```bash
curl "http://127.0.0.1:8000/api/v1/athletes?date=2026-04-12"
```

### Athlete roster for a month
```bash
curl "http://127.0.0.1:8000/api/v1/athletes?month=2026-04"
```

### Athlete detail
```bash
curl http://127.0.0.1:8000/api/v1/athletes/14
```

### Athlete route history
```bash
curl http://127.0.0.1:8000/api/v1/athletes/14/routes
```

### Backfill coverage
```bash
curl http://127.0.0.1:8000/api/v1/backfill/coverage
```

### Start sync subprocess
```bash
curl -X POST "http://127.0.0.1:8000/api/v1/sync/run?date=2026-04-12&refresh_following_roster=true"
```

### Stop sync subprocess
```bash
curl -X POST http://127.0.0.1:8000/api/v1/sync/stop
```

### Start backfill subprocess
```bash
curl -X POST "http://127.0.0.1:8000/api/v1/backfill/run?steps=10"
```

### Stop backfill subprocess
```bash
curl -X POST http://127.0.0.1:8000/api/v1/backfill/stop
```

---

## Testing

Run the full suite:
```bat
python -m pytest
```

Run targeted suites:
```bat
python -m pytest tests/test_db.py
python -m pytest tests/test_session.py
python -m pytest tests/test_crawler.py
python -m pytest tests/test_status_api.py
python -m pytest tests/test_delay_utils.py
python -m pytest tests/test_parsers.py
```

Pytest configuration is in `pytest.ini`.

---

## Operational Notes

- The default database file is `data/strava_sync.db`.
- The application creates runtime directories if missing.
- API DB access is read-only; ingestion uses read-write connections.
- Sync and backfill subprocesses write logs to `logs/`.
- The root API serves static frontend assets only after a Vite build.
- Session cookies are stored locally in plaintext; protect local files accordingly.
- This repository is designed for localhost operation and is not production-hardened for public network exposure.

---

## Troubleshooting

### Frontend does not load
Build the frontend first:
```bat
npm --prefix frontend run build
```

### Session validation fails
Provide a valid session source:
- `--cookie-value`,
- `--cookies-file cookies.txt`,
- or interactive Playwright recovery.

### Rate limiting occurs
Tune delay- and retry-related env vars:
- `RATE_LIMIT_RETRIES`
- `RATE_LIMIT_BACKOFF_SECONDS`
- `API_DELAY_MIN_SECONDS`
- `API_DELAY_MAX_SECONDS`
- `FEED_DELAY_MIN_SECONDS`
- `FEED_DELAY_MAX_SECONDS`
- `BACKFILL_DELAY_MIN_SECONDS`
- `BACKFILL_DELAY_MAX_SECONDS`
- `STREAM_DELAY_MIN_SECONDS`
- `STREAM_DELAY_MAX_SECONDS`
- `ROSTER_DELAY_MIN_SECONDS`
- `ROSTER_DELAY_MAX_SECONDS`

### Environment drift warning appears
The application can emit a one-time warning about incompatible global `requests` dependencies. The repository already prefers virtual-environment usage; use one consistently.

### Setup shows `No Python at ...Python312\\python.exe`
This usually means a stale or broken virtual environment launcher in `.venv\Scripts\python.exe` that still references an old base Python install.

Current toolkit behavior on Windows setup:
- verifies `.venv` interpreter health,
- auto-recreates `.venv` when broken,
- uses `py -3` (or `python`) to rebuild.

If needed, remove `.venv` manually and run setup again:
```bat
toolkit.bat setup
```

### Stop long-running jobs safely
- CLI ingestion and media download: `Ctrl+C`
- API runner tasks: call the corresponding `/stop` endpoint
- Windows viewer helper: `toolkit.bat backend-stop`
