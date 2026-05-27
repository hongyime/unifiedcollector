# Unified Telegram Toolkit

Unified Telegram Toolkit is a multi-account Telegram operations platform for collection, enrichment, export, and visualization workflows.  
It orchestrates account-level scanning, controlled group joining, media retrieval, user intelligence extraction, profile-photo harvesting, bulk photo dispatch, and browser-based analytics views from a single CLI entry point.

## Current Product Scope

The current implementation centers on these production modules:

- `main.py` interactive/CLI orchestrator
- `toolkit/managers/processors/link_collector_processor.py` link discovery and bot-link filtering
- `toolkit/managers/join_groups.py` link validation/join orchestration with optional language filtering
- `toolkit/managers/leave_groups.py` group cleanup by title policy
- `toolkit/managers/processors/media_downloader_processor.py` resumable media download with hash dedupe
- `toolkit/managers/processors/user_analyzer_processor.py` SQLite-backed user and membership analysis
- `toolkit/managers/download_profile_photos.py` profile-photo download and reconciliation
- `toolkit/managers/send_photos.py` resumable multi-account photo dispatch to a chat
- `toolkit/managers/account_manager.py` account/session lifecycle management
- `toolkit/managers/manage_download_state.py` download state inspection/reset
- `toolkit/managers/backup.py` deleted-message export from admin log
- `toolkit/managers/resender.py` replay of backed-up messages

## Quick Reference

```bash
# Run the main interactive CLI
python main.py

# Detect dead code periodically
python scripts/detect_dead_code.py

# Run REST API server
python -m toolkit.server.api_server
```

## High-Level Architecture

1. Account credentials are loaded from `toolkit/core/config.py` (which reads `.env` first).
2. `main.py` routes user input to manager modules.
3. Managers communicate with Telegram using Telethon clients.
4. Runtime state is persisted to `data/` as:
   - text link queues
   - JSON progress/state files
   - CSV exports
   - SQLite analysis database
   - hash tracking files for deduplication
5. Web UIs in `web/` load generated `data/*.csv` or compact index JSON files.

## Core Capabilities

### 1) Link Collection
- Scans dialogs and messages across selected accounts.
- Extracts Telegram links using regex from message text sources.
- Filters bot-like links (keyword + entity checks).
- Handles channel-linked discussion groups by temporarily joining when needed.
- Persists progress in `data/scan_progress.json`.
- Appends discovered links to `data/collected_links.txt`.

### 2) Group Joining
- Reads candidate links from `data/valid_links.txt`.
- Validates links against a multi-account validation pool.
- Supports account selection modes (single, multi, all).
- Optionally enforces title-language filtering (blocks Cyrillic/Japanese titles).
- For non-megagroup channels, requires joinable linked discussion group; if unavailable, rollbacks channel join.
- Logs outcomes to `data/joined_links.txt` and discovered discussion groups to `data/discovered_discussion_groups.txt`.

### 3) Group Cleanup
- Scans memberships across selected accounts.
- Flags groups by naming policy (language or hidden-members criteria).
- Executes leave operations with confirmation and FloodWait handling.

### 4) Media Download
- Downloads supported media from groups/personal chats/channels using priority ordering.
- For channels, shifts scraping target to linked discussion groups.
- Uses per-account resume state: `data/<account>_download_state.json`.
- Uses hash dedupe tracking via `data/downloaded_hashes.txt` (legacy JSON fallback supported).
- Skips WebM videos, voice audio, and animations in primary account workflow.
- Organizes output by chat folder naming convention:
  - `group_<id>_<name>`
  - `supergroup_<id>_<name>`
  - `user_<id>_<name>`
  - `channel_<id>_<name>`

### 5) User Analysis
- SQLite-backed extraction (`data/users_analysis.db`) with WAL mode enabled.
- Data sources:
  - participant enumeration
  - message sender backfill
  - linked discussion groups for channels
- Resume/progress support in `data/analysis_progress.json`.
- Exports canonical outputs:
  - `data/Users.csv`
  - `data/Memberships.csv`
- Tracks profile changes over time using `toolkit/managers/user_change_tracker.py`:
  - `data/user_history.json`
  - `data/user_changes.json`
  - `data/user_changes.csv`
  - `data/users_enhanced.csv`
