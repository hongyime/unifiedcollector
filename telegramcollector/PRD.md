# Product Requirements Document

**System:** Telegram Media Intelligence Platform  
**Version:** Current (post-audit, all critical bugs resolved)  
**Classification:** Technical Specification  

---

## 1. Executive Summary

This system is a multi-service platform that continuously collects messages and media from Telegram accounts, performs automated face recognition on collected media, and organises identified individuals into dedicated forum topics within a designated Telegram group. Secondary pipelines extract social graph data, discover Telegram group links, and provide a bulk media sending capability. All services are containerised and communicate through a shared PostgreSQL database using a cursor-based pipeline pattern.

---

## 2. System Architecture

### 2.1 Service Topology

The system comprises 14 Docker containers across three tiers:

**Infrastructure Tier**
| Container | Image | Purpose |
|---|---|---|
| `postgres` | `pgvector/pgvector:pg16` | Primary data store with vector extension |
| `redis` | `redis:7-alpine` | Processing queue, dynamic config, DLQ |

**Processing Tier**
| Container | Entrypoint | Memory Limit |
|---|---|---|
| `collector` | `services.collector.main` | 2 GB |
| `login_bot` | `services.login_bot.main` | 256 MB |
| `face_recognition` | `services.face_recognition.main` | 4 GB |
| `user_intelligence` | `services.user_intelligence.main` | 1 GB |
| `link_discovery` | `services.link_discovery.main` | 512 MB |
| `bulk_sender` | `services.bulk_sender.main` | 512 MB |

**Dashboard Tier**
| Container | Port | Framework |
|---|---|---|
| `index` | 8500 | Python stdlib `http.server` |
| `collector_dashboard` | 8501 | Streamlit |
| `face_dashboard` | 8502 | Streamlit |
| `user_intel_dashboard` | 8503 | Streamlit |
| `link_discovery_dashboard` | 8504 | Streamlit |
| `bulk_sender_dashboard` | 8505 | Streamlit |

### 2.2 Inter-Service Data Flow

```
Telegram API
     │
     ▼
[login_bot] ──── registers accounts ────► collector.telegram_accounts
                                                    │
                                                    ▼
                                           [collector]
                                           ├── BackfillWorker
                                           ├── RealtimeWorker
                                           ├── StoryScanner
                                           ├── AdminLogPoller
                                           └── GroupManager
                                                    │
                                    writes to collector.raw_messages
                                                    │
                          ┌─────────────────────────┼──────────────────────┐
                          ▼                         ▼                      ▼
               [face_recognition]        [user_intelligence]    [link_discovery]
               reads raw_messages        reads user_sightings   reads raw_messages
               via service_cursors       via service_cursors    via service_cursors
                          │
                          ▼
               face_recognition.telegram_topics
               face_recognition.face_embeddings
                          │
                          ▼
                   [Hub Telegram Group]
                   (forum topics per identity)
```

### 2.3 Cursor-Based Pipeline

All downstream services consume `collector.raw_messages` or `collector.user_sightings` via a persistent cursor stored in `collector.service_cursors`. Each service:

1. Reads its `last_message_id` from `collector.service_cursors` on startup.
2. Fetches a batch of rows with `id > last_message_id ORDER BY id ASC LIMIT batch_size`.
3. Processes the batch.
4. UPSERTs the new `last_message_id` back to `collector.service_cursors`.

This pattern guarantees at-least-once delivery and allows independent replay by resetting the cursor.

### 2.4 Shared Infrastructure Layer (`shared/`)

| Module | Responsibility |
|---|---|
| `config.py` | Pydantic-settings configuration; Redis-backed dynamic overrides |
| `database.py` | Async connection pool (`psycopg` + `psycopg-pool`); circuit breaker; health monitor |
| `bot_pool.py` | Multi-bot round-robin rotation; FloodWait lockout; background health monitor |
| `processing_queue.py` | Redis-primary queue with in-memory fallback; backpressure (NORMAL/WARNING/CRITICAL); autoscaler; SIGTERM drain |
| `hub_notifier.py` | Rate-limited, batched Telegram notifications; SQLite offline cache with replay |
| `topic_manager.py` | Forum topic creation/rename/repair via bot client |
| `media_uploader.py` | Media upload to forum topics with deduplication and retry |
| `media_downloader.py` | In-memory media download with size limits and concurrency control |
| `resilience.py` | Circuit breaker (CLOSED/OPEN/HALF_OPEN); retry-with-jitter decorator; token-bucket rate limiter |
| `dlq.py` | Dead letter queue with error classification (TRANSIENT/PERMANENT/RESOURCE) and exponential backoff retry |
| `observability.py` | Prometheus metrics; structured JSON logging; trace ID context |

