# Instagram Data Collection Toolkit - Product Requirements Document

**Version:** 3.0  
**Status:** Production Ready  
**Last Updated:** 2026-04-26  
**Document Type:** Technical Specification

---

## Executive Summary

The Instagram Data Collection Toolkit is a production-grade Python CLI application designed for automated Instagram data collection and media archival. Built with enterprise-level resilience features, the system provides multi-account rotation, intelligent quota management, and comprehensive progress tracking to ensure reliable operation within Instagram's platform constraints.

### Core Value Proposition

**Automated Intelligence**: Collect follower/following relationships and media content from public Instagram profiles with zero manual intervention.

**Enterprise Resilience**: Multi-account rotation with automatic failover prevents rate-limiting failures and ensures continuous operation.

**Data Integrity**: Database-backed persistence with atomic writes and file locking guarantees zero data loss during interruptions.

**Platform Compliance**: Built-in quota enforcement (180 profile views/day, 6000 actions/day per account) respects Instagram's API limitations.

### Target Use Cases

- **Social Network Research**: Analyze follower/following relationships across user networks
- **Content Archival**: Preserve Instagram media (posts, stories, highlights) for backup or analysis
- **Competitive Intelligence**: Track profile metrics and content strategies over time
- **Data Science**: Build datasets for machine learning and network analysis

---

## System Architecture

### Architectural Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                      CLI Interface Layer                        │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │  Spider  │  │ Download │  │Following │  │Selective │      │
│  │ Commands │  │ Commands │  │Download  │  │Download  │      │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘      │
└───────┼────────────┼────────────┼────────────┼─────────────────┘
        │            │            │            │
        └────────────┴────────────┴────────────┘
                               │
                    ┌──────────▼──────────┐
                    │  main.py (Router)    │
                    │  Command Dispatcher  │
                    └──────────┬──────────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
┌───────▼────────┐    ┌───────▼────────┐    ┌───────▼────────┐
│ Data Collection │    │ Media Downloads │    │  Data Analysis  │
│                 │    │                 │    │                 │
│ Relationship    │    │ MediaDownloader │    │  UserAnalyzer   │
│ Collector       │    │ Following       │    │  ProfileAnalyzer│
│                 │    │ Downloader      │    │                 │
└───────┬─────────┘    └───────┬─────────┘    └───────┬─────────┘
        │                      │                      │
        └──────────────────────┴──────────────────────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
┌───────▼────────┐    ┌───────▼────────┐    ┌───────▼────────┐
│ Rate Limiting  │    │ Account Mgmt   │    │  Persistence    │
│                 │    │                │    │                 │
│ RateLimiter     │    │ Cooldown       │    │ ProgressManager │
│ QuotaManager    │    │ Manager        │    │ DatabaseManager │
│                 │    │ ProfileAccess  │    │                 │
└───────┬─────────┘    └───────┬─────────┘    └───────┬─────────┘
        │                      │                      │
        └──────────────────────┴──────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │   Instaloader API   │
                    │  (Instagram Client) │
                    └─────────────────────┘
