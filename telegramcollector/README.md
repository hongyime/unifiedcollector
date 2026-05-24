# Telegram Media Intelligence Platform

A containerised, multi-service platform that collects messages and media from Telegram accounts, performs automated face recognition, and organises identified individuals into dedicated forum topics in a designated Telegram group. Secondary pipelines track user social graphs, discover Telegram group links, and provide bulk media sending.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Architecture Overview](#architecture-overview)
3. [Initial Setup](#initial-setup)
4. [Environment Configuration](#environment-configuration)
5. [Starting the System](#starting-the-system)
6. [Service Startup Behaviour](#service-startup-behaviour)
7. [Registering Telegram Accounts](#registering-telegram-accounts)
8. [Dashboards](#dashboards)
9. [Bot Commands](#bot-commands)
10. [Testing](#testing)
11. [Maintenance](#maintenance)
12. [Troubleshooting](#troubleshooting)

---

## Prerequisites

| Requirement | Minimum Version | Notes |
|---|---|---|
| Docker Desktop | 24.x | Must be running before any command |
| Docker Compose | v2.x | Included with Docker Desktop |
| Python | 3.11+ | Required only for local test execution |
| Telegram API credentials | — | Obtain from [my.telegram.org](https://my.telegram.org) |
| Telegram Bot token | — | Create via [@BotFather](https://t.me/botfather) |
| Telegram group with Topics enabled | — | The "Hub Group" where identities are published |

> **GPU acceleration (optional):** The face recognition service defaults to CPU. To enable CUDA, set `USE_GPU=true` in `.env` and ensure the Docker host has NVIDIA drivers and the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) installed.

---

## Architecture Overview

The system runs as 14 Docker containers:

```
Infrastructure:  postgres (pgvector/pg16)  |  redis (7-alpine)
                          │                         │
Processing:    collector  login_bot  face_recognition  user_intelligence  link_discovery  bulk_sender
                          │
Dashboards:    index(8500)  collector(8501)  face(8502)  user_intel(8503)  link_disc(8504)  bulk_sender(8505)
```

Data flows from `collector` → `collector.raw_messages` → downstream services via a shared cursor table (`collector.service_cursors`). Each downstream service reads its own cursor, processes a batch, and advances the cursor atomically.

---

## Initial Setup

### Step 1 — Clone the repository

```bash
git clone <repository-url>
cd <repository-directory>
```

### Step 2 — Obtain Telegram API credentials

1. Go to [https://my.telegram.org](https://my.telegram.org) and log in.
2. Click **API development tools**.
3. Create an application (any name).
4. Copy `api_id` (integer) and `api_hash` (string).

### Step 3 — Create a Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/botfather).
2. Send `/newbot` and follow the prompts.
3. Copy the bot token (format: `123456789:ABCdef...`).
4. Add the bot as **admin** to your Hub Group.

### Step 4 — Create the Hub Group

1. Create a new Telegram group.
2. Go to **Group Settings → Topics** and enable Topics.
3. Add your bot as admin.
4. Forward any message from the group to [@userinfobot](https://t.me/userinfobot) to obtain the numeric group ID (starts with `-100`).

### Step 5 — Configure environment

```bash
cp .env.template .env
```

Edit `.env` and set at minimum:

```env
TG_API_ID=<your_api_id>
TG_API_HASH=<your_api_hash>
BOT_TOKEN=<your_bot_token>
HUB_GROUP_ID=<your_hub_group_id>
DB_PASSWORD=<choose_a_secure_password>

# IMPORTANT: point this at an external HDD — media storage grows very large
MEDIA_STORE_PATH=/mnt/external/telegramcollector/media
```

See [Environment Configuration](#environment-configuration) for the full variable reference.

### Step 6 — Build and start

```bash
docker-compose up -d --build
```

Wait approximately 60–90 seconds for all services to initialise. Verify with:

```bash
docker-compose ps
```

All containers should show `Up`. The `postgres` and `redis` containers will show `(healthy)`.

---

## Environment Configuration

All variables are read from `.env`. Variables with no default are **required** and will cause a startup failure if absent.

### Telegram Credentials

| Variable | Required | Default | Description |
|---|---|---|---|
| `TG_API_ID` | **Yes** | — | Integer API ID from my.telegram.org |
| `TG_API_HASH` | **Yes** | — | API hash string from my.telegram.org |
| `BOT_TOKEN` | No | `""` | Single bot token; fallback when `BOT_TOKENS` is empty |
| `BOT_TOKENS` | No | `""` | Multi-bot rotation: `Name1:token1;Name2:token2` |
| `FACE_BOT_TOKENS` | No | `""` | Separate bot pool for face recognition publishing |
| `HUB_GROUP_ID` | **Yes** | — | Numeric ID (e.g. `-1001234567890`) or `@username` |

### Database

| Variable | Required | Default | Description |
|---|---|---|---|
| `DB_PASSWORD` | **Yes** | — | PostgreSQL password |
| `DB_NAME` | No | `telegramcollector` | Database name |
| `DB_HOST` | No | `postgres` | Hostname (use `postgres` inside Docker) |
| `DB_PORT` | No | `5432` | Port |
| `DB_USER` | No | `postgres` | Superuser name |

### Redis

| Variable | Required | Default | Description |
|---|---|---|---|
| `REDIS_HOST` | No | `redis` | Hostname (use `redis` inside Docker) |
| `REDIS_PORT` | No | `6379` | Port |
| `REDIS_PASSWORD` | No | — | Password (leave blank if none) |

### Processing

| Variable | Default | Description |
|---|---|---|
| `NUM_WORKERS` | `6` | Parallel processing workers |
| `USE_GPU` | `true` | Enable CUDA for face recognition (requires NVIDIA runtime) |
| `SIMILARITY_THRESHOLD` | `0.55` | Face matching cosine similarity threshold (0.0–1.0) |
| `MIN_QUALITY_THRESHOLD` | `0.67` | Minimum face detection confidence score |
| `MAX_MEDIA_SIZE_MB` | `50` | Skip files larger than this |
| `RUN_MODE` | `both` | `backfill`, `realtime`, or `both` |

### Service Master Switches

| Variable | Default | Description |
|---|---|---|
| `FACE_PROCESSING_ENABLED` | `false` | Start face recognition paused; set `true` to activate |
| `USER_INTEL_PROCESSING_ENABLED` | `false` | Start user intelligence paused |
| `LINK_DISCOVERY_PROCESSING_ENABLED` | `true` | Link discovery runs by default |

### Resilience

| Variable | Default | Description |
|---|---|---|
| `SIGTERM_DRAIN_TIMEOUT` | `30` | Seconds to drain in-flight tasks on shutdown |
| `SESSION_ROTATION_ENABLED` | `true` | Auto-rotate to next account on session failure |
| `ACCOUNT_SCHEDULE_ENABLED` | `false` | Time-window scheduling for shared accounts |
| `ACCOUNT_ACTIVE_START` | `00:00` | UTC activation time (HH:MM) |
| `ACCOUNT_ACTIVE_END` | `24:00` | UTC deactivation time (HH:MM) |

### Storage Paths

> ⚠️ **External storage is strongly recommended.** The collector archives all media, documents, and files from every accessible chat on every connected account. This can easily reach hundreds of gigabytes or more depending on the number of accounts and chats. Point `MEDIA_STORE_PATH` at an external HDD or NAS before you start. Running on a system drive will fill it.

| Variable | Default | Description |
|---|---|---|
| `MEDIA_STORE_PATH` | `./media` | **Change this to your external HDD path** — e.g. `/mnt/external/telegramcollector/media` |
| `SESSIONS_BASE_PATH` | `./sessions` | Host path for Telegram session files (small, can stay local) |

---

## Starting the System

```bash
# Start all services in the background
docker-compose up -d

# View logs for all services
docker-compose logs -f

# View logs for a specific service
docker-compose logs -f collector
docker-compose logs -f face_recognition

# Stop all services
docker-compose down

# Stop and remove all data volumes (destructive)
docker-compose down -v
```

---

## Service Startup Behaviour

Not all services do useful work immediately after `docker-compose up`. Understanding which services start automatically versus which require activation will save you confusion on first run.

### Services that start and run immediately

**Login Bot** — starts listening for `/startcollector` as soon as the container is up. This is the first thing you interact with.

Each bot handles one registration session at a time. If you configure multiple bots via `BOT_TOKENS`, they operate in parallel — three bots means three users can register simultaneously, each with their own dedicated bot for the full phone → OTP → 2FA flow. The system automatically routes users to an available bot and redirects to another if one is rate-limited by Telegram.

**Collector** — starts all internal workers (backfill, realtime, story scanner, admin log poller, group manager) immediately, but does nothing useful until at least one Telegram account is registered. You will see `Loaded 0 active account(s)` in the logs until then.

The collector archives **everything** accessible from every connected account — all messages (text, media, documents, polls, reactions), profile photos, group membership changes, admin log events, and stories. This includes all private chats, groups, and channels the account can see. All raw content is written to `collector.raw_messages` and related tables, with media files saved to disk at `MEDIA_STORE_PATH`.

**Link Discovery** — starts processing `collector.raw_messages` immediately via its cursor. It will begin extracting Telegram links as soon as the collector writes messages.

**Bulk Sender** — starts polling for jobs every 5 seconds. Sits idle until you create a job in the database.

**All dashboards and the index page** — start immediately and are accessible as soon as the containers are up.

### Services that start paused — require explicit activation

**Face Recognition** — boots fully (downloads the InsightFace `buffalo_l` model on first run, ~280 MB), connects to the database, then sits in a poll loop doing nothing until enabled. This is intentional — you want data flowing before you start processing it.

To activate without restarting:

```bash
docker-compose exec redis redis-cli SET config:FACE_PROCESSING_ENABLED true
```

To activate permanently (survives restarts), set in `.env` and restart:

```bash
# in .env
FACE_PROCESSING_ENABLED=true

docker-compose restart face_recognition
```

> Also requires `FACE_BOT_TOKENS` to be set. Without it the service starts but cannot publish anything to the Hub Group — you will see the warning `publisher will not be able to upload media`.

**User Intelligence** — starts automatically and processes `collector.user_sightings` as soon as data arrives from the collector. No activation needed.

### Summary

| Service | Starts automatically | Processes immediately | What it needs to do work |
|---|---|---|---|
| `collector` | Yes | No | At least one registered Telegram account |
| `login_bot` | Yes | Yes | `BOT_TOKEN` or `BOT_TOKENS` in `.env` |
| `face_recognition` | Yes | No | `FACE_PROCESSING_ENABLED=true` + `FACE_BOT_TOKENS` |
| `user_intelligence` | Yes | Yes | Data in `collector.user_sightings` |
| `link_discovery` | Yes | Yes | Messages in `collector.raw_messages` |
| `bulk_sender` | Yes | No | Jobs created in `bulk_sender.send_jobs` |
| All dashboards | Yes | Yes | Nothing — read-only views |

### Recommended startup sequence

1. `docker-compose up -d --build` — everything starts
2. Register at least one Telegram account via the login bot
3. Watch the collector begin scanning (`docker-compose logs -f collector`)
4. Once messages are flowing, enable face recognition
5. Enable user intelligence when you want social graph data

### Optional features controlled by `.env`

| Feature | Variable | Default | Notes |
|---|---|---|---|
| Account time-window scheduling | `ACCOUNT_SCHEDULE_ENABLED` | `false` | Connects/disconnects accounts on a UTC schedule — useful when sharing accounts with another project |
| Story scanning | `STORY_SCAN_ENABLED` | `true` | Set `false` to disable entirely |
| Link metadata resolution | `LINK_DISCOVERY_RESOLVE_METADATA` | `false` | Makes Telegram API calls to resolve chat titles and member counts for discovered links |
| GPU for face recognition | `USE_GPU` | `true` | Requires NVIDIA runtime; falls back to CPU automatically if unavailable |
| Social graph construction | `USER_INTEL_NETWORK_ENABLED` | `true` | Set `false` to skip building user connection graphs (faster if you only need membership tracking) |

---

## Registering Telegram Accounts

Accounts are registered through the login bot. The bot handles the full Telegram authentication flow and stores session files for the collector.

1. Find your bot in Telegram (the one created in Step 3).
2. Send `/startcollector`.
3. Enter your phone number in E.164 format (e.g. `+12345678900`).
4. Enter the verification code sent to your phone.
5. If 2FA is enabled, enter your password (it is deleted immediately).

The bot auto-deletes all messages within 30 seconds. On successful registration, the session file is distributed to all service directories and backfill jobs are created for all accessible dialogs.

**Multiple accounts:** Repeat the process for each account. All registered accounts run in parallel.

---

## Dashboards

Dashboard ports are dynamically allocated to avoid conflicts. Find the actual host port with:

```bash
docker-compose ps
```

| Service | Internal Port | Purpose |
|---|---|---|
| Index | 8500 | Landing page listing all dashboard URLs with live status |
| Collector | 8501 | Scan progress, account status, queue metrics |
| Face Recognition | 8502 | Identity gallery, face search, merge/split/rename |
| User Intelligence | 8503 | User profiles, membership history, social graph |
| Link Discovery | 8504 | Discovered links, queue rules management |
| Bulk Sender | 8505 | Job management, send progress |

Open the index page first — it shows the live-allocated URLs for all dashboards:

```
http://localhost:<index_host_port>
```

---

## Bot Commands

The following commands are available on all configured bots (send in any private chat with the bot):

| Command | Description |
|---|---|
| `/status` | System status: active accounts, queue depth, worker count |
| `/pause` | Pause all scanning and processing |
| `/resume` | Resume scanning and processing |
| `/restart` | Graceful restart of the collector |
| `/help` or `/commands` | List available commands |

---

## Testing

### Run the full test suite

```bash
# Install test dependencies (if running locally, not in Docker)
pip install pytest pytest-asyncio hypothesis

# Run all tests
python -m pytest tests/ -v

# Run only the audit bugfix tests
python -m pytest tests/test_audit_bugfix_exploration.py tests/test_audit_bugfix_preservation.py -v
```

### Test structure

| File | Purpose |
|---|---|
| `tests/test_audit_bugfix_exploration.py` | Bug condition tests — verify each of the 8 critical bugs is fixed |
| `tests/test_audit_bugfix_preservation.py` | Preservation tests — verify no regressions on non-buggy code paths |
| `tests/test_bot_pool.py` | Bot pool rotation and lockout logic |
| `tests/test_face_processor.py` | InsightFace detection and embedding extraction |
| `tests/test_hub_notifier.py` | Notification batching and rate limiting |
| `tests/test_story_scanner.py` | Story scanning and priority queue |
| `tests/test_resilience.py` | Circuit breaker state machine |
| `tests/test_integration.py` | End-to-end pipeline integration |

### Run a single test file

```bash
python -m pytest tests/test_audit_bugfix_exploration.py -v --tb=short
```

---

## Maintenance

### Enable face recognition processing

Face recognition starts paused. Enable it via Redis (takes effect immediately, no restart required):

```bash
docker-compose exec redis redis-cli SET config:FACE_PROCESSING_ENABLED true
```

Or set in `.env` and restart:

```bash
docker-compose restart face_recognition
```

### Reset a service cursor (replay from beginning)

```bash
docker-compose exec postgres psql -U postgres -d telegramcollector \
  -c "UPDATE collector.service_cursors SET last_message_id = 0 WHERE service_name = 'face_recognition';"
```

### Check queue depth

```bash
docker-compose exec redis redis-cli LLEN processing_queue:tasks
```

### View dead letter queue

```bash
docker-compose exec redis redis-cli HLEN processing_queue:dead_letter
```

### Update the system

```bash
git pull
docker-compose up -d --build
```

### Backup the database

```bash
docker-compose exec postgres pg_dump -U postgres telegramcollector > backup_$(date +%Y%m%d).sql
```

---

## Troubleshooting

### A service is restarting repeatedly

```bash
docker-compose logs --tail=50 <service_name>
```

Common causes:
- Missing or incorrect `.env` values (check `DB_PASSWORD`, `TG_API_ID`, `BOT_TOKEN`).
- Database not yet healthy — services wait for the `postgres` healthcheck but may timeout on first boot.
- `FACE_BOT_TOKENS` not set — the face recognition service will fail to initialise the bot pool.

### Bot not responding to `/startcollector`

```bash
docker-compose logs --tail=30 login_bot
```

- Verify `BOT_TOKEN` is correct.
- Ensure the bot has not been blocked by the user.

### Face recognition not processing

1. Check `FACE_PROCESSING_ENABLED` is `true`.
2. Verify `FACE_BOT_TOKENS` is set and the bots are admins in the Hub Group.
3. Check the cursor: `SELECT * FROM collector.service_cursors WHERE service_name = 'face_recognition';`
4. Check the DLQ: `docker-compose exec redis redis-cli HGETALL face_recognition:dlq`

### WSL2 clock drift (Windows hosts)

If bots time out on connection with the message `"very new message"`:

```powershell
# Run on the Windows host
wsl -d docker-desktop -e sh -c "hwclock -s"
```

Then restart the affected containers.

### Database connection failure on fresh deployment

Ensure `DB_NAME=telegramcollector` in `.env` (or leave it unset — the default is `telegramcollector`). The old default `face_archiver` is no longer used.

### Viewing all container statuses

```bash
docker-compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
```