---

## 3. Database Schema

### 3.1 Schemas and Ownership

| Schema | Owner Role | Tables |
|---|---|---|
| `collector` | `collector_user` | 19 tables |
| `face_recognition` | `face_recog_user` | 4 tables |
| `user_intelligence` | `user_intel_user` | 3 tables |
| `link_discovery` | `link_disc_user` | 2 tables |
| `bulk_sender` | `bulk_sender_user` | 2 tables |

A read-only `dashboard_user` role has `SELECT` on all schemas.

### 3.2 Key Tables

**`collector.raw_messages`** — Central message store consumed by all downstream services.

| Column | Type | Notes |
|---|---|---|
| `id` | BIGSERIAL PK | Cursor key for downstream services |
| `chat_id` | BIGINT | Source chat |
| `message_id` | BIGINT | Telegram message ID |
| `message_type` | VARCHAR(30) | `photo`, `video`, `circle_video`, `text`, `service`, etc. |
| `has_media` | BOOLEAN | Indexed partial index for media queries |
| `media_path` | TEXT | Filesystem path to downloaded file |
| `file_unique_id` | VARCHAR(255) | Telegram deduplication key |
| `payload` | JSONB NOT NULL | Full Telegram message object |

**`face_recognition.face_embeddings`** — 512-dimensional InsightFace embeddings.

| Column | Type | Notes |
|---|---|---|
| `embedding` | `vector(512)` | IVFFlat index with `lists=1000` |
| `topic_id` | INTEGER FK | References `telegram_topics.id` |
| `quality_score` | REAL | InsightFace `det_score` |
| `is_representative` | BOOLEAN | Exemplar flag |

**`collector.service_cursors`** — Shared cursor table.

| Column | Type | Notes |
|---|---|---|
| `service_name` | VARCHAR(50) PK | `face_recognition`, `user_intelligence`, `link_discovery`, `bulk_sender` |
| `last_message_id` | BIGINT | Monotonically increasing |

### 3.3 Extensions and Sequences

- `CREATE EXTENSION vector` — pgvector for cosine similarity search.
- `topic_reservation_seq` — PostgreSQL sequence used by `TopicManager` for atomic topic ID reservation.

---

## 4. Feature Matrix

### 4.1 Collector Service

| Feature | Status | Module |
|---|---|---|
| Backfill scanning (historical messages) | Implemented | `backfill_worker.py` |
| Realtime event streaming | Implemented | `realtime_worker.py` |
| Story scanning (24h expiry, priority queue) | Implemented | `story_scanner.py` |
| Admin log polling | Implemented | `admin_log_poller.py` |
| Group join queue management | Implemented | `group_manager.py` |
| Multi-account support | Implemented | `account_manager.py` |
| Time-window account scheduling | Implemented | `scheduler.py` |
| Clock drift monitoring | Implemented | `clock_monitor.py` |
| GitHub update checker (signal file only) | Partial stub | `update_checker.py` |
| Bot commands (`/status`, `/pause`, `/resume`, `/restart`, `/help`) | Implemented | `bot_commands.py` |
| Media download and storage | Implemented | `media_store.py` |
| Rate limiting (token bucket, 30 req/s) | Implemented | `rate_limiter.py` |

### 4.2 Login Bot Service

| Feature | Status | Notes |
|---|---|---|
| Phone number registration via Telegram bot | Implemented | `/startcollector` command |
| OTP verification | Implemented | 5-digit code handling |
| 2FA password handling | Implemented | Immediate message deletion |
| Rate limiting (5 attempts / 5 min) | Implemented | Rolling window |
| Auto-delete all messages (30s) | Implemented | Background loop |
| Multi-bot rotation on FloodWait | Implemented | `active_login_bots` registry |
| Session file distribution to all services | Implemented | `session_router.py` |
| Backfill job creation on registration | Implemented | `create_backfill_jobs()` |

### 4.3 Face Recognition Service