```

### Component Responsibilities

#### Layer 1: CLI Interface
- **Command Parsers**: Argument parsing and validation
- **Interactive Menus**: Windows batch file interface for ease of use
- **Help System**: Comprehensive `--help` documentation

#### Layer 2: Business Logic
- **InstagramProcessor**: Orchestrates batch operations with account rotation
- **RelationshipCollector**: Collects follower/following data with retry logic
- **MediaDownloader**: Downloads posts, stories, highlights, profile photos
- **FollowingMediaDownloader**: Specialized downloader for followed accounts only
- **SelectiveDownloadManager**: Interactive username selection and batch download

#### Layer 3: Infrastructure Services
- **RateLimiter**: Enforces delays (20-40s base, 30s periodic pauses)
- **AccountCooldownManager**: 15-minute cooldown after rate-limit hits
- **AccountQuotaManager**: Daily quota tracking (180 views, 6000 actions per account)
- **ProfileAccessTracker**: Tracks which accounts can access which profiles
- **ProgressManager**: Persistent operation state for resumption
- **DatabaseManager**: SQLite/PostgreSQL abstraction layer

#### Layer 4: Data Persistence
- **SQLite Database**: 11-table schema for all operational data
- **Atomic Writes**: Temp file + rename pattern prevents corruption
- **File Locking**: Cross-platform locking (Windows/Unix) for concurrent safety

---

## Feature Matrix

### Data Collection Features

| Feature | Status | Description | CLI Command |
|---------|--------|-------------|-------------|
| **Spider Single User** | ✅ Production | Collect followers/following for one user | `python main.py spider --username <user>` |
| **Spider Batch** | ✅ Production | Batch spider all users from database | `python main.py spider --batch` |
| **Seeding** | ✅ Production | Build username list from accounts' connections | `python main.py spider --seed` |
| **Mutual Connections** | ✅ Production | Collect only users followed by N+ accounts | `python main.py spider --seed --seed-mutual --min-mutual 2` |
| **Selective Collection** | ✅ Production | Collect only followers or only following | `--seed-followers-only` / `--seed-following-only` |
| **High-Priority Filter** | ✅ Production | Process high-priority users first | `--high-priority-only` |
| **Progress Reset** | ✅ Production | Clear all progress data | `python main.py spider --reset` |

### Media Download Features

| Feature | Status | Description | CLI Command |
|---------|--------|-------------|-------------|
| **Download Single User** | ✅ Production | Download all media for one user | `python main.py download --username <user>` |
| **Download Batch** | ✅ Production | Batch download all users from database | `python main.py download --batch` |
| **Following Download** | ✅ Production | Download only from followed accounts | `python main.py following-download --interactive` |
| **Selective Download** | ✅ Production | Download from hand-picked username list | `python main.py selective-download --select` |
| **Profile Photos** | ✅ Production | Download only profile photos | `--profile-only` |
| **Posts Only** | ✅ Production | Download only posts | `--posts-only` |
| **Stories Only** | ✅ Production | Download only stories | `--stories-only` |
| **Highlights Only** | ✅ Production | Download only highlights | `--highlights-only` |
| **Post Limit** | ✅ Production | Limit number of posts per user | `--limit <number>` |

### Resilience & Safety Features

| Feature | Status | Description | Implementation |
|---------|--------|-------------|----------------|
| **Account Rotation** | ✅ Production | Auto-switch on rate limit errors | `InstagramProcessor` |
| **Cooldown Management** | ✅ Production | 15-minute cooldown after rate limit | `AccountCooldownManager` |
| **Quota Enforcement** | ✅ Production | 180 views/day, 6000 actions/day per account | `AccountQuotaManager` |
| **2FA Support** | ✅ Production | Interactive OTP input (3-retry) | `InstagramAccountManager` |
| **Progress Tracking** | ✅ Production | Resume from interruptions | `ProgressManager` |
| **Intelligent Routing** | ✅ Production | Route to best account per profile | `ProfileAccessTracker` |
| **Retry with Backoff** | ✅ Production | Exponential backoff (max 3 retries) | `retry_with_backoff` |
| **Emergency Break** | ✅ Production | 5-10 minute break on severe rate limit | `RateLimiter` |
| **Atomic Writes** | ✅ Production | Prevent data corruption | `safe_json_write` |
| **File Locking** | ✅ Production | Concurrent access protection | `FileLock` |

### Data Management Features

| Feature | Status | Description | Location |
|---------|--------|-------------|----------|
| **Database Migration** | ✅ Production | Migrate JSON files to database | `python main.py db-migrate` |
| **Database Reset** | ✅ Production | Clear all data (keep schema) | `python main.py db-reset` |
| **Username Management** | ✅ Production | Add/list tracked usernames | `python main.py add-username <user>` |
| **Priority Analysis** | ✅ Production | Analyze username priorities | `python main.py priority-analysis` |
| **User Network Analysis** | ✅ Production | Generate network reports | `python main.py analyze` |
| **Profile Analysis** | ✅ Production | Analyze profile metadata | `python main.py analyze-profiles` |
| **Access Statistics** | ✅ Production | View profile access stats | `python main.py access-stats` |
| **Progress Management** | ✅ Production | Show/resume/clear progress | `python main.py progress show` |

---

## Data Models

### Database Schema (11 Tables)

#### Core Data Tables

**profiles** - User profile metadata
```sql
CREATE TABLE profiles (
    username            TEXT PRIMARY KEY,
    full_name           TEXT,
    biography           TEXT,
    external_url        TEXT,
    profile_pic_url     TEXT,
    followers_count     INTEGER NOT NULL DEFAULT 0,
    following_count     INTEGER NOT NULL DEFAULT 0,
    media_count         INTEGER NOT NULL DEFAULT 0,
    is_public           INTEGER NOT NULL DEFAULT 1,
    is_verified         INTEGER NOT NULL DEFAULT 0,
    last_collected_ts   REAL NOT NULL,
    collected_by        TEXT NOT NULL,
    created_at          REAL NOT NULL DEFAULT (unixepoch()),
    updated_at          REAL NOT NULL DEFAULT (unixepoch())
)
```

**relationships** - Follower/following connections
```sql
CREATE TABLE relationships (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    source                      TEXT NOT NULL,
    target                      TEXT NOT NULL,
    type                        TEXT NOT NULL CHECK(type IN ('followers','following')),
    collected_by                TEXT NOT NULL,
    source_is_public            INTEGER NOT NULL DEFAULT 1,
    source_followed_by_collector INTEGER NOT NULL DEFAULT 0,
    collected_ts                REAL NOT NULL DEFAULT (unixepoch()),
    UNIQUE(source, target, type)
)
```

**usernames** - Tracked username list
```sql
CREATE TABLE usernames (
    username            TEXT PRIMARY KEY,
    source_account      TEXT NOT NULL,
    added_ts            REAL NOT NULL DEFAULT (unixepoch()),
    last_accessed_ts    REAL,
    metadata_json       TEXT NOT NULL DEFAULT '{}',
    created_at          REAL NOT NULL DEFAULT (unixepoch())
)
```

#### Operational Tables

**operation_progress** - Operation state tracking
```sql
CREATE TABLE operation_progress (
    operation_id        TEXT NOT NULL,
    username            TEXT NOT NULL,
    status              TEXT NOT NULL CHECK(status IN ('pending','completed','failed')),
    details_json        TEXT NOT NULL DEFAULT '{}',
    error_msg           TEXT,
    updated_at          REAL NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY (operation_id, username)
)
```

**account_cooldowns** - Account cooldown tracking
```sql
CREATE TABLE account_cooldowns (
    account_name        TEXT PRIMARY KEY,
    until_ts            REAL NOT NULL,
    reason              TEXT NOT NULL DEFAULT 'rate-limit',
    created_at          REAL NOT NULL DEFAULT (unixepoch())
)
```

**account_quotas** - Daily quota usage
```sql
CREATE TABLE account_quotas (
    account_name        TEXT PRIMARY KEY,
    quota_date          TEXT NOT NULL,
    profile_views       INTEGER NOT NULL DEFAULT 0,
    actions             INTEGER NOT NULL DEFAULT 0,
    updated_at          REAL NOT NULL DEFAULT (unixepoch())
)
```

#### Access Tracking Tables

**profile_access_attempts** - Access attempt history
```sql
CREATE TABLE profile_access_attempts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    target_username     TEXT NOT NULL,
    accessing_account   TEXT NOT NULL,
    can_access          INTEGER NOT NULL DEFAULT 0,
    is_public           INTEGER,
    is_followed         INTEGER NOT NULL DEFAULT 0,
    error_msg           TEXT,
    attempt_ts          REAL NOT NULL DEFAULT (unixepoch())
)
```

**profile_access_summary** - Aggregated access data
```sql
CREATE TABLE profile_access_summary (
    username                TEXT PRIMARY KEY,
    is_public               INTEGER,
    last_checked_ts         REAL,
    last_successful_ts      REAL,
    total_attempts          INTEGER NOT NULL DEFAULT 0,
    known_accessible_by_json TEXT NOT NULL DEFAULT '[]'
)
```

### Configuration Data Model

```python
InstagramAccount:
  name: str                    # Short identifier (e.g., "main")
  username: str               # Instagram username
  password: str               # Instagram password
  browser: str (optional)      # Browser name for cookie import
