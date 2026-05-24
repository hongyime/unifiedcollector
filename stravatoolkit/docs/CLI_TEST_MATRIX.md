# CLI Test Matrix

## Scope

This matrix documents manual verification for `toolkit.bat` command paths that are difficult to cover with automated tests on Windows batch menus.

## Preconditions

- Run commands from the repository root.
- Activate the project virtual environment or use `toolkit.bat` directly.
- Prepare `cookies.txt` or `data/.env` with a valid Strava session when exercising live sync flows.

## Direct Command Coverage

| Command | Expected Result | Status Evidence |
|---------|-----------------|-----------------|
| `toolkit.bat sync-today` | Runs one daily sync using today's date | Covered by operational workflow; verify crawler summary and exit code |
| `toolkit.bat sync-date YYYY-MM-DD` | Runs one daily sync for supplied date | Verify date-specific run and exit code |
| `toolkit.bat watch [interval] [steps] [parallelism] [year-cap]` | Enters repeating sync loop until interrupted | Verify startup banner and Ctrl+C behavior |
| `toolkit.bat backfill [parallelism] [year-cap]` | Runs deep historical backfill | Verify backfill progress output and resumability |
| `toolkit.bat status` | Prints archive summary | Verify roster, backfill, and last sync fields |
| `toolkit.bat photos-profiles [output-dir]` | Downloads profile photos | Verify files created in output directory |
| `toolkit.bat photos-activities [output-dir]` | Downloads discovered activity media | Verify media files created |
| `toolkit.bat photos-date YYYY-MM-DD [output-dir]` | Downloads activity media for one date | Verify date-filtered output |
| `toolkit.bat photos-all [output-dir]` | Runs profile and activity media download flow | Verify combined output |
| `toolkit.bat backend` | Starts FastAPI server on `127.0.0.1:8000` | Verify server responds on `/api/v1/status` |
| `toolkit.bat backend-stop` | Stops process on port 8000 | Verify process exits and port is free |
| `toolkit.bat backend-status` | Reports backend PID or free state | Verify output reflects actual server state |
| `toolkit.bat build` | Builds frontend assets in `frontend/dist` | Verify build exits successfully |
| `toolkit.bat setup` | Creates venv, installs deps, prompts for auth bootstrap | Verify setup completion messages |
| `toolkit.bat help` | Prints usage/help guidance | Verify help text appears and exits cleanly |

## Interactive Menu Coverage

| Menu Path | Expected Result |
|-----------|-----------------|
| Main -> Archive and Routes | Opens archive menu |
| Main -> Media Downloads | Opens media menu |
| Main -> Viewer App | Opens viewer menu |
| Main -> Status, Setup, and Help | Opens support menu |
| Archive -> Sync today once | Executes `sync-today` flow |
| Archive -> Sync a specific date once | Prompts for date then executes sync |
| Archive -> Keep today fresh | Starts watch mode |
| Archive -> Deepen saved history | Starts backfill flow |
| Archive -> Show archive status | Prints status and returns to archive menu |
| Media -> Save profile photos | Executes profile photo download |
| Media -> Save all discovered activity media | Executes activity media download |
| Media -> Save activity media for one date | Prompts for date and downloads filtered media |
| Media -> Save everything | Executes all media download flow |
| Viewer -> Start local viewer | Starts backend on port 8000 |
| Viewer -> Stop local viewer | Stops backend |
| Viewer -> Viewer status | Prints backend status |
| Viewer -> Rebuild frontend | Runs frontend build |
| Support -> Show archive status | Prints status |
| Support -> First-time setup | Runs setup |
| Support -> Help | Prints help |

## Verification Notes

- Record date, operator, and outcome when running the matrix.
- Re-run affected rows whenever `toolkit.bat` command routing or arguments change.
- If a direct command gains automated coverage later, update this matrix to reference the test file.
