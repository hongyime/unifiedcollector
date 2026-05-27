# Instagram Data Collection Toolkit

A production-grade Python CLI application for automated Instagram data collection and media archival with enterprise-level resilience features.

---

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [System Requirements](#system-requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Quick Start](#quick-start)
- [Usage](#usage)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Security](#security)
- [Architecture](#architecture)

---

## Overview

The Instagram Data Collection Toolkit provides automated data collection and media downloading capabilities for Instagram profiles with built-in resilience features including multi-account rotation, intelligent quota management, and comprehensive progress tracking.

### What It Does

- **Data Collection**: Collect follower/following relationships for public profiles
- **Media Archival**: Download posts, stories, highlights, and profile photos
- **Batch Processing**: Process multiple users automatically with account rotation
- **Network Analysis**: Generate analytics reports from collected data
- **Progress Tracking**: Resume operations from interruption without data loss

### What It Does NOT Do

- Access private profiles (Instagram API limitation)
- Bypass rate limits or anti-scraping measures
- Store credentials remotely (local-only security model)
- Guarantee 100% data completeness (subject to Instagram API limitations)

---

## Features

### Data Collection
- ✅ Spider single user or batch process from database
- ✅ Seed username list from accounts' followers/following
- ✅ Mutual connection filtering (users followed by N+ accounts)
- ✅ High-priority user filtering
- ✅ Progress tracking and resumption

### Media Downloads
- ✅ Download posts, stories, highlights, profile photos
- ✅ Batch download with account rotation
- ✅ Following-based downloads (only from followed accounts)
- ✅ Selective downloads (hand-picked username list)
- ✅ Media type filtering (posts-only, stories-only, etc.)

### Resilience Features
- ✅ Multi-account rotation on rate limits
- ✅ 15-minute cooldown management
- ✅ Daily quota enforcement (180 views, 6000 actions per account)
- ✅ 2FA support with interactive OTP input
- ✅ Intelligent routing (best account per profile)
- ✅ Retry with exponential backoff

### Data Management
- ✅ SQLite database with 11-table schema
- ✅ Database migration from legacy JSON files
- ✅ Priority analysis for batch operations
- ✅ User network analysis and reporting
- ✅ Profile access statistics

---

## System Requirements

### Prerequisites

| Requirement | Minimum | Recommended |
|-------------|---------|-------------|
| **Python** | 3.7+ | 3.12 |
| **Disk Space** | 100 MB for toolkit | Depends on media downloads |
| **RAM** | 256 MB | 512 MB |
| **Internet** | Stable connection | High-speed recommended |

### Operating Systems

- **Windows**: Primary support (tested on Windows 11)
- **Unix/Linux**: Full support with file locking via `fcntl`
- **macOS**: Full support with file locking via `fcntl`

### Required Python Packages

| Package | Version | Purpose |
|---------|---------|---------|
| `instaloader` | 4.15 | Instagram API client |
| `requests` | 2.33.1 | HTTP client |
| `python-dotenv` | 1.2.2 | Environment variable loading |
| `pytest` | 9.0.3 | Testing framework |
| `hypothesis` | 6.152.2 | Property-based testing |

---

## Installation

### Step 1: Clone the Repository

```bash
git clone <repository-url>
cd instagramtoolkit
```

### Step 2: Set Up Virtual Environment

**Windows:**
```bash
setup.bat
```

**Manual Setup:**
```bash
# Create virtual environment
python -m venv .venv

# Activate virtual environment
# Windows
.venv\Scripts\activate
# Unix/Linux/macOS
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Step 3: Verify Installation

```bash
python -c "import sys; sys.path.insert(0, 'lib'); from config import INSTAGRAM_ACCOUNTS; print(f'Installation verified - {len(INSTAGRAM_ACCOUNTS)} accounts configured')"
```

Expected output: `Installation verified - 0 accounts configured`

### Step 4: Configure Instagram Accounts

Create `.env` file in the project root (copy from `.env.example`):

```env
# Database Configuration (optional - defaults to SQLite)
DATABASE_URL=sqlite:///data/instagram_toolkit.db

# Download Filters (optional - 0 = no filter)
FILTER_MAX_FOLLOWERS=1000

# Account 1 (required)
INSTA_ACCOUNT_1_NAME=main
INSTA_ACCOUNT_1_USER=your_instagram_username
INSTA_ACCOUNT_1_PASS=your_instagram_password

# Account 2 (optional - for rotation)
INSTA_ACCOUNT_2_NAME=alt
INSTA_ACCOUNT_2_USER=other_username
INSTA_ACCOUNT_2_PASS=other_password
```

**Important**: Never commit `.env` file to version control. It's listed in `.gitignore`.

---

## Configuration

### Environment Variables

| Variable | Description | Example | Required |
|----------|-------------|---------|----------|
| `DATABASE_URL` | Database connection string | `sqlite:///data/instagram_toolkit.db` | No (defaults to SQLite) |
| `FILTER_MAX_FOLLOWERS` | Download filter threshold | `1000` | No (0 = no filter) |
| `INSTA_ACCOUNT_{N}_NAME` | Short identifier | `"main"` | Yes |
| `INSTA_ACCOUNT_{N}_USER` | Instagram username | `"username"` | Yes |
| `INSTA_ACCOUNT_{N}_PASS` | Instagram password | `"password"` | Yes |
| `INSTA_ACCOUNT_{N}_BROWSER` | Browser for cookie import | `"Chrome"` | No |
| `INSTA_ACCOUNT_{N}_PROXY` | Per-account SOCKS5 proxy | `"socks5://user:pass@host:port"` | No |
| `PROXY_URL` | Global proxy for all accounts | `"socks5://user:pass@host:port"` | No |

### Directory Configuration

All directories are created automatically on first use:

| Directory | Purpose |
|-----------|---------|
| `data/` | SQLite database and legacy data files |
| `downloads/` | Default media download location (configurable) |
| `sessions/` | Instaloader session files (sensitive) |
| `archived_logs/` | Archived progress and logs |

### Rate Limiting Configuration

Default rate limits (in `src/config.py`):

| Parameter | Default Value | Description |
|-----------|---------------|-------------|
| `MIN_DELAY` | 20s | Minimum delay between operations |
| `MAX_DELAY` | 40s | Maximum delay between operations |
| `ENUM_PAUSE_EVERY` | 12 | Pause every N operations during enumeration |
| `ENUM_PAUSE_SECONDS` | 30s | Pause duration during enumeration |
| `ACCOUNT_COOLDOWN_MINUTES` | 15 | Cooldown after rate limit |
| `DAILY_QUOTA_PROFILE_VIEWS` | 180 | Max profile views/day per account |
| `DAILY_QUOTA_ACTIONS` | 6000 | Max actions/day per account |

---

## Quick Start

### Windows Users: Interactive Menu

```bash
start_toolkit.bat
```

This opens an interactive menu with all operations.

### Command Line Interface

#### List Configured Accounts

```bash
python main.py list
```

#### Test All Account Logins

```bash
python main.py test-all
```

#### Collect Relationships (Spider)

```bash
# Spider single user
python main.py spider --username target_user

# Spider batch (all users in database)
python main.py spider --batch

# Build username list from accounts' followers, then spider
python main.py spider --seed

# Collect only mutual connections (followed by 2+ accounts)
python main.py spider --seed --seed-mutual --min-mutual 2

# Spider with specific account
python main.py spider --username target_user --account account_name
```

#### Download Media

```bash
# Download single user
python main.py download --username target_user

# Download batch
python main.py download --batch

# Download with post limit
python main.py download --username target_user --limit 20

# Download only posts
python main.py download --username target_user --posts-only
```

#### Following Media Download

```bash
# Interactive menu
python main.py following-download --interactive

# Download from specific followed account
python main.py following-download --username followed_account

# Download from all followed accounts
python main.py following-download --all
```

#### Selective Download

```bash
# Interactive selection
python main.py selective-download --select

# Show current selection
python main.py selective-download --list

# Add username to selection
python main.py selective-download --add target_user

# Download from selection
python main.py selective-download --download
```

#### Progress Management

```bash
# Show progress for all operations
python main.py progress show

# Resume interrupted operation
python main.py progress resume --operation spider

# Retry failed users
python main.py progress resume --operation spider --retry-failed

# Clear progress
python main.py progress clear --operation spider --confirm
```

#### Analytics

```bash
# Analyze collected user network
python main.py analyze

# Analyze profile metadata
python main.py analyze-profiles

# View profile access statistics
python main.py access-stats

# Priority analysis
python main.py priority-analysis --account account_name
```

#### Database Management

```bash
# Migrate JSON files to database
python main.py db-migrate

# Reset database (clear all data, keep schema)
python main.py db-reset

# Add username to tracking database
python main.py add-username target_user

# List all tracked usernames
python main.py list-usernames

# Cleanup backup files
python main.py cleanup-bak
```

---

## Usage

### Complete Workflow Example

#### Step 1: Build Target List

```bash
# Seed username list from your accounts' followers
python main.py spider --seed-only
```

This creates a username list in the database from your accounts' followers/following.

#### Step 2: Collect Relationships

```bash
# Spider all users in database
python main.py spider --batch
```

This collects follower/following data for all users with automatic account rotation.

#### Step 3: Download Media

```bash
# Download media for all users
python main.py download --batch
```

This downloads posts, stories, highlights, and profile photos with account rotation.

#### Step 4: Analyze Data

```bash
# Generate reports
python main.py analyze
```

This generates network analysis reports from collected data.

### Batch Processing with Account Rotation

All batch operations automatically rotate accounts when rate limits are encountered:

```bash
# Spider batch with account rotation
python main.py spider --batch

# Download batch with account rotation
python main.py download --batch
```

The system will:
1. Start with the first available account
2. Switch to next account on rate limit
3. Apply 15-minute cooldown to rate-limited account
4. Continue with remaining accounts
5. Resume from interruption if stopped

### Quota Management

Quotas are enforced automatically per account:

- **Profile Views**: 180/day per account
- **General Actions**: 6000/day per account
- **Reset Time**: Midnight (00:00) server time

When quota is exhausted, the tool automatically switches to the next available account.

### 2FA Authentication

If an account requires two-factor authentication:

```bash
python main.py login account_name
```

Interactive prompt:
```
🔐 2FA required for account_name
Enter 2FA code (or 'skip'): <OTP_CODE>
```

You have 3 retry attempts per session.

### Progress Resumption

All operations support resumption from interruptions:

```bash
# Resume interrupted spider
python main.py progress resume --operation spider

# Resume and retry failed users
python main.py progress resume --operation spider --retry-failed
```

Progress is stored in the database and survives crashes.

---

## Testing

### Run All Tests

**Windows:**
```bash
run_tests.bat
```

**Manual:**
```bash
# Unit tests only (offline, fast)
python -m pytest tests/ -v

# Unit + integration tests (requires --run-integration flag)
python -m pytest tests/ -v --run-integration

# Integration tests only
python -m pytest tests/ -v --run-integration -m integration
```

### Test Coverage

```
collected 1015 items
```

1015 test cases across multiple test modules covering:
- **Unit tests**: Offline, no API calls
- **Integration tests**: Instagram API calls with active sessions (requires `--run-integration` flag)

---

## Troubleshooting

### Common Issues

#### "No accounts found in .env file"

**Cause**: `.env` file missing or misconfigured

**Solution**:
1. Create `.env` file in project root
2. Add at least one account with credentials
3. Verify format: `INSTA_ACCOUNT_1_NAME`, `INSTA_ACCOUNT_1_USER`, `INSTA_ACCOUNT_1_PASS`

#### "Login failed for account"

**Cause**: Invalid credentials, 2FA required, or account locked

**Solution**:
1. Verify username and password in `.env`
2. Check if account requires 2FA (use interactive login with OTP)
3. Check account status on Instagram web/app

#### "Rate limit detected"

**Cause**: Account exceeded Instagram API limits

**Solution**:
1. Wait for cooldown (default 15 minutes)
2. Tool will automatically switch to next account
3. Increase delays in `src/config.py` if needed

#### "Quota exhausted for account_name"

**Cause**: Daily quota limit reached (180 views or 6000 actions)

**Solution**:
1. Tool will automatically switch to next available account
2. Wait until next day for quota reset
3. Add more accounts for rotation capacity

#### "Database is locked"

**Cause**: Another process is using the database

**Solution**:
1. Wait for other operation to complete
2. Or close other processes accessing the database
3. Database uses WAL mode for better concurrency

### Getting Help

#### Log Files

Check archived logs in:
- `archived_logs/` - Archived progress and logs

#### Progress State

Check progress state:
```bash
python main.py progress show
```

#### Diagnostic Scripts

```bash
# Check account access
python scripts/check_account_access.py

# Debug profile access
python scripts/debug_profile_access.py

# Refresh sessions
python scripts/refresh_sessions.py
```

---

## Security

### Critical Security Notice

**🚨 NEVER COMMIT CREDENTIALS TO VERSION CONTROL**

The `.env` file contains sensitive Instagram credentials and must never be committed to version control. If you accidentally commit credentials:

1. **Immediately rotate all exposed passwords** on Instagram
2. Remove the commit from git history using `git filter-branch` or BFG Repo-Cleaner
3. Force push to remote repository
4. Notify all collaborators to re-clone the repository

### Security Best Practices

#### Credential Management
- Use strong, unique passwords for each Instagram account
- Rotate passwords regularly (every 90 days recommended)
- Never share credentials or session files
- Store `.env` file securely with restricted file permissions

#### Session File Security
- Session files in `sessions/` directory contain authentication tokens
- Treat session files as sensitive as passwords
- Delete session files when no longer needed
- Never commit session files to version control

#### Proxy Configuration Security
- Use trusted proxy providers only
- Verify proxy supports SOCKS5 protocol
- Test proxy connection before large operations
- Monitor proxy for suspicious activity

#### Network Security
- Use secure, private networks for operations
- Avoid public WiFi for authentication
- Consider VPN for additional privacy layer
- Monitor for unusual account activity

### Data Privacy

- All data stored locally only (no remote transmission)
- Database file (`data/instagram_toolkit.db`) contains collected data
- Downloaded media stored in `downloads/` directory
- No telemetry or analytics sent to external servers

---

## Architecture

### Component Overview

```
CLI Layer (main.py)
    ↓
Command Dispatcher
    ↓
Processing Layer
    ├── InstagramProcessor (batch operations)
    ├── RelationshipCollector (data collection)
    ├── MediaDownloader (media downloads)
    └── FollowingMediaDownloader (following-only)
    ↓
Service Layer
    ├── RateLimiter (rate management)
    ├── AccountCooldownManager (cooldowns)
    ├── AccountQuotaManager (quotas)
    ├── ProfileAccessTracker (access patterns)
    ├── ProgressManager (progress tracking)
    └── InstagramAccountManager (authentication)
    ↓
Data Layer
    ├── DatabaseManager (SQLite/PostgreSQL)
    ├── Repositories (data access)
    └── Schema (11-table database)
    ↓
API Layer
    Instaloader (Instagram API client)
```

### Design Principles

1. **Modularity**: Each component has a single, well-defined responsibility
2. **Resilience**: All operations support retry, account switching, and resumption
3. **Safety**: Database transactions and atomic writes prevent data loss
4. **Extensibility**: Command-based architecture allows easy feature additions
5. **Security**: Local-only credential storage, no remote data transmission

### Database Schema

The system uses an 11-table SQLite database:

| Table | Purpose |
|-------|---------|
| `profiles` | User profile metadata |
| `profile_snapshots` | Historical profile data |
| `relationships` | Follower/following connections |
| `usernames` | Tracked username list |
| `username_following_status` | Following status per account |
| `profile_access_attempts` | Access attempt history |
| `profile_access_summary` | Aggregated access data |
| `operation_progress` | Operation state tracking |
| `batch_state` | Batch processing state |
| `account_cooldowns` | Account cooldown tracking |
| `account_quotas` | Daily quota usage |

---

## File Structure

```
instagramtoolkit/
├── data/                           # Data storage
│   ├── instagram_toolkit.db        # SQLite database (primary data store)
│   └── usernames.txt               # Legacy username list (optional)
├── downloads/                      # Media downloads (configurable)
├── sessions/                       # Instagram session files (sensitive)
│   ├── .gitkeep                    # Keep directory in git
│   └── {username}                  # Session file per account
├── archived_logs/                  # Archived progress and logs
├── src/                           # Library modules (32 files)
│   ├── commands/                   # CLI command implementations
│   │   ├── base.py                # BaseCommand abstract class
│   │   ├── spider.py              # Spider command
│   │   ├── download.py            # Download command
│   │   └── following_download.py  # Following download command
│   ├── db/                        # Database layer
│   │   ├── repositories/          # Data access layer
│   │   ├── backends.py            # SQLite/PostgreSQL backends
│   │   ├── manager.py             # DatabaseManager
│   │   ├── migrate_json.py        # JSON-to-DB migration
│   │   └── schema.py              # Database schema DDL
│   ├── account_cooldown.py         # Cooldown management
│   ├── account_manager.py          # Authentication
│   ├── analyze_users.py            # User analysis
│   ├── archive_manager.py          # Archive retention
│   ├── batch_processor.py          # Batch processing
│   ├── cli_helpers.py              # CLI utilities
│   ├── collect_relationships.py   # Data collection
│   ├── config.py                   # Configuration
│   ├── conservative_rate_limiter.py # Conservative rate limiting
│   ├── download_media.py          # Media downloads
│   ├── download_path_manager.py   # Path management
│   ├── exception_handler.py        # Error handling
│   ├── following_media_downloader.py # Following downloads
│   ├── io_utils.py                # I/O utilities
│   ├── media_utils.py             # Media utilities
│   ├── operation_classifier.py     # Operation classification
│   ├── operation_router.py         # Operation routing
│   ├── parallel_processor.py      # Batch processor (main)
│   ├── priority_manager.py         # Priority analysis
│   ├── profile_access_tracker.py   # Access tracking
│   ├── profile_analyzer.py         # Profile analysis
│   ├── progress_manager.py         # Progress management
│   ├── rate_limiter.py            # Rate limiting
│   ├── selective_download_manager.py # Selective downloads
│   ├── smart_account_selector.py   # Account selection
│   ├── user_metadata_manager.py    # User metadata
│   ├── username_database.py        # Username database
│   └── validation.py              # Input validation
├── tests/                         # Test suite (1015 tests)
│   ├── conftest.py                # Pytest configuration
│   └── test_*.py                  # Unit/integration tests
├── web/                           # Web dashboard (local development only)
│   ├── server.py                  # HTTP server
│   ├── dashboard.html              # Frontend
│   ├── dashboard.js               # Dashboard logic
│   └── style.css                  # Styling
├── scripts/                       # Utility scripts
│   ├── check_account_access.py
│   ├── debug_profile_access.py
│   └── refresh_sessions.py
├── .env                           # Credentials (gitignored)
├── .env.example                   # Template for .env
├── .gitignore                     # Git exclusions
├── requirements.txt               # Python dependencies
├── pytest.ini                     # Pytest configuration
├── pyproject.toml                 # Project metadata
├── main.py                        # CLI entry point
├── setup.bat                      # Windows setup script
├── start_toolkit.bat              # Windows menu interface
├── run_tests.bat                  # Test runner
├── quick_actions.bat              # Quick workflows
├── README.md                      # This file
└── PRD.md                         # Product Requirements Document
```

---

## Best Practices

### Safe Usage

1. **Respect Rate Limits**: Default settings are safe. Avoid reducing delays.
2. **Use Multiple Accounts**: Rotate accounts to distribute API load.
3. **Monitor Quotas**: Check quota usage before large operations.
4. **Test First**: Run small test operations before large batches.
5. **Backup Data**: Regular backups of `data/` directory.

### Security

1. **Protect `.env` File**: Never commit to version control
2. **Use Strong Passwords**: Secure Instagram account passwords
3. **Rotate Accounts**: Don't rely on single account
4. **Secure Sessions**: Session files in `sessions/` directory are sensitive

### Performance

1. **Use Batch Mode**: Batch operations with rotation are more efficient
2. **Priority Processing**: Use `--high-priority-only` for targeted operations
3. **Resume Operations**: Don't restart from scratch on interruption
4. **Database Maintenance**: Periodic database cleanup for optimal performance

---

## Web Dashboard

The toolkit includes a web-based dashboard for visualizing collected relationship data.

### Starting the Dashboard

```bash
python web/server.py
```

Access at: `http://localhost:8080`

### Features

- Network graph visualization of follower/following relationships
- Interactive node exploration
- Profile statistics display
- Real-time data loading from database

### Security Warning

**⚠️ LOCAL DEVELOPMENT ONLY**

The web dashboard has significant security limitations:
- No authentication (accessible to anyone on the network)
- CORS configured to allow all origins (`*`)
- Server binds to all interfaces (`0.0.0.0`)
- Not suitable for production deployment

**Recommended Usage**: Local development and testing on trusted, private networks only.

---

## Development

### Adding New Commands

Create new command in `src/commands/`:

```python
# src/commands/new_command.py
from src.commands.base import BaseCommand
import argparse

class NewCommand(BaseCommand):
    """Description of command."""
    
    name = "new-command"
    description = "Command description"
    help_text = "Help text"
    
    def _add_arguments(self):
        """Add arguments."""
        self.parser.add_argument('value', help='Value to process')
    
    def execute(self, args: argparse.Namespace) -> int:
        """Execute command."""
        print(f"Processing: {args.value}")
        return 0
```

Register in `src/commands/__init__.py`:

```python
def get_commands() -> dict[str, type[BaseCommand]]:
    return {
        # existing commands...
        'new-command': NewCommand,
    }
```

### Running Tests

```bash
# Run all tests
python -m pytest tests/ -v

# Run specific test file
python -m pytest tests/test_account_manager.py -v

# Run with coverage (if installed)
python -m pytest tests/ --cov=lib --cov-report=html
```

---

## License

This project is intended for personal educational and research purposes only.

---

## Disclaimer

This toolkit is for educational and research purposes only. Users are responsible for:

- Complying with Instagram's Terms of Service
- Respecting user privacy and copyright
- Using collected data ethically and legally
- Not using accounts that don't belong to you without permission

The authors are not responsible for any misuse of this toolkit or violations of Instagram's Terms of Service.

---

## Support

For issues, questions, or contributions:

1. Check the [Troubleshooting](#troubleshooting) section
2. Review the [PRD.md](PRD.md) for detailed technical specifications
3. Run diagnostic scripts in `scripts/` directory
4. Check archived logs in `archived_logs/` directory

---

**README Version:** 3.0  
**Last Updated:** 2026-04-26  
**Status:** Production Ready

All features documented in this README have been verified to exist in the codebase and are functional as of the last update date.