| Feature | Status | Notes |
|---|---|---|
| InsightFace `buffalo_l` model (512-dim embeddings) | Implemented | CPU and GPU (CUDA) |
| GPU → CPU fallback on init failure | Implemented | `processor.py` |
| Quality filter (`det_score >= threshold`) | Implemented | Dynamic via Redis |
| Size filter (min 40×40 px) | Implemented | `processor.py` |
| Video frame extraction (adaptive, up to N frames) | Implemented | `processor.py` |
| Circle video extraction (FPS-based) | Implemented | `processor.py` |
| pgvector cosine similarity search (IVFFlat) | Implemented | `matcher.py` |
| Advisory lock for race-safe identity creation | Implemented | `pg_advisory_xact_lock` |
| Forum topic creation via bot pool | Implemented | `publisher.py` |
| Upload deduplication | Implemented | `face_recognition.uploaded_media` |
| Identity merge (atomic transaction) | Implemented | `corrections.py` |
| Identity split | Implemented | `corrections.py` |
| Identity rename | Implemented | `corrections.py` |
| DLQ for failed messages | Implemented | Redis `face_recognition:dlq` |

### 4.4 User Intelligence Service

| Feature | Status | Notes |
|---|---|---|
| User profile change tracking | Implemented | `change_tracker.py` |
| Chat membership tracking | Implemented | `membership_tracker.py` |
| Social graph construction (shared-chat connections) | Implemented | `network_builder.py` (toggleable) |
| Cursor-based consumption of `user_sightings` | Implemented | `main.py` |

### 4.5 Link Discovery Service

| Feature | Status | Notes |
|---|---|---|
| Telegram link extraction from message text | Implemented | `extractor.py` |
| Bot link filtering | Implemented | `extractor.py` |
| Optional metadata resolution via Telegram API | Implemented | `resolver.py` (disabled by default) |
| Configurable queue rules (language, keyword, member count filters) | Implemented | `queue_rules.py` |
| Auto-queue matching links to `group_join_queue` | Implemented | `main.py` |

### 4.6 Bulk Sender Service

| Feature | Status | Notes |
|---|---|---|
| Job-based file sending to target chats | Implemented | `sender.py` |
| Send delay enforcement (min 1.0s) | Implemented | Clamped at runtime |
| File deduplication per job (SHA-64 hash) | Implemented | `bulk_sender.sent_items` |
| Orphaned job recovery on startup | Implemented | `job_manager.py` |
| Bot token or user session sending | Implemented | Configurable |
| Max retry per file | Implemented | `BULK_SENDER_MAX_RETRIES` |

### 4.7 Shared Infrastructure

| Feature | Status | Notes |
|---|---|---|
| Multi-bot pool with round-robin rotation | Implemented | `bot_pool.py` |
| FloodWait lockout with timed auto-unlock | Implemented | `bot_pool.py` |
| Circuit breaker (database, Telegram) | Implemented | `resilience.py` |
| Redis-backed processing queue with backpressure | Implemented | `processing_queue.py` |
| In-memory queue fallback when Redis unavailable | Implemented | `processing_queue.py` |
| Worker autoscaler | Implemented | `processing_queue.py` |
| SIGTERM graceful drain with task re-queue | Implemented | `processing_queue.py` |
| Dead letter queue with error classification | Implemented | `dlq.py` |
| Prometheus metrics (queue depth, latency, errors) | Implemented | `observability.py` (port 8000) |
| Structured JSON logging with trace IDs | Implemented | `observability.py` |
| Dynamic settings via Redis (`config:KEY`) | Implemented | `config.py` |
| Hub Group notification batching with SQLite cache | Implemented | `hub_notifier.py` |

---

## 5. Environment Variables

All variables are loaded from `.env` via `pydantic-settings`. Variables marked **Required** have no default and will cause a startup failure if absent.

### 5.1 Telegram Credentials

| Variable | Type | Required | Default | Description |
|---|---|---|---|---|
| `TG_API_ID` | int | **Yes** | — | Telegram application API ID from my.telegram.org |
| `TG_API_HASH` | str | **Yes** | — | Telegram application API hash |
| `BOT_TOKEN` | str | No | `""` | Single bot token; used as fallback if `BOT_TOKENS` is empty |
| `BOT_TOKENS` | str | No | `""` | Semicolon-separated `Name:token` pairs for multi-bot rotation |
| `FACE_BOT_TOKENS` | str | No | `""` | Separate bot pool for face recognition publishing |
| `HUB_GROUP_ID` | str\|int | **Yes** | — | Numeric ID or `@username` of the Hub Telegram group |

### 5.2 Database

| Variable | Type | Required | Default | Description |
|---|---|---|---|---|
| `DB_HOST` | str | No | `postgres` | PostgreSQL hostname |
| `DB_PORT` | int | No | `5432` | PostgreSQL port |
| `DB_NAME` | str | No | `telegramcollector` | Database name |
| `DB_USER` | str | No | `postgres` | Database superuser |
| `DB_PASSWORD` | str | **Yes** | — | Database password |

