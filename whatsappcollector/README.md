# WhatsApp Collector

A multi-service ingestion and intelligence platform. Connects one or more messaging sessions, normalizes and stores all events in PostgreSQL, archives media, runs biometric identity clustering, builds user-graph signals, discovers group invite links, and executes controlled bulk media delivery.

## Architecture at a Glance

| Layer | Components | Notes |
|---|---|---|
| Ingestion | `wa-client-ts-1`, `wa-client-ts-2` | Baileys-based session clients; internal HTTP control and media bridge |
| Broker | RabbitMQ (`whatsapp.events`) or Redis Streams | DLQ: `dlq.failed` |
| Canonical data | `collector` | Owns `collector.*` schema and service cursor registry |
| Downstream processors | `media_archival`, `face_recognition`, `user_intelligence`, `link_discovery`, `bulk_sender` | Cursor-driven fan-out from collector data |
| Shared library | `shared/` | Circuit breaker, task supervisor, DLQ, config, DB, Redis, observability |
| Ops UI | `dashboard_index` (nginx) + per-service Streamlit dashboards | Dashboards are manual processes; workers start by default |

---

## Storage Requirements

This system is designed for use with an **external HDD or SSD**. Media archival, face embeddings, and message history can consume **hundreds of gigabytes** over time.

> ⚠️ **Warning**: Leaving `EXTERNAL_STORAGE_ROOT` empty will cause Docker internal volumes to fill up and exhaust disk space in production.

Set `EXTERNAL_STORAGE_ROOT` in your `.env` file to point to a path on your external drive:

**Windows:**
```
EXTERNAL_STORAGE_ROOT=D:\whatsapp_data
```

**Linux / macOS:**
```
EXTERNAL_STORAGE_ROOT=/mnt/external/whatsapp_data
```

The following services use this path for persistent storage: media archival downloads, face recognition model files and embeddings, and message/chat history databases.

---

## First Run Checklist

Follow these steps in order after cloning the repository to reach a fully operational state.