```

### Progress Data Model

```python
ProgressData:
  operation_id: str            # Unique operation identifier
  username: str               # Target username
  status: str                 # "pending" | "completed" | "failed"
  details_json: str           # JSON-encoded operation details
  error_msg: str (optional)    # Error message if failed
  updated_at: float           # Unix timestamp
```

---

## Security & Performance

### Security Architecture

#### Authentication & Authorization
- **Session Persistence**: Instaloader sessions stored in `sessions/` directory (local-only)
- **2FA Support**: Interactive OTP input with 3-retry capability
- **Browser Cookie Import**: Support for Chrome, Firefox, Edge, Safari
- **No Remote Storage**: All credentials and data stored locally only

#### Data Protection
- **Credential Isolation**: `.env` file excluded from version control via `.gitignore`
- **Session Security**: Session files treated as sensitive as passwords
- **No API Keys**: Uses Instaloader's authentication mechanism (no third-party APIs)
- **Local-Only Operation**: No data transmission to external servers

#### Anti-Detection Measures
- **Conservative Rate Limiting**: 20-40s base delay between operations
- **Periodic Pauses**: 30s pause every 12 operations during enumeration
- **Automatic Breaks**: 5-10 minute breaks after 30-50 operations
- **Account Rotation**: Automatic switching on rate-limit detection
- **Cooldown Enforcement**: 15-minute minimum cooldown per account
- **Quota Management**: 180 profile views/day, 6000 actions/day per account

### Performance Characteristics

#### Scalability Limits
- **Single Account**: 180 profile views/day (Instagram limit)
- **Multi-Account**: Linear scaling (N accounts × 180 views/day)
- **Batch Processing**: Automatic account rotation extends capacity
- **Large Networks**: Priority-based processing optimizes quota usage

#### Resource Usage
- **CPU**: Minimal (network-bound operations)
- **Memory**: <100MB typical (state persisted to database)
- **Disk I/O**: Network-bound (Instagram API calls dominate)
- **Storage**: Depends on media downloads (configurable location)

#### Rate Limiting Strategy

| Operation Type | Base Delay | Periodic Pause | Emergency Break | Cooldown |
|----------------|-----------|----------------|-----------------|----------|
| Profile Access | 20-40s | Every 12 ops (30s) | 5-10 min | 15 min |
| Media Download | 20-40s | Every 10 posts (10s) | 5-10 min | 15 min |
| Follower/Following Enum | 20-40s | Every 12 ops (30s) | 5-10 min | 15 min |
| Account Switch | 60-120s | N/A | N/A | N/A |

---

## Non-Functional Requirements

### Reliability
- **Error Handling**: Comprehensive exception categorization with recovery strategies
- **Retry Logic**: Exponential backoff (max 3 retries, 30-600s delays)
- **State Persistence**: Atomic writes with database transactions
- **Graceful Degradation**: Account switching on failures, skip on non-recoverable errors

### Availability
- **Resumption**: All operations support resume from interruption
- **Account Pool**: Multi-account support ensures at least one healthy account
- **Offline Analytics**: Analysis features work without API access
- **Progress Tracking**: Database-backed progress state survives crashes

### Maintainability
- **Modular Design**: 22 dedicated modules with clear separation of concerns
- **Extensible Architecture**: Command-based pattern for easy feature additions
- **Test Coverage**: 1015 test cases (unit + integration)
- **Comprehensive Logging**: Structured logging for debugging and audit trail

### Compatibility
- **Operating Systems**: Windows (primary), Unix/Linux, macOS
- **Python Version**: 3.7+ (tested on 3.12)
- **Database Backends**: SQLite (default), PostgreSQL (optional)
- **Dependencies**: Minimal external dependencies (4 core packages)

### Usability
- **CLI Interface**: Consistent command structure with `--help` documentation
- **Batch Files**: Windows batch menu system for non-technical users
- **Interactive Prompts**: 2FA OTP, download path selection, account selection
- **Progress Feedback**: Real-time progress tracking and summary reports

---

## Environment Configuration

### Required Environment Variables

| Variable | Type | Purpose | Example | Required |
|----------|------|---------|---------|----------|
| `DATABASE_URL` | string | Database connection string | `sqlite:///data/instagram_toolkit.db` | No (defaults to SQLite) |
| `FILTER_MAX_FOLLOWERS` | integer | Download filter threshold | `1000` | No (0 = no filter) |
| `INSTA_ACCOUNT_{N}_NAME` | string | Short identifier | `"main"` | Yes |
| `INSTA_ACCOUNT_{N}_USER` | string | Instagram username | `"username"` | Yes |
| `INSTA_ACCOUNT_{N}_PASS` | string | Instagram password | `"password"` | Yes |
| `INSTA_ACCOUNT_{N}_BROWSER` | string | Browser for cookie import | `"Chrome"` | No |
| `INSTA_ACCOUNT_{N}_PROXY` | string | Per-account SOCKS5 proxy | `"socks5://..."` | No |
| `PROXY_URL` | string | Global proxy for all accounts | `"socks5://..."` | No |