- Preserves last known non-empty usernames, names, and phone values across repeat scans so partial Telegram payloads do not overwrite prior identity data.
- Adds convenient alias history fields to `data/users_enhanced.csv`:
  - `current_name`
  - `historical_usernames`
  - `historical_names`
  - `username_history_count`
  - `name_history_count`
  - `last_username_change`
  - `last_name_change`

### 6) Profile Photo Download
- Loads users from `data/Users.csv`.
- Supports parallel and rotated-account strategies.
- Tracks downloaded assets using:
  - unified hash file (`data/downloaded_hashes.txt` by config)
  - centralized profile tracking (`data/downloaded_profile_photos.json`)
- Includes reconciliation mode (`PROFILE_RECONCILE_MODE`) to align filesystem and tracking metadata.

### 7) Bulk Photo Sending
- Sends image files from a local folder to a target chat across selected accounts.
- Validates image integrity using Pillow before send.
- Deduplicates by hash keys and tracks progress:
  - `data/photo_send_progress.json`
  - `data/sent_photo_hashes.txt`
- Supports optional source-file deletion after successful send.

### 8) Backup + Resend
- `backup.py`: extracts deleted admin-log messages from a source chat and stores dump in `deleted/messages_dump.json`.
- `resender.py`: replays backed-up messages to configured destination group with retry and pacing logic.

### 9) Visualization
- Dashboard: `web/enhanced_dashboard.html` / `web/dashboard.html`
- Network graph: `web/visualize.html`
- Primary data sources:
  - `data/dashboard_index.json` and `data/visualize_index.json` (if generated)
  - fallback to `data/Users.csv` and `data/Memberships.csv`

## Project Structure

```text
telegramtoolkit/
├── main.py                  # Main entry point
├── requirements.txt         # Python dependencies
├── .env                     # Configuration (create from .env.example)
├── data/                    # Database and exported data
├── sessions/                # Telegram session files
├── web/                     # Web dashboards (HTML/JS)
├── scripts/                 # Utility scripts
│   ├── configure_performance.py  # Performance settings tool
│   └── detect_dead_code.py       # Code quality analyzer
├── toolkit/                 # Core application code
│   ├── core/                # Core infrastructure
│   │   ├── config.py
│   │   ├── dynamic_config.py
│   │   ├── login_verifier.py
│   │   ├── parallel_processor.py
│   │   ├── progress_logger.py
│   │   ├── resilience.py
│   │   └── utils.py
│   ├── managers/            # Feature managers
│   └── server/              # API server
├── start_toolkit.bat        # Windows launcher
├── quick_actions.bat        # Quick menu (Windows)
└── start_server.bat         # Server launcher (Windows)
```

## Prerequisites

- Python 3.10+ (Windows-focused scripts are included; Linux/macOS are also possible with Python CLI usage)
- Telegram API credentials per account (`api_id`, `api_hash`, `phone`)
- Existing or newly created Telethon session files
- Network access to Telegram

Python dependencies:

- `telethon>=1.36.0`
- `pandas>=2.2.0`
- `openpyxl>=3.1.5`
- `Pillow>=10.4.0`
- `cryptg>=0.4.0` (optional acceleration)
- `aiofiles>=24.1.0`
- `python-dateutil>=2.9.0`
- `python-dotenv>=1.0.0` (recommended)

## Environment Configuration

Create `.env` in repository root with sequential account blocks.

### Required Variables

For each account index `N` starting at `1`:

- `ACCOUNT_N_NAME` (`string`)  
  Human-readable unique account name.
- `ACCOUNT_N_API_ID` (`integer`)  
  Telegram application API ID.
- `ACCOUNT_N_API_HASH` (`string`)  
  Telegram application API hash.
- `ACCOUNT_N_PHONE` (`string`)  
  E.164-style phone number, e.g. `+123456789`.
- `ACCOUNT_N_SESSION` (`string`)  
  Relative or absolute session file path, e.g. `sessions/659XXXXXXXX.session`.
- `ACCOUNT_N_PREFIX` (`string`)  
  Short identifier used by some management/migration routines.

Loading stops at the first missing `ACCOUNT_N_NAME`, so account numbering must be contiguous.

### Optional Variables

- `BACKUP_GROUP_ID` (`integer`, default from `config.py`)  
  Destination group/channel ID used by backup/resend workflows.

### Example (sanitized)

