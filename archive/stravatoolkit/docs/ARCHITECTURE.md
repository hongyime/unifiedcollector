# Architecture Documentation — Strava Toolkit
> Last Updated: 2026-04-26

---

## Overview

The Strava Toolkit is a local-first system for archiving and visualizing activity data from Strava. It consists of three main components:

1. **Ingestion System** — Python-based scraper and data pipeline
2. **API Server** — FastAPI backend serving normalized data
3. **Viewer Frontend** — React SPA for playback and visualization

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         User Interface                          │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  React SPA (frontend/)                                    │  │
│  │  - MapLibre GL for route visualization                    │  │
│  │  - Date-based playback controls                           │  │
│  │  - Athlete roster and detail views                        │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              ▲
                              │ HTTP/JSON
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      FastAPI Server (app/)                      │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  REST API Endpoints                                       │  │
│  │  - /api/v1/activities (playback data)                     │  │
│  │  - /api/v1/athletes (roster, detail, routes)             │  │
│  │  - /api/v1/sync, /api/v1/backfill (subprocess control)   │  │
│  │  - /api/v1/status, /api/v1/dates, /api/v1/coverage       │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Subprocess Runners (app/processes/)                      │  │
│  │  - SyncRunner: daily feed sync subprocess                │  │
│  │  - BackfillRunner: historical backfill subprocess        │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              ▲
                              │ SQLite (read-only)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                   SQLite Database (data/)                       │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  8 Tables (WAL mode)                                      │  │
│  │  - athletes, activities, streams, activity_photos         │  │
│  │  - athlete_photo_history, following_roster_snapshots      │  │
│  │  - crawl_runs, session_state                             │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              ▲
                              │ SQLite (read-write)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                  Ingestion System (ingestion/)                  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Crawler (ingestion/crawler.py)                           │  │
│  │  - Orchestrates daily sync + historical backfill         │  │
│  │  - Manages shutdown coordination                         │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Scrapers (ingestion/)                                    │  │
│  │  - FollowingFeedScraper: daily activity feed             │  │
│  │  - FollowRosterScraper: followed athletes list           │  │
│  │  - HistoricalActivityScraper: athlete backfill           │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Session Management (ingestion/session.py)               │  │
│  │  - Cookie-based authentication                           │  │
│  │  - Automatic reauthentication via Playwright             │  │
│  │  - Rate limit handling with exponential backoff          │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Database Layer (ingestion/db.py)                        │  │
│  │  - Schema management and migrations                      │  │
│  │  - Athlete, activity, stream persistence                 │  │
│  │  - Playback query builders                               │  │
│  │  - Backfill state management                             │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              ▲
                              │ HTTPS
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Strava API/Website                         │
│  - Following feed (JSON API)                                   │
│  - Athlete profiles (HTML scraping)                            │
│  - Activity streams (JSON API)                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## Module Map

### `app/` — FastAPI Application
- `main.py` — FastAPI app initialization, middleware, static file serving
- `database.py` — DB connection dependency with thread-safe initialization
- `models.py` — Pydantic response models for API endpoints
- `routers/` — API endpoint handlers
  - `activities.py` — Day playback endpoint
  - `athletes.py` — Athlete roster, detail, routes
  - `backfill.py` — Backfill subprocess control
  - `coverage.py` — Backfill coverage reporting
  - `dates.py` — Available dates listing
  - `status.py` — System status endpoint
  - `sync.py` — Daily sync subprocess control
- `processes/` — Subprocess management
  - `sync_runner.py` — SyncRunner class for daily sync
  - `backfill_runner.py` — BackfillRunner class for historical backfill

### `ingestion/` — Data Ingestion System
- `main.py` — CLI entrypoint, argument parsing, orchestration
- `config.py` — Settings dataclass, environment variable loading
- `crawler.py` — Main crawler orchestration (sync + backfill)
- `session.py` — StravaSession class, authentication, rate limiting
- `db.py` — Database schema, queries, persistence (1300 lines)
- `scraper.py` — FollowingFeedScraper (daily feed)
- `athlete_scraper.py` — FollowRosterScraper, HistoricalActivityScraper
- `parsers.py` — HTML/JSON parsing utilities
- `transform.py` — Stream data transformation and normalization
- `delay_utils.py` — Random delays, exponential backoff
- `photo_downloader.py` — Media download CLI
- `venv_health.py` — Virtual environment health checks
- `venv_healer.py` — Automatic venv repair
- `backfill_health.py` — Backfill coverage diagnostics
- `status_report.py` — Status summary generation
- `runtime_env.py` — Runtime environment validation
- `auth_bootstrap.py` — Interactive auth setup

