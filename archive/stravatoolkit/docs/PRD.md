# Product Requirements Document

## Purpose

Strava Toolkit provides a local-first archive of Strava activity data for the user and athletes they follow. The system stores structured activity metadata, GPS stream points, and downloadable media so the archive remains usable even when Strava data changes or disappears.

## Product Goals

1. Archive activities from the Strava following feed on demand.
2. Backfill historical activities with configurable limits and resumable progress.
3. Preserve route playback data with timestamped GPS points.
4. Provide a local viewer and API for map-based exploration.
5. Preserve profile photos and activity media when available.
6. Recover from expired authentication with minimal manual work.
7. Operate safely under rate limits and recover cleanly from interruptions.

## Primary Users

- Individual operator running the toolkit locally on Windows.
- Technical user running Python modules directly on non-Windows systems.

## Functional Requirements

### FR1 Activity Archiving
- The system shall sync activities for a target date from the Strava following feed.
- The system shall support a sync-today shortcut from `toolkit.bat`.
- The system shall ingest duplicate-safe activity records into SQLite.

### FR2 Historical Backfill
- The system shall backfill older activities using saved athlete cursors.
- The system shall enforce a configurable year cap.
- The system shall resume from saved cursor state after interruption.

### FR3 GPS Stream Storage
- The system shall store route points with longitude, latitude, and absolute unix timestamp.
- The system shall expose stored route history through the API for frontend playback.

### FR4 Viewer and API
- The system shall expose REST endpoints for status, activities, athletes, routes, and backfill coverage.
- The system shall serve a frontend capable of consuming those endpoints.
- The system shall support local FastAPI startup through `toolkit.bat backend` and `toolkit.bat viewer` workflows.

### FR5 Media Preservation
- The system shall download athlete profile photos.
- The system shall download discovered activity media.
- The system shall support all-media and date-filtered media download workflows.

### FR6 Authentication Recovery
- The system shall accept cookies from `data/.env` and `cookies.txt`.
- The system shall attempt automatic recovery when the active session expires.
- The system shall support Playwright-assisted login fallback.

### FR7 Rate Limiting and Resilience
- The system shall apply delay ranges before API requests.
- The system shall retry HTTP 429 responses with exponential backoff.
- The system shall apply cooldown/backoff when repeated auth recovery attempts fail.
- The system shall continue historical backfill when daily feed refresh fails transiently.

## Non-Functional Requirements

### NFR1 Local Operation
- The system shall run fully on the local machine without cloud dependencies.
- SQLite shall be the default database.

### NFR2 Recoverability
- The system shall use idempotent persistence where practical so interrupted runs can be restarted safely.
- The system shall persist backfill progress and authentication state.

### NFR3 Usability
- The Windows batch interface shall expose direct commands and interactive menus.
- The README shall document required environment variables and primary workflows.

### NFR4 Testability
- API contracts consumed by frontend hooks shall be covered by automated tests.
- CLI command coverage shall be maintained with a manual verification matrix when fully automated batch testing is not present.

## Out of Scope

- Multi-user deployment.
- Hosted cloud synchronization service.
- Official Strava API integration using OAuth app credentials.

## Acceptance Criteria

1. `toolkit.bat sync-today` archives the target day without duplicate corruption.
2. `toolkit.bat backfill` resumes from saved state and respects the year cap.
3. `GET /api/v1/activities?date=YYYY-MM-DD` returns a playback payload used by the frontend.
4. `GET /api/v1/athletes/{athlete_id}/routes` returns saved route history with path points.
5. Media download commands write files to the configured output directory.
6. Expired sessions trigger recovery attempts before hard failure.