```env
ACCOUNT_1_NAME=account_one
ACCOUNT_1_API_ID=123456
ACCOUNT_1_API_HASH=your_api_hash_here
ACCOUNT_1_PHONE=+10000000000
ACCOUNT_1_SESSION=sessions/10000000000.session
ACCOUNT_1_PREFIX=acc1

ACCOUNT_2_NAME=account_two
ACCOUNT_2_API_ID=234567
ACCOUNT_2_API_HASH=your_api_hash_here
ACCOUNT_2_PHONE=+10000000001
ACCOUNT_2_SESSION=sessions/10000000001.session
ACCOUNT_2_PREFIX=acc2

BACKUP_GROUP_ID=-1001234567890
```

## Installation & Setup

1. Clone repository.
2. Create and activate Python virtual environment.
3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Create `.env` using sanitized template above.
5. Ensure `sessions/` exists and contains matching `.session` files (or generate sessions via Account Manager).
6. Run account verification through toolkit flow before heavy operations.

Windows launcher alternative:

```bash
start_toolkit.bat
```

## Usage

### Interactive Mode

```bash
python main.py
```

### Command Mode

```bash
python main.py links
python main.py join
python main.py leave
python main.py media
python main.py users
python main.py profiles
python main.py photos
python main.py dashboard
python main.py visualize
python main.py pipeline
python main.py accounts
python main.py state
python main.py export
python main.py backup
python main.py resend
```

### Typical Operational Workflow

1. `python main.py links`
2. `python main.py join`
3. `python main.py media`
4. `python main.py users`
5. `python main.py profiles`
6. `python main.py dashboard` / `python main.py visualize`

## Data Artifacts

- `data/collected_links.txt`
- `data/valid_links.txt`
- `data/joined_links.txt`
- `data/discovered_discussion_groups.txt`
- `data/Users.csv`
- `data/Memberships.csv`
- `data/user_history.json`
- `data/user_changes.json`
- `data/user_changes.csv`
- `data/users_enhanced.csv`
- `data/users_analysis.db`
- `data/dashboard_index.json`
- `data/visualize_index.json`
- `data/analysis_progress.json`
- `data/scan_progress.json`
- `data/<account>_download_state.json`
- `data/downloaded_hashes.txt`
- `data/downloaded_profile_photos.json`
- `data/photo_send_progress.json`
- `data/sent_photo_hashes.txt`
- `deleted/messages_dump.json`

## Testing

No formal automated unit/integration test suite is currently defined in repository configuration.

Recommended validation path after changes:

1. Verify accounts: `python main.py accounts` → test login option.
2. Smoke-run one lightweight command:
   - `python main.py links` on a small subset, or
   - `python main.py state` to verify state subsystem.
3. Validate web data rendering:
   - Run `python main.py` and select "Open Dashboard" or "Open Visualizer"
   - Open the displayed URL in your browser
4. Confirm exports are generated and readable in `data/`.

## Deployment

This project is designed primarily for local/desktop operation.

- No container/orchestrator manifests are included.
- No CI/CD pipeline configuration is included.
- Web delivery is via local Python HTTP server on port `8000`.

Production-style deployment baseline (if needed):

1. Provision secure host with Python runtime.
2. Keep `.env` and `sessions/` outside source control.
3. Run toolkit via scheduler or supervised process for selected commands.
4. Restrict HTTP server exposure to trusted network/VPN only.

## Security Notes

- Session files (`sessions/*.session`) are authentication artifacts; treat as secrets.
- `.env` contains account credentials and must never be committed.
- Local web server blocks direct serving of sensitive paths (`.env`, `sessions/`, `.git/`, and `toolkit/core/config.py`), but should still be considered non-hardened for internet exposure.
- Data exports can contain personal identifiers; apply your jurisdiction’s privacy/compliance controls.

## Known Technical Constraints

- Configuration currently supports up to 19 sequentially indexed account blocks.
- Several workflows rely on interactive prompts and are not fully non-interactive.
- Some CSV schema expectations in web indexing tolerate missing fields by fallback rather than strict validation.
- Retry/rate-limit handling is robust but still bounded by Telegram restrictions and account health.
- SQLite is tuned for concurrent reads plus a single active writer; avoid running multiple long-lived write-heavy toolkit jobs against the same `data/users_analysis.db` at the same time.
- When the database is temporarily busy, CLI/API/import/export workflows now surface a lock/busy message and should be retried after the active write completes instead of forcing recovery or deleting the database.

## License & Responsibility

Use is intended for lawful research and administrative operations.  
Operators are responsible for complying with Telegram terms, local regulations, and privacy obligations.