### `frontend/` — React Viewer
- `src/App.jsx` — Main application component
- `src/components/` — UI components
  - `AthleteRoster.jsx` — Athlete list with filtering
  - `AthleteDetailPanel.jsx` — Athlete detail sidebar
  - `MapCanvas.jsx` — MapLibre GL route visualization
  - `PlaybackControls.jsx` — Date picker and playback controls
  - `CoveragePanel.jsx` — Backfill coverage visualization
- `src/hooks/` — React hooks for data fetching
- `src/lib/` — Utilities (API client, color generation)

### `tests/` — Test Suite
- `test_db.py` — Database operations
- `test_session.py` — Authentication and session management
- `test_crawler.py` — Crawler orchestration
- `test_parsers.py` — HTML/JSON parsing
- `test_delay_utils.py` — Delay and backoff logic
- `test_db_integrity.py` — Database integrity checks
- `test_db_rescrape.py` — Activity rescraping
- `test_*_shutdown.py` — Graceful shutdown behavior

### `docs/` — Documentation
- `PRD.md` — Product requirements document
- `CLI_TEST_MATRIX.md` — CLI testing matrix
- `DYNAMIC_DELAY_CONFIGURATION.md` — Delay configuration guide
- `ARCHITECTURE.md` — This file

---

## Data Flow

### Daily Sync Flow
```
1. User triggers sync (CLI or API)
   ↓
2. Crawler validates session
   ↓
3. FollowingFeedScraper fetches today's feed
   ↓
4. For each activity:
   - Save activity metadata
   - Fetch and transform streams (lat/lng, time, etc.)
   - Save streams to DB
   ↓
5. Mark sync complete, save state
```

### Historical Backfill Flow
```
1. User triggers backfill (CLI or API)
   ↓
2. Crawler loads backfill state (athlete cursors)
   ↓
3. For each athlete (parallel workers):
   - HistoricalActivityScraper fetches month page
   - Parse activities from HTML
   - For each new activity:
     - Save activity metadata
     - Fetch and transform streams
     - Save streams to DB
   - Advance month cursor
   - Check step budget
   ↓
4. Save backfill state, mark run complete
```

### API Query Flow
```
1. Frontend requests /api/v1/activities?date=2026-04-26
   ↓
2. FastAPI router validates date format
   ↓
3. get_db() dependency provides read-only connection
   ↓
4. build_day_playback() queries:
   - Activities for date
   - Streams for each activity
   - Athlete metadata
   ↓
5. Transform to playback format (GeoJSON)
   ↓
6. Return JSON response
   ↓
7. Frontend renders routes on map
```

---

## Database Schema

### Core Tables

**athletes** (8 columns)
- Primary key: `athlete_id`
- Tracks followed athletes and backfill state
- Fields: name, avatar_url, is_following, is_tracked, backfill_status, backfill_completed_at, backfill_oldest_seen_utc, backfill_last_issue_*

**activities** (15 columns)
- Primary key: `activity_id`
- Foreign key: `athlete_id` → athletes
- Activity metadata: name, type, distance, elapsed_time, start_date_*, calendar_date
- Stream status: `stream_status` (pending/fetched/unavailable/error)

**streams** (7 columns)
- Primary key: `(activity_id, stream_type, idx)`
- Foreign key: `activity_id` → activities
- Stream data: stream_type (latlng/time/altitude/etc.), idx, value, raw_value

**activity_photos** (9 columns)
- Primary key: `photo_id`
- Foreign keys: `activity_id` → activities, `athlete_id` → athletes
- Photo metadata: source_url_*, media_type, local_path, md5_hash

**athlete_photo_history** (8 columns)
- Primary key: `id`
- Foreign key: `athlete_id` → athletes
- Profile photo history: source_url, local_path, md5_hash, captured_at, last_seen_at

**following_roster_snapshots** (4 columns)
- Primary key: `(athlete_id, snapshot_date)`
- Foreign key: `athlete_id` → athletes
- Historical following status tracking

**crawl_runs** (8 columns)
- Primary key: `run_id`
- Crawl execution history: run_type, target_date, started_at, completed_at, status, summary

**session_state** (4 columns)
- Primary key: `id` (singleton table)
- Session persistence: cookie_value, auth_mode, last_updated_at

### Relationships
```
athletes (1) ──< (N) activities
activities (1) ──< (N) streams
activities (1) ──< (N) activity_photos
athletes (1) ──< (N) athlete_photo_history
athletes (1) ──< (N) following_roster_snapshots
```

---

## Authentication Flow

### Cookie Sources (Priority Order)
1. **Explicit `--cookie-value`** — Direct CLI argument
2. **cookies.txt** — Netscape format cookie file
3. **Toolkit session store** — `data/.env` file
4. **Playwright fallback** — Interactive browser login