### 5.3 Redis

| Variable | Type | Required | Default | Description |
|---|---|---|---|---|
| `REDIS_HOST` | str | No | `redis` | Redis hostname |
| `REDIS_PORT` | int | No | `6379` | Redis port |
| `REDIS_DB` | int | No | `0` | Redis database index |
| `REDIS_PASSWORD` | str | No | `None` | Redis password (optional) |

### 5.4 Processing

| Variable | Type | Default | Description |
|---|---|---|---|
| `NUM_WORKERS` | int | `6` | Worker coroutines in the processing queue |
| `QUEUE_MAX_SIZE` | int | `4000` | High-watermark for backpressure CRITICAL state |
| `WORKER_TASK_TIMEOUT` | int | `300` | Seconds before a stuck worker is cancelled |
| `MAX_MEDIA_SIZE_MB` | int | `50` | Maximum media file size to download |
| `USE_GPU` | bool | `true` | Enable CUDA for InsightFace |
| `SIMILARITY_THRESHOLD` | float | `0.55` | Cosine similarity threshold for identity matching |
| `MIN_QUALITY_THRESHOLD` | float | `0.67` | Minimum InsightFace `det_score` |
| `RUN_MODE` | str | `both` | `backfill`, `realtime`, or `both` |

### 5.5 Face Recognition Service

| Variable | Type | Default | Description |
|---|---|---|---|
| `FACE_PROCESSING_ENABLED` | bool | `false` | Master switch; service starts paused when false |
| `FACE_BATCH_SIZE` | int | `10` | Rows per cursor iteration |
| `FACE_POLL_INTERVAL` | int | `5` | Sleep seconds when batch is empty |
| `FACE_SIMILARITY_THRESHOLD` | float | `0.55` | Per-service similarity threshold |
| `FACE_MIN_QUALITY_THRESHOLD` | float | `0.67` | Per-service quality threshold |
| `FACE_VIDEO_MAX_FRAMES` | int | `10` | Max frames extracted from video |
| `FACE_CIRCLE_VIDEO_FPS` | float | `2.0` | FPS for circle video extraction |

### 5.6 User Intelligence Service

| Variable | Type | Default | Description |
|---|---|---|---|
| `USER_INTEL_PROCESSING_ENABLED` | bool | `false` | Master switch |
| `USER_INTEL_BATCH_SIZE` | int | `100` | Sightings rows per iteration |
| `USER_INTEL_POLL_INTERVAL` | int | `5` | Sleep seconds when batch is empty |
| `USER_INTEL_NETWORK_ENABLED` | bool | `true` | Enable social graph construction |

### 5.7 Link Discovery Service

| Variable | Type | Default | Description |
|---|---|---|---|
| `LINK_DISCOVERY_PROCESSING_ENABLED` | bool | `true` | Master switch |
| `LINK_DISCOVERY_BATCH_SIZE` | int | `100` | Messages per iteration |
| `LINK_DISCOVERY_POLL_INTERVAL` | int | `5` | Sleep seconds when batch is empty |
| `LINK_DISCOVERY_RESOLVE_METADATA` | bool | `false` | Enable Telegram API metadata resolution |
| `LINK_DISCOVERY_RESOLVE_RATE_LIMIT` | int | `10` | Max API calls per minute for resolution |

### 5.8 Bulk Sender Service

| Variable | Type | Default | Description |
|---|---|---|---|
| `BULK_SENDER_SEND_DELAY` | float | `1.5` | Inter-send delay in seconds (min 1.0 enforced) |
| `BULK_SENDER_MAX_RETRIES` | int | `3` | Retries per file on transient error |
| `BULK_SENDER_SESSIONS_PATH` | str | `/data/sessions/bulk_sender` | Session files directory |
| `BULK_SENDER_BOT_TOKENS` | str | `""` | Semicolon-separated bot tokens for sending |

### 5.9 Resilience and Scheduling