### Configuration File Format

**.env file structure:**
```env
# Database Configuration
DATABASE_URL=sqlite:///data/instagram_toolkit.db

# Download Filters
FILTER_MAX_FOLLOWERS=1000

# Account 1
INSTA_ACCOUNT_1_NAME=main
INSTA_ACCOUNT_1_USER=instagram_username
INSTA_ACCOUNT_1_PASS=instagram_password

# Account 2 (optional - for rotation)
INSTA_ACCOUNT_2_NAME=alt
INSTA_ACCOUNT_2_USER=other_username
INSTA_ACCOUNT_2_PASS=other_password
```

**Security Note**: Never commit `.env` file to version control. Use `.env.example` as template.

---

## Deployment & Runtime Requirements

### System Requirements

| Requirement | Minimum | Recommended |
|-------------|---------|-------------|
| **Python** | 3.7 | 3.12 |
| **RAM** | 256 MB | 512 MB |
| **Disk Space** | 100 MB (toolkit) | Depends on media downloads |
| **Internet** | Stable connection | High-speed recommended |
| **OS** | Windows 10+ | Windows 11 |

### Runtime Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `instaloader` | 4.15 | Instagram API client |
| `requests` | 2.33.1 | HTTP client |
| `python-dotenv` | 1.2.2 | Environment variable loading |
| `pytest` | 9.0.3 | Testing framework |
| `hypothesis` | 6.152.2 | Property-based testing |