### Reauthentication Sequence
```
1. Request fails with 401/403/redirect to /login
   ↓
2. Pause for auth recovery cooldown (exponential backoff)
   ↓
3. Try recovery sources in order:
   - Primary source (cookiestxt or env)
   - Fallback source (if configured)
   - Playwright interactive login (if auto fallback enabled)
   ↓
4. For each source:
   - Load candidate cookie
   - Validate with /frontend/athletes/current
   - If valid: persist and continue
   - If invalid: try next source
   ↓
5. If all sources fail: raise SessionError(error_type="auth_failed")
```

### Rate Limit Handling
- HTTP 429 triggers exponential backoff
- Default: 2 retries with 60s base delay
- Backoff formula: `base_delay * (backoff_factor ** attempt) + jitter`
- Max delay capped at 180s per retry
- After exhausting retries: raise SessionError(error_type="rate_limited")

---

## Subprocess Model

### SyncRunner Lifecycle
```
1. API receives POST /api/v1/sync/run
   ↓
2. SyncRunner.start() called
   - Generate log file path
   - Build command: python -m ingestion.main --date X --sync-only
   - Open log file handle
   - Spawn subprocess with platform-specific isolation
   - Store process reference
   ↓
3. Subprocess runs independently
   - Logs to file
   - Saves progress to DB
   ↓
4. API receives POST /api/v1/sync/stop
   ↓
5. SyncRunner.stop() called
   - Send CTRL_BREAK_EVENT (Windows) or SIGTERM (POSIX)
   - Wait up to 15s for graceful shutdown
   - Force terminate if still running
   - Close log file handle
```

### BackfillRunner Lifecycle
- Same as SyncRunner but with `--backfill-only` flag
- Supports `--backfill-steps` parameter for budget control

### Platform-Specific Isolation
- **Windows:** `creationflags=CREATE_NEW_PROCESS_GROUP`
- **POSIX/Linux:** `start_new_session=True`
- Ensures subprocess survives parent Ctrl+C

---

## Key Design Decisions

### 1. WAL Mode for SQLite
**Decision:** Use WAL (Write-Ahead Logging) mode instead of default rollback journal.

**Rationale:**
- Concurrent readers don't block writers
- Better performance for write-heavy workloads
- Atomic commits without blocking reads

**Trade-offs:**
- Requires periodic checkpointing (handled automatically)
- WAL file can grow large (mitigated by regular checkpoints)

### 2. Autocommit Mode
**Decision:** Use autocommit mode (isolation_level=None) for all connections.

**Rationale:**
- Explicit transaction control where needed
- Avoids long-running transactions blocking other operations
- Simpler error handling (no need to rollback on every exception)

**Trade-offs:**
- Must manually wrap multi-statement operations in transactions
- No automatic rollback on errors

### 3. Singapore Timezone Hardcode
**Decision:** Hardcode `Asia/Singapore` (UTC+8) as the activity timezone.

**Rationale:**
- Strava uses Singapore time for activity timestamps
- Consistent with Strava's internal representation
- Simplifies date boundary calculations

**Trade-offs:**
- Not configurable per-user
- Assumes all activities use Strava's timezone

### 4. Delay Ranges
**Decision:** Use random delays between requests with configurable min/max ranges.

**Rationale:**
- Avoids rate limiting by spreading requests over time
- Randomization prevents detection as automated scraping
- Different ranges for different operation types (feed, backfill, streams, roster)

**Default Ranges:**
- API requests: 5-10s
- Feed fetches: 5-12s
- Backfill: 5-15s
- Streams: 5-8s
- Roster: 5-10s

**Trade-offs:**
- Slower data collection
- Necessary to avoid HTTP 429 responses

### 5. Subprocess Model for Long-Running Operations
**Decision:** Run sync and backfill as separate subprocesses controlled by API.

**Rationale:**
- Prevents API server blocking during long operations
- Allows monitoring via log files
- Enables graceful shutdown without killing API server
- Isolates ingestion errors from API server

**Trade-offs:**
- More complex process management
- Requires platform-specific signal handling
- Log file I/O overhead

### 6. Read-Only API Connections
**Decision:** API server uses read-only SQLite connections.

**Rationale:**
- Prevents accidental data modification via API
- Allows multiple concurrent API readers
- Clear separation: ingestion writes, API reads

**Trade-offs:**
- Cannot update data via API (by design)
- Requires separate ingestion process for writes

### 7. Monolithic db.py (1300 lines)
**Decision:** Keep all database logic in single file (for now).

**Rationale:**
- Simpler imports during initial development
- All queries visible in one place
- Easier to understand data flow

**Trade-offs:**
- Large file difficult to navigate
- Mixing concerns (schema, queries, transforms)
- **Note:** Planned for refactoring in T3-2