| Variable | Type | Default | Description |
|---|---|---|---|
| `CIRCUIT_BREAKER_THRESHOLD` | int | `5` | Failures before circuit opens |
| `CIRCUIT_BREAKER_TIMEOUT` | int | `60` | Seconds before OPEN → HALF_OPEN |
| `SIGTERM_DRAIN_TIMEOUT` | int | `30` | Seconds to drain in-flight tasks on shutdown |
| `REDIS_RECONNECT_INTERVAL` | int | `30` | Seconds between Redis reconnect attempts |
| `REDIS_RECONNECT_MAX_ATTEMPTS` | int | `0` | Max reconnect attempts (0 = infinite) |
| `SESSION_ROTATION_ENABLED` | bool | `true` | Auto-rotate to next account on session failure |
| `ACCOUNT_SCHEDULE_ENABLED` | bool | `false` | Enable time-window account scheduling |
| `ACCOUNT_ACTIVE_START` | str | `00:00` | UTC time to activate accounts (HH:MM) |
| `ACCOUNT_ACTIVE_END` | str | `24:00` | UTC time to deactivate accounts (HH:MM) |
| `MAX_WORKERS` | int | `10` | Autoscaler maximum worker count |
| `SCALE_UP_SUSTAINED_SECONDS` | int | `60` | Seconds above high-watermark before scale-up |
| `SCALE_DOWN_SUSTAINED_SECONDS` | int | `120` | Seconds below low-watermark before scale-down |

### 5.10 Observability

| Variable | Type | Default | Description |
|---|---|---|---|
| `ENABLE_PROMETHEUS` | bool | `true` | Expose Prometheus metrics endpoint |
| `PROMETHEUS_PORT` | int | `8000` | Prometheus HTTP server port |
| `LOG_FORMAT` | str | `json` | `json` or `text` |
| `HUB_NOTIFY_BATCH_INTERVAL` | int | `30` | Seconds between Hub notification flushes |
| `HUB_NOTIFY_RATE_LIMIT` | int | `100` | Max Hub messages per minute |

---

## 6. Security

### 6.1 Implemented Controls

- **Session isolation**: Telegram session files are stored per-service under `SESSIONS_BASE_PATH`. The login bot distributes copies to each service subdirectory via `SessionRouter`.
- **Message auto-deletion**: The login bot deletes all messages (including phone numbers, OTP codes, and 2FA passwords) within 30 seconds. 2FA passwords are deleted immediately on receipt.
- **Rate limiting**: Login attempts are limited to 5 per 5-minute rolling window per user.
- **Bot FloodWait isolation**: When a bot is rate-limited by Telegram, it is locked out and the pool rotates to the next available bot.
- **Per-service database users**: Each service connects with a least-privilege PostgreSQL role. The `dashboard_user` role is read-only.
- **Dynamic settings**: Sensitive thresholds can be overridden at runtime via Redis without restarting containers.

### 6.2 Known Deficiencies

- **Hardcoded database passwords**: `init-db.sql` creates service DB users with static plaintext passwords (`collector_password`, `face_recog_password`, etc.). These are committed to the repository. Production deployments must rotate these credentials after initial setup.
- **No TLS on internal services**: Inter-container communication uses plain TCP within the Docker network.
- **No authentication on dashboards**: Streamlit dashboards are bound to `127.0.0.1` only but have no authentication layer.

---

## 7. Non-Functional Requirements

### 7.1 Error Handling

- All service cursor loops catch per-item exceptions and continue processing the batch (never abort on a single failure).
- Failed face recognition messages are pushed to a Redis DLQ (`face_recognition:dlq`) with error type, retry count, and timestamp.
- The shared DLQ processor (`dlq.py`) classifies errors as TRANSIENT, PERMANENT, or RESOURCE and applies retry intervals of 1m, 5m, 15m, 1h, 2h.
- Hub notifications that fail to send are cached in a local SQLite file (`hub_cache.db`) and replayed on reconnect.

### 7.2 Logging

- All services emit structured JSON logs via `python-json-logger`.
- Each log record includes `timestamp`, `level`, `name`, `message`, and optionally `trace_id`.
- Trace IDs propagate through the processing queue via task metadata.

### 7.3 Observability

Prometheus metrics exposed on port 8000 (collector container):

| Metric | Type | Description |
|---|---|---|
| `telegram_queue_depth` | Gauge | Current items in processing queue |
| `telegram_processing_seconds` | Histogram | Task processing latency |
| `telegram_errors_total` | Counter | Errors by type label |
| `telegram_worker_status` | Gauge | Per-worker up/down status |
| `telegram_faces_detected_total` | Counter | Cumulative faces detected |
| `telegram_media_processed_total` | Counter | Media items processed by type |

### 7.4 Graceful Shutdown

On `SIGTERM`, the processing queue:
1. Sets `_running = False`.
2. Waits up to `SIGTERM_DRAIN_TIMEOUT` seconds for in-flight workers to complete.
3. Re-enqueues any tasks still held by workers that did not finish.
4. Cancels remaining workers.

All service cursor loops finish the current batch before exiting.