### Directory Structure

```
instagramtoolkit/
├── data/                           # Data storage
│   ├── instagram_toolkit.db        # SQLite database (primary data store)
│   └── usernames.txt               # Legacy username list (optional)
├── downloads/                      # Media downloads (configurable)
├── sessions/                       # Instagram session files
│   ├── .gitkeep                    # Keep directory in git
│   └── {username}                  # Session file per account
├── archived_logs/                  # Archived progress and logs
├── src/                           # Library modules (32 files)
│   ├── commands/                   # CLI command implementations
│   ├── db/                        # Database layer
│   │   ├── repositories/          # Data access layer
│   │   ├── backends.py            # SQLite/PostgreSQL backends
│   │   ├── manager.py             # DatabaseManager
│   │   ├── migrate_json.py        # JSON-to-DB migration
│   │   └── schema.py              # Database schema DDL
│   └── *.py                       # Core utilities and services
├── tests/                         # Test suite (1015 tests)
├── web/                           # Web dashboard (local development only)
├── scripts/                       # Utility scripts
├── .env                           # Credentials (gitignored)
├── .env.example                   # Template for .env
├── requirements.txt               # Python dependencies
├── pytest.ini                     # Pytest configuration
├── main.py                        # CLI entry point
├── setup.bat                      # Windows setup script
├── start_toolkit.bat              # Windows menu interface
└── run_tests.bat                  # Test runner
```