### 8. Cookie-Based Authentication
**Decision:** Use session cookies instead of OAuth tokens.

**Rationale:**
- Strava's public API has strict rate limits
- Web scraping provides more data (following feed, profile pages)
- Cookies easier to extract and persist

**Trade-offs:**
- Requires browser automation for initial auth
- Cookies expire and need refresh
- More fragile than official API

---

## Performance Characteristics

### Ingestion Performance
- **Daily sync:** ~30-60 seconds for 50 activities
- **Backfill:** ~5-10 activities per minute (rate-limited)
- **Stream fetch:** ~5-8 seconds per activity
- **Database writes:** ~1000 stream points per second

### API Performance
- **Day playback:** <100ms for 50 activities with streams
- **Athlete list:** <50ms for 100 athletes
- **Athlete detail:** <100ms with full route history
- **Database reads:** Read-only, no locking, fast

### Storage
- **Database size:** ~1MB per 100 activities with streams
- **WAL overhead:** ~10-20% of main DB size (periodic checkpoint)
- **Photos:** Variable (not stored in DB, only references)

---

## Error Handling Strategy

### Session Errors
- **Type:** `SessionError` with `error_type` attribute
- **Types:** `"rate_limited"`, `"auth_failed"`, `"network"`, `"unknown"`
- **Recovery:** Automatic reauthentication for auth failures
- **Backoff:** Exponential backoff for rate limits

### Database Errors
- **Strategy:** Let SQLite errors propagate
- **Integrity:** Foreign key constraints enforced
- **Transactions:** Explicit where needed, autocommit otherwise

### Scraping Errors
- **Strategy:** Mark activity as degraded, continue with others
- **Logging:** Print to stdout/stderr (will be replaced with logging module)
- **State:** Save progress before each athlete/activity

### API Errors
- **Validation:** FastAPI automatic validation (422 responses)
- **Not Found:** Explicit 404 responses
- **Server Errors:** Generic 500 response (no stack trace leakage)

---

## Testing Strategy

### Unit Tests
- Parsers (HTML/JSON extraction)
- Transforms (stream normalization)
- Delay utilities (backoff calculations)
- Configuration loading

### Integration Tests
- Database operations (CRUD, queries)
- Session management (auth, reauthentication)
- Crawler orchestration (sync, backfill)

### End-to-End Tests
- Full workflow simulation
- Shutdown coordination
- Subprocess management

### Test Fixtures
- Mock Strava responses (HTML, JSON)
- In-memory SQLite databases
- Fake session cookies

---

## Deployment Model

### Local Development
```bash
# Setup
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements.txt
playwright install chromium
npm --prefix frontend install
npm --prefix frontend run build

# Run API server
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# Run ingestion
python -m ingestion.main --date 2026-04-26 --auth-mode cookiestxt
```

### Production Considerations
- **Not production-ready** for public internet exposure
- Designed for localhost/LAN use only
- No authentication on API endpoints
- No HTTPS (assumes local network)
- No rate limiting on API
- Session cookies stored in plaintext

### Recommended Deployment
- Run on local machine or trusted LAN
- Use reverse proxy (nginx) for HTTPS if needed
- Add authentication layer if exposing to network
- Regular database backups (WAL + main DB)

---

## Future Improvements

### Planned (Tier 3)
- Split `ingestion/db.py` into package (T3-2)
- Organize scrapers into `ingestion/core/scrapers/` (T3-3)
- Move tools to `ingestion/tools/` (T3-4)
- Implement structured logging (T3-7)

### Potential Enhancements
- OAuth support for official Strava API
- Multi-user support with authentication
- Real-time activity notifications
- Activity comparison and analytics
- Export to GPX/TCX formats
- Mobile-responsive frontend
- Docker containerization
- Automated backfill scheduling

---

## References

- **PRD:** `docs/PRD.md` — Product requirements
- **CLI Testing:** `docs/CLI_TEST_MATRIX.md` — CLI test matrix
- **Delay Config:** `docs/DYNAMIC_DELAY_CONFIGURATION.md` — Delay tuning guide
- **Audit Report:** `AUDIT.md` — Codebase audit findings
- **Bug Tracking:** `bugfix.md` — Bug fix log
- **Task List:** `tasks.md` — Implementation tasks

---

## Glossary

- **WAL:** Write-Ahead Logging (SQLite journaling mode)
- **Stream:** Time-series data (lat/lng, altitude, time, etc.)
- **Backfill:** Historical data collection for past activities
- **Cursor:** Month-based position in backfill progress
- **Degraded:** Athlete/activity with known issues, skipped temporarily
- **Roster:** List of followed athletes
- **Feed:** Daily activity feed from Strava
- **Playback:** Date-based activity visualization
- **Session:** Authenticated Strava session (cookie-based)