1. **Set external storage path** — In `.env`, set `EXTERNAL_STORAGE_ROOT` to a path on your external HDD/SSD (see [Storage Requirements](#storage-requirements)).
   - Verify: the path exists and has sufficient free space (500 GB+ recommended).

2. **Rotate all secrets** — Replace every value containing `CHANGE_ME_` in `.env` with a strong random secret.
   - Verify: `grep -r "CHANGE_ME_" .env` returns no results.

3. **Run pre-build check** — Execute the pre-build check script to validate your environment.
   - Windows: `.\infrastructure\scripts\pre_build_check.ps1`
   - Linux/macOS: `bash infrastructure/scripts/pre_build_check.sh`
   - Verify: script exits with no errors.

4. **Build and start containers** — `docker compose build && docker compose up -d`
   - Verify: `docker compose ps` shows all containers starting.

5. **Run database migrations** — `python infrastructure/scripts/run_migrations.py`
   - Verify: script completes with "All migrations applied successfully".

6. **Scan QR codes** — Open the dashboard for each wa-client session and scan the QR code with WhatsApp.
   - Verify: session status changes to `connected` in the dashboard.

7. **Verify service health** — `docker compose ps`
   - Verify: all services show `(healthy)` status. Allow 2–3 minutes for health checks to pass.

8. **Configure Findings Hub** — Set `FINDINGS_HUB_GROUP_NAME` in `.env` to the exact name of your target WhatsApp group, then restart wa-client-ts containers.
   - If the group name doesn't match, open the collector dashboard → Findings Hub panel to see all detected groups and their JIDs. Set `FINDINGS_HUB_GROUP_JID` directly.
   - Verify: collector dashboard shows "Active hub group: &lt;group name&gt;".

9. **Handle face recognition degraded mode** — If the face_recognition service shows `degraded` in logs, the dlib model files are downloading automatically. Wait for the download to complete (files are ~100 MB each).
   - If download fails (no internet access), manually place model files at the path shown in `FACE_MODELS_PATH`.
   - Verify: `docker compose logs face_recognition | grep face_models_loaded` shows success.

---

## Prerequisites

| Tool | Minimum Version | Purpose |
|---|---|---|
| Docker | 24.x | Container runtime |
| Docker Compose | v2.20+ | Orchestration (`docker compose` CLI) |
| Python | 3.12 | Local scripts, tests, migrations |
| Node.js | 20.x | TypeScript service local build/test |
| npm | 10.x | TypeScript dependency management |

> Python and Node.js are only required for running tests and scripts locally. The Docker images are self-contained.

---

## Quick Start

### 1. Clone and configure

```bash
cp .env.template .env
# Edit .env — rotate ALL secrets marked CHANGE_ME_* before proceeding
```

### 2. Run pre-build checks

```bash
# Linux / macOS
bash infrastructure/scripts/pre_build_check.sh

# Windows PowerShell
./infrastructure/scripts/pre_build_check.ps1
```

The script validates that no default `CHANGE_ME_*` credentials remain in `.env`.

### 3. Build and start

```bash
docker compose build
docker compose up -d
docker compose ps
```

All services should reach `(healthy)` status within ~60 seconds. Infrastructure services (postgres, redis, rabbitmq) must be healthy before application services start.

### 4. Run database migrations (first run only)

```bash
python infrastructure/scripts/run_migrations.py
```

On a fresh instance, `infrastructure/init-db.sql` is applied automatically via the Postgres init mount. The migration runner is for incremental schema changes.

### 5. Pair WhatsApp sessions

```bash
docker compose logs -f wa-client-ts-1
# Scan the QR code displayed in the logs with your phone
docker compose logs -f wa-client-ts-2
```

---

## Runtime Ports

| Service | Host Port | Purpose |
|---|---|---|
| `dashboard_index` | `8500` | Static dashboard index page |
| `collector` | `8501`, `9090` | Streamlit UI (manual), Prometheus metrics + `/health` |
| `media_archival` | `8502`, `9091` | Streamlit UI (manual), metrics + `/health` |
| `face_recognition` | `8503`, `9092` | Streamlit UI (manual), metrics + `/health` |
| `user_intelligence` | `8504`, `9093` | Streamlit UI (manual), metrics + `/health` |
| `link_discovery` | `8505`, `9094` | Streamlit UI (manual), metrics + `/health` |
| `bulk_sender` | `8506`, `9095` | Streamlit UI (manual), metrics + `/health` |
| `wa-client-ts-1` | `3011` | Internal control + media bridge |
| `wa-client-ts-2` | `3012` | Internal control + media bridge |
| `rabbitmq` | `5672`, `15672` | AMQP + management UI |

All host-side ports are configurable via environment variables (see `.env.template`).

### Starting Streamlit Dashboards

Worker containers start by default. Streamlit dashboards are separate manual processes:

```bash
docker compose exec collector streamlit run collector/dashboard/app.py --server.address 0.0.0.0 --server.port 8501
docker compose exec media_archival streamlit run media_archival/dashboard/app.py --server.address 0.0.0.0 --server.port 8501
docker compose exec face_recognition streamlit run face_recognition_service/dashboard/app.py --server.address 0.0.0.0 --server.port 8501
docker compose exec user_intelligence streamlit run user_intelligence/dashboard/app.py --server.address 0.0.0.0 --server.port 8501
docker compose exec link_discovery streamlit run link_discovery/dashboard/app.py --server.address 0.0.0.0 --server.port 8501
docker compose exec bulk_sender streamlit run bulk_sender/dashboard/app.py --server.address 0.0.0.0 --server.port 8501
```

---

## Environment Variables

Copy `.env.template` to `.env` and fill in all required values. Do not commit `.env` to version control.

### Infrastructure

| Variable | Required | Default | Description |
|---|---|---|---|
| `POSTGRES_HOST` | Yes | `postgres` | PostgreSQL hostname |
| `POSTGRES_PORT` | Yes | `5432` | PostgreSQL port |
| `POSTGRES_DB` | Yes | — | Database name |
| `POSTGRES_USER` | Yes | — | Database user |
| `POSTGRES_PASSWORD` | **Yes** | — | Database password — rotate immediately |
| `POSTGRES_SSL_MODE` | Yes | `require` | `disable`, `require`, `verify-ca`, or `verify-full` |
| `POSTGRES_SSL_CERT_DAYS` | No | `3650` | Self-signed cert lifetime (days) |
| `DATABASE_URL` | **Yes** | — | asyncpg DSN used by all Python services |
| `BROKER_TYPE` | Yes | `rabbitmq` | `rabbitmq` or `redis` |
| `RABBITMQ_URL` | Yes (rabbit) | — | AMQP DSN including credentials |
| `RABBITMQ_USER` | Yes | — | RabbitMQ management user |
| `RABBITMQ_PASSWORD` | **Yes** | — | RabbitMQ password — rotate immediately |
| `RABBITMQ_ERLANG_COOKIE` | **Yes** | — | RabbitMQ cluster cookie — rotate immediately |
| `RABBITMQ_VHOST` | No | `/` | RabbitMQ virtual host |
| `REDIS_PASSWORD` | **Yes** | — | Redis password — rotate immediately |
| `REDIS_URL` | Yes | — | Redis DSN including password |
| `LOG_LEVEL` | No | `info` | Log level for all services (`debug`, `info`, `warn`, `error`) |
| `EXTERNAL_STORAGE_ROOT` | No | — | Host path for external storage (e.g. `/mnt/data`); uses Docker volumes if empty |
| `COMPOSE_PROJECT_NAME` | Recommended | `whatsappcollector` | Docker resource naming prefix |

### Session Client (`wa-client-ts`)

| Variable | Required | Default | Description |
|---|---|---|---|
| `SESSION_NAME` | Yes | `session_1` | Active session identifier for this container |
| `SESSION_NAMES` | No | `session_1` | CSV list of all session names (used by collector dashboard) |
| `SYNC_FULL_HISTORY` | No | `false` | `true` to enable full history sync on first connect |
| `PAIRING_CODE_PHONE` | No | — | E.164 phone number for pairing-code mode instead of QR |
| `MEDIA_STORAGE_PATH` | Yes | `/data/media` | Shared media volume root |
| `AUTH_STORAGE_PATH` | No | `./auth_info/<session>` | Auth state directory |
| `MEDIA_BRIDGE_SECRET` | **Yes** | — | HMAC secret for `/media/decrypt` and `/send-media`; minimum 32 characters |
| `MEDIA_BRIDGE_URL` | Yes | `http://wa-client-ts-1:3001` | Bridge URL used by collector and media_archival |
| `SESSION_BRIDGES_JSON` | No | — | JSON map `{"session_name": "http://host:port"}` for multi-session bridge routing |
| `FINDINGS_HUB_ENABLED` | No | `true` | Set `false` to disable the findings hub sender |
| `FINDINGS_HUB_GROUP_JID` | No | — | WhatsApp JID of the findings hub group (bypasses auto-detection) |
| `FINDINGS_HUB_GROUP_NAME` | No | `Findings Hub` | Group subject used for auto-detection |
| `FINDINGS_SEND_DELAY` | No | `3` | Base delay (seconds) between findings sends |

### Collector

| Variable | Required | Default | Description |
|---|---|---|---|
| `METRICS_PORT` | No | `9090` | Prometheus metrics and `/health` port |
| `COLLECTOR_BACKFILL_REQ_PER_MIN` | No | `5` | Backfill request rate limit |
| `COLLECTOR_BACKFILL_POLL_SECONDS` | No | `30` | Backfill resume loop interval |
| `COLLECTOR_DEDUP_TTL_SECONDS` | No | `86400` | Redis dedup key TTL (seconds) |
| `SESSION_RISK_THRESHOLD` | No | `0.8` | Session risk score threshold (0–1) |
| `SESSION_COOLDOWN_SECONDS` | No | `300` | Auto-cooldown duration on high risk |
| `MAX_PAYLOAD_BYTES` | No | `10485760` | Maximum accepted message payload (bytes) |
| `DB_POOL_SIZE` | No | `5` | asyncpg pool min size |
| `DB_MAX_OVERFLOW` | No | `10` | asyncpg pool max overflow |
| `DB_POOL_TIMEOUT` | No | `30` | Pool acquire timeout (seconds) |
| `DB_POOL_RECYCLE` | No | `1800` | Connection recycle interval (seconds) |
| `CONTROL_PLANE_SECRET_KEY` | Yes (for encrypted secret storage) | — | AES key material (raw 16/24/32-byte or base64/urlsafe-base64) used to encrypt persisted control-plane secrets |
| `CONTROL_PLANE_SECRET_KEY_ID` | No | `local-kek-v1` | Key identifier stored with encrypted rows to support key rotation |
| `DASHBOARD_AUTH_REQUIRED` | No | `true` | Require dashboard sign-in for role-based mutation authorization |
| `DASHBOARD_VIEWER_USERNAME` | No | `viewer` | Viewer identity (read-only access) |
| `DASHBOARD_VIEWER_PASSWORD` | Yes (if auth required) | — | Viewer password |
| `DASHBOARD_OPERATOR_USERNAME` | No | `operator` | Operator identity (standard mutation access) |
| `DASHBOARD_OPERATOR_PASSWORD` | Yes (if auth required) | — | Operator password |
| `DASHBOARD_ADMIN_USERNAME` | No | `admin` | Admin identity (high-impact mutation access) |
| `DASHBOARD_ADMIN_PASSWORD` | Yes (if auth required) | — | Admin password |

### Media Archival

| Variable | Required | Default | Description |
|---|---|---|---|
| `MEDIA_ARCHIVAL_BATCH_SIZE` | No | `50` | Pending media rows per poll cycle |
| `MEDIA_ARCHIVAL_POLL_SECONDS` | No | `5` | Download loop poll interval |
| `MEDIA_ARCHIVAL_MAX_DOWNLOAD_RETRIES` | No | `3` | Failures before a row is dead-lettered |
| `MEDIA_RETENTION_DAYS` | No | `90` | File retention window |
| `MEDIA_CLEANUP_INTERVAL_HOURS` | No | `24` | Cleanup job interval |
| `MEDIA_REDOWNLOAD_ENABLED` | No | `false` | Enable expiring-media refresh loop |
| `MEDIA_REDOWNLOAD_LOOKAHEAD_HOURS` | No | `2` | Expiry lookahead window |

### Face Recognition

| Variable | Required | Default | Description |
|---|---|---|---|
| `FACE_MODELS_PATH` | Recommended | `/data/models` | Directory containing dlib model files |
| `FACE_BIOMETRIC_SEMAPHORE` | No | `1` | Max concurrent CPU-bound embedding extractions |
| `FACE_MATCH_THRESHOLD` | No | `0.6` | L2 distance threshold for identity match |
| `FACE_DETECTION_MODEL` | No | `hog` | `hog` (CPU) or `cnn` (GPU) |
| `FACE_UPSAMPLE_TIMES` | No | `1` | Face detector upsampling passes |
| `FACE_PROCESSING_BATCH_SIZE` | No | `8` | Pending media rows per poll cycle |
| `FACE_POLL_SECONDS` | No | `15` | Processing loop poll interval |
| `MAX_IMAGE_DIMENSION` | No | `1600` | Input image resize clamp (pixels) |
| `VIDEO_FRAME_RATE` | No | `1` | Frames per second to sample from video |
| `VIDEO_NOTE_FRAME_RATE` | No | `2` | FPS for video note messages |
| `PHASH_DEDUP_THRESHOLD` | No | `10` | pHash distance threshold for near-duplicate frame filter |
| `FINDINGS_MAX_PER_HOUR` | No | `30` | Token bucket cap on findings publication |
| `FINDINGS_MIN_CONFIDENCE` | No | `0.5` | Minimum confidence to publish a finding |

### User Intelligence

| Variable | Required | Default | Description |
|---|---|---|---|
| `USER_INTEL_BATCH_SIZE` | No | `500` | Sightings rows per poll cycle |
| `USER_INTEL_POLL_INTERVAL_SEC` | No | `10` | Worker poll interval |
| `USER_INTEL_PROMETHEUS_PORT` | No | `9093` | Metrics port |
| `TRACKED_FIELDS` | No | `display_name,push_name,...` | Comma-separated profile fields to diff |

### Link Discovery

| Variable | Required | Default | Description |
|---|---|---|---|
| `LINK_DISCOVERY_BATCH_SIZE` | No | `200` | Candidate message rows per poll cycle |
| `LINK_DISCOVERY_POLL_INTERVAL_SEC` | No | `10` | Worker poll interval |
| `LINK_DISCOVERY_MAX_JOINS_PER_HOUR` | No | `3` | Join rate limit |
| `LINK_DISCOVERY_JOIN_DELAY_SECONDS` | No | `120` | Delay between join attempts |
| `LINK_DISCOVERY_PROMETHEUS_PORT` | No | `9094` | Metrics port |

### Bulk Sender

| Variable | Required | Default | Description |
|---|---|---|---|
| `BULK_SENDER_INTERNAL_TARGET_JID` | Yes (internal mode) | — | Fixed target JID for internal send jobs |
| `BULK_SENDER_INTERNAL_MIN_DELAY` | No | `2.0` | Minimum delay between internal sends (seconds) |
| `BULK_SENDER_EXTERNAL_MIN_DELAY` | No | `8.0` | Minimum delay between external sends (seconds) |
| `BULK_SENDER_EXTERNAL_MAX_PER_HOUR` | No | `30` | Hard hourly cap for external sends |
| `BULK_SENDER_MAX_EXTERNAL_TARGETS` | No | `20` | Maximum target rows per external job |
| `BULK_SENDER_MEMBERSHIP_MIN_AGE_HOURS` | No | `48` | Minimum membership age for external targets |
| `BULK_SENDER_PROMETHEUS_PORT` | No | `9095` | Metrics port |

### Port Overrides (all optional)

| Variable | Default | Service |
|---|---|---|
| `WA_CLIENT_1_HOST_PORT` | `3011` | wa-client-ts-1 |
| `WA_CLIENT_2_HOST_PORT` | `3012` | wa-client-ts-2 |
| `DASHBOARD_INDEX_PORT` | `8500` | dashboard_index |
| `COLLECTOR_DASHBOARD_PORT` | `8501` | collector Streamlit |
| `COLLECTOR_METRICS_PORT` | `9090` | collector metrics |
| `MEDIA_ARCHIVAL_DASHBOARD_PORT` | `8502` | media_archival Streamlit |
| `MEDIA_ARCHIVAL_METRICS_PORT` | `9091` | media_archival metrics |
| `FACE_RECOGNITION_DASHBOARD_PORT` | `8503` | face_recognition Streamlit |
| `FACE_RECOGNITION_METRICS_PORT` | `9092` | face_recognition metrics |
| `USER_INTELLIGENCE_DASHBOARD_PORT` | `8504` | user_intelligence Streamlit |
| `USER_INTELLIGENCE_METRICS_PORT` | `9093` | user_intelligence metrics |
| `LINK_DISCOVERY_DASHBOARD_PORT` | `8505` | link_discovery Streamlit |
| `LINK_DISCOVERY_METRICS_PORT` | `9094` | link_discovery metrics |
| `BULK_SENDER_DASHBOARD_PORT` | `8506` | bulk_sender Streamlit |
| `BULK_SENDER_METRICS_PORT` | `9095` | bulk_sender metrics |

---

## Testing

### TypeScript unit tests

```bash
cd services/wa-client-ts
npm test
```

### Python phase tests

```bash
python -m pytest tests/py_tests/test_collector_phase_c.py \
  tests/py_tests/test_media_archival_phase_d.py \
  tests/py_tests/test_face_recognition_phase_e.py \
  tests/py_tests/test_user_intelligence_phase_f.py \
  tests/py_tests/test_link_discovery_phase_g.py \
  tests/py_tests/test_bulk_sender_phase_h.py -q
```

### Audit bugfix regression tests

```bash
python -m pytest tests/py_tests/test_audit_bugfix_*.py -q
```

### Additional gates

```bash
# Biometric benchmark
python -m pytest tests/py_tests/test_biometric_benchmark_gate.py -q

# Infrastructure scripts
python -m pytest tests/py_tests/test_infrastructure_scripts.py -q

# Integration harness (requires running stack)
RUN_INTEGRATION_TESTS=1 python -m pytest tests/py_tests/test_integration_harness.py -q
```

---

## Migrations

```bash
# Apply all pending migrations
python infrastructure/scripts/run_migrations.py

# Override migration directory
MIGRATIONS_DIR=infrastructure/migrations python infrastructure/scripts/run_migrations.py
```

---

## Operational Scripts

| Script | Purpose |
|---|---|
| `infrastructure/scripts/pre_build_check.sh` | Validates `.env` has no default `CHANGE_ME_*` secrets |
| `infrastructure/scripts/pre_build_check.ps1` | PowerShell equivalent |
| `infrastructure/scripts/check_ports.sh` | Pre-start host port conflict checker; supports `PORT_CONFLICT_STRATEGY=auto_increment` |
| `infrastructure/scripts/check_ports.ps1` | PowerShell equivalent |
| `infrastructure/scripts/run_migrations.py` | Applies SQL migrations in order |
| `infrastructure/scripts/manage_dlq.py` | DLQ inspection and replay |
| `infrastructure/scripts/clear_queues.py` | Purge broker queues (use with caution) |

---

## Face Recognition Model Setup

The `face_recognition` service requires two dlib model files in the mounted models volume:

```
/data/models/shape_predictor_68_face_landmarks.dat
/data/models/dlib_face_recognition_resnet_model_v1.dat
```

If models are absent at startup, the worker starts in degraded mode (`is_ready=False`) and retries loading every 60 seconds. No container restart is required once models are placed in the volume.

Download script (legacy processor path, models are compatible):

```bash
bash services/processor-py/scripts/download_models.sh
```

---

## Repository Layout

```
services/
    wa-client-ts/            # WhatsApp session client (TypeScript)
    collector/               # Canonical ingest writer (Python)
    media_archival/          # Media download and retention (Python)
    face_recognition/        # Biometric embedding and identity (Python)
    user_intelligence/       # Profile change and graph tracking (Python)
    link_discovery/          # Link extraction and join queueing (Python)
    bulk_sender/             # Controlled bulk send (Python)
    processor-py/            # Legacy monolith (optional, Compose profile: legacy)
shared/                      # Cross-service Python library
infrastructure/
    init-db.sql              # Bootstrap SQL (applied on first Postgres start)
    migrations/              # Incremental SQL migrations
    scripts/                 # Pre-build, port check, DLQ, migration scripts
    dashboard_index/         # nginx template and entrypoint for dashboard index
tests/
    py_tests/                # Python phase, audit, benchmark, and integration tests
    ts_tests/                # TypeScript unit tests
```