---

## Known Issues & Limitations

### Platform Limitations
- **Instagram API Rate Limits**: Strict enforcement prevents unlimited scraping (180 views/day per account)
- **Public Profiles Only**: Cannot access private profiles (respects Instagram's privacy model)
- **CAPTCHA/Challenge**: May require manual intervention for some operations
- **Account Restrictions**: Aggressive usage can result in temporary account restrictions

### Current Limitations
- **Download Speed**: Paced by Instagram rate limits (not system-limited)
- **2FA Required**: Some accounts require interactive OTP input per session
- **Session Expiry**: Sessions may expire after ~90 days of inactivity
- **Network Dependency**: Requires stable internet connection for all operations

### Data Integrity Considerations
- **Duplicate Media**: Instaloader may re-download previously downloaded files
- **Incomplete Following List**: Instagram API may not return complete following list for some profiles
- **Profile Deletion**: If target profile is deleted, collection fails for that target
- **Rate Limit Variability**: Instagram's rate limits may vary by account age, activity, and other factors

### Web Dashboard Limitations
- **No Authentication**: Dashboard accessible to anyone on the network (local development only)
- **CORS Configuration**: Allows all origins (`*`) - not suitable for production
- **Rendering Limits**: Large datasets (>1000 nodes) may cause browser performance issues
- **Network Binding**: Server binds to all interfaces (`0.0.0.0`) - use on trusted networks only

---

## Success Criteria

### Functional Success
- ✅ **Complete Feature Implementation**: All 30+ documented features functional
- ✅ **Account Rotation**: Automatic switching on rate limits verified
- ✅ **Progress Resumption**: Operations resume correctly after interruption
- ✅ **Quota Enforcement**: Daily quotas prevent account bans
- ✅ **2FA Support**: Interactive OTP authentication functional
- ✅ **Data Integrity**: Database transactions prevent corruption

### Quality Success
- ✅ **Test Coverage**: 1015 test cases covering critical paths
- ✅ **Error Handling**: Comprehensive recovery strategies in place
- ✅ **Documentation**: Code documented with docstrings and inline comments
- ✅ **CLI Interface**: Consistent and user-friendly command structure
- ✅ **Batch Files**: Windows menu interface functional

### Performance Success
- ✅ **Rate Limit Adherence**: Quotas prevent API restrictions
- ✅ **Scalability**: Handles large-scale batch operations with resumption
- ✅ **Resource Efficiency**: Minimal memory footprint (<100MB typical)
- ✅ **Concurrent Safety**: Database transactions prevent data corruption

---

## Future Enhancements

### Planned Features
- **Command Module Integration**: Migrate from monolithic dispatcher to command-based architecture
- **PostgreSQL Support**: Full testing and optimization for PostgreSQL backend
- **Web Dashboard Authentication**: Add authentication and HTTPS support
- **Advanced Analytics**: Machine learning-based network analysis
- **Export Formats**: Additional export formats (GraphML, GEXF for network analysis tools)

### Under Consideration
- **Distributed Processing**: Multi-machine coordination for large-scale operations
- **Real-Time Monitoring**: Live dashboard for operation monitoring
- **Automated Reporting**: Scheduled reports and alerts
- **API Server**: REST API for programmatic access

---

**Document Version:** 3.0  
**Last Updated:** 2026-04-26  
**Status:** Production Ready  
**Maintained By:** System Architecture Team

All features documented in this PRD have been verified to exist in the codebase and are functional as of the last update date.
