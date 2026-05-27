# Product Requirements Document

_Code-as-truth snapshot. Generated from repository implementation audit._

---

## 1. Executive Summary

This system is a multi-service ingestion and intelligence platform that connects one or more messaging sessions, normalizes and persists all events to PostgreSQL, archives media payloads, performs biometric identity clustering on image and video content, builds user-graph intelligence signals, discovers and queues group invite links, and executes controlled bulk media delivery with anti-abuse enforcement.

The active architecture is **schema-per-service microservices** orchestrated via Docker Compose. The message broker is RabbitMQ (default) with Redis Streams as a configurable fallback. A legacy monolithic processor (`services/processor-py`) remains available under the `legacy` Compose profile for rollback compatibility.

---

## 2. System Architecture

### 2.1 Service Topology

| Service | Language | Role | Metrics Port | Schema |
|---|---|---|---|---|
| `wa-client-ts-1` / `wa-client-ts-2` | TypeScript / Node.js 20 | Session connectors — event normalization, broker publishing, media decrypt bridge, outbound send | — | — |
| `collector` | Python 3.12 | Canonical ingest consumer; writes all collector schema tables; backfill and session health orchestration | 9090 | `collector.*` |
| `media_archival` | Python 3.12 | Downloads and decrypts media via bridge; deduplicates by SHA-256; manages retention and optional redownload | 9091 | `media_archival.*` |
| `face_recognition` | Python 3.12 | Extracts 128-d dlib embeddings from images and video frames; nearest-centroid identity matching via pgvector; publishes findings | 9092 | `face_recognition.*` |
| `user_intelligence` | Python 3.12 | Tracks field-level profile changes; records chat memberships; builds co-membership connection graph | 9093 | `user_intelligence.*` |
| `link_discovery` | Python 3.12 | Extracts group invite and contact links from message text and nested payloads; applies queue rules; enqueues joins | 9094 | `link_discovery.*` |
| `bulk_sender` | Python 3.12 | Executes internal and external send jobs with hard rate caps, operator confirmation gates, and file-hash deduplication | 9095 | `bulk_sender.*` |
| `dashboard_index` | nginx:alpine | Static index page served via envsubst template; links to all Streamlit dashboards | 8500 | — |
| `shared/` | Python 3.12 | Cross-service library: `circuit_breaker`, `task_supervisor`, `dlq`, `config`, `db`, `redis_client`, `observability` | — | — |

**Infrastructure:** PostgreSQL 15 with pgvector extension, RabbitMQ 3.13, Redis 7.

### 2.2 End-to-End Data Flow

```
Session Client (wa-client-ts)
  │  normalizes events → publishes to broker routing keys
  ▼
Broker (RabbitMQ topic exchange: whatsapp.events  |  Redis Streams)
  │
  ├─► collector          consumes: messages.inbound, messages.status,
  │                                messages.history, contacts.update,
  │                                groups.metadata, session.events, calls
  │                       writes: collector.raw_messages, collector.users,
  │                               collector.chats, collector.user_sightings,
  │                               collector.service_cursors
  │
  └─► findings.publish ◄─ face_recognition publishes sighting events
        │
        └─► wa-client-ts FindingsHubSender → outbound message to configured group

Downstream services (cursor-driven fan-out from collector.raw_messages):
  media_archival    → downloads media, writes media_archival.media_files
  face_recognition  → processes media, writes face_recognition.face_embeddings / identity_entities
  user_intelligence → processes sightings, writes user_intelligence.user_history / user_connections
  link_discovery    → scans message text, writes link_discovery.discovered_links / join_queue
  bulk_sender       → executes send_jobs, writes bulk_sender.sent_items
```

### 2.3 Broker Topology

**RabbitMQ exchange:** `whatsapp.events` (topic, durable)  
**DLQ exchange/queue:** `dlq.events` / `dlq.failed`

| Queue | Producer | Consumer | Purpose |
|---|---|---|---|
| `messages.inbound` | `wa-client-ts` | `collector` | Inbound messages (text, media, reactions) |
| `messages.status` | `wa-client-ts` | `collector` | WhatsApp status/story broadcast updates |
| `messages.history` | `wa-client-ts` | `collector` | History sync payloads (initial and on-demand backfill) |
| `contacts.update` | `wa-client-ts` | `collector` | Contact profile updates |
| `groups.metadata` | `wa-client-ts` | `collector` | Group metadata and participant updates |
| `session.events` | `wa-client-ts` | `collector` | Session lifecycle events (connect, disconnect, heartbeat) |
| `calls` | `wa-client-ts` | `collector` | Call log events |
| `findings.publish` | `face_recognition` | `wa-client-ts` FindingsHubSender | Face sighting findings for operator notification |

Redis Streams mode maps routing keys to stream names via `BrokerProducer.getRedisStreamsForRoutingKey()`.

### 2.4 Shared Library (`shared/`)

All modular Python services import from `shared/`:

| Module | Purpose |
|---|---|
| `shared/config.py` | `BaseConfig` (pydantic-settings); resolves `.env` relative to repo root |
| `shared/observability.py` | structlog setup; `HealthAndMetricsHandler`; `start_metrics_server()` |
| `shared/task_supervisor.py` | `TaskSupervisor` — asyncio watchdog with restart and flap detection |
| `shared/circuit_breaker.py` | `CircuitBreaker` — three-state (Closed/Open/Half-Open) for Redis dedup |
| `shared/dlq.py` | `nack_to_dlq()`, `DLQConsumerBase` |
| `shared/db.py` | asyncpg pool factory |
| `shared/redis_client.py` | ioredis-compatible Redis client wrapper |

---

## 3. Functional Requirements

### FR-1 — Multi-Session Ingestion and Pairing

- Sessions identified by `SESSION_NAME` environment variable per container.
- QR code lifecycle tracked in memory; exposed via `GET /qr` as base64 PNG.
- Pairing-code mode supported for valid E.164 phone numbers via `PAIRING_CODE_PHONE`.
- Automatic reconnect with capped exponential backoff (max 60 s).
- Corrupted auth state detection: 3 stream-515 errors within 60 s triggers auth wipe and re-pair.

### FR-2 — Message Normalization and Routing

- Canonicalization includes: chat type, sender JID/LID, forwarding metadata, media metadata, message type.
- Status broadcasts, channel/newsletter messages, and reactions are explicitly handled.
- Anti-ban throttle: 1 s minimum interval on `msg.*` and `profile_photo.*` routing keys; `messages.history` and `contacts.update` are exempt.

### FR-3 — Backfill Orchestration

- `POST /backfill-request` validates payload and calls Baileys `fetchMessageHistory`.
- Request ID ↔ correlation ID mapping persisted in runtime state with TTL.
- Collector stores durable backfill jobs; resumes pending/running jobs on startup.
- Session disconnect pauses active backfill jobs for that session.

### FR-4 — Media Archival and Retention

- `POST /media/decrypt` — HMAC-SHA256 authenticated; timing-safe compare; returns `application/octet-stream` with `X-Media-SHA256`, `X-Media-MimeType`, `X-Local-Path` headers.
- Archival deduplicates by SHA-256 (`by_id_path`) and writes per-message symlink (`by_message_path`).
- Cleanup deletes files only when safe (min cursor + retention window).
- Optional redownload loop for assets approaching expiry (`MEDIA_REDOWNLOAD_ENABLED`).
- Per-row failure counter: after `MEDIA_ARCHIVAL_MAX_DOWNLOAD_RETRIES` (default 3) consecutive failures, cursor advances past the row and the failure is recorded.

### FR-5 — Face Recognition and Identity Tracking

- Processes JPEG/PNG images and sampled video frames (configurable FPS, pHash near-duplicate filter).
- 128-dimensional dlib ResNet embeddings via `face_recognition` library.
- pgvector HNSW nearest-centroid match with configurable L2 threshold (`FACE_MATCH_THRESHOLD`, default 0.6).
- Creates new identity entity when no match found within threshold.
- Token-bucket rate limiter on findings publication (`FINDINGS_MAX_PER_HOUR`).
- Confidence gate (`FINDINGS_MIN_CONFIDENCE`) filters low-quality embeddings.
- Non-fatal startup: missing model files set `is_ready=False`; `_model_reload_loop` retries every 60 s without crashing the worker.

### FR-6 — User Intelligence Graph

- Tracks field-level profile changes for: `display_name`, `push_name`, `business_name`, `phone_number`, `is_business`, `is_verified`, `profile_photo` (configurable via `TRACKED_FIELDS`).
- Records user↔chat memberships with message count.
- Builds user↔user connection edges from shared chat co-membership.

### FR-7 — Link Discovery and Queueing

- Extracts `chat.whatsapp.com/*` and `wa.me/*` links from message body and all nested payload strings (recursive walk).
- Deduplicates discovered links by URL.
- Applies active queue rules (ordered, first-match-wins) with keyword whitelist/blacklist.
- Auto-enqueues joins when rule `auto_queue=true` matches.

### FR-8 — Bulk Sending with Anti-Abuse Policy

- Modes: `internal` (fixed target JID) and `external` (operator-confirmed target list).
- External mode: requires `operator_confirmed=TRUE`; hard cap `min(BULK_SENDER_EXTERNAL_MAX_PER_HOUR, 30)` per hour; max `BULK_SENDER_MAX_EXTERNAL_TARGETS` targets per job.
- Membership-age gate: targets must have been members for at least `BULK_SENDER_MEMBERSHIP_MIN_AGE_HOURS` (default 48 h).
- File-hash deduplication prevents resending the same file to the same target within a job.
- Session disconnect triggers job cooldown.

### FR-9 — Operator Controls

- `POST /logout` — per-session logout (session-name guarded).
- `POST /send-media` — HMAC-authenticated outbound media send; `file_path` must be inside `MEDIA_STORAGE_PATH` (path-traversal guard).
- `POST /join-group` — join by invite code with per-session rate limiting and `Retry-After` semantics.
- Collector dashboard: per-session and bulk logout; schema wipe Danger Zone with typed confirmation (`WIPE <schema>` / `WIPE ALL`); wipe targets allowlisted to the six modular schemas.

### FR-10 — Findings Hub

- `FindingsHubSender` consumes `findings.publish` and forwards face sighting images/captions to a configured WhatsApp group.
- Group detection via `groupFetchAllParticipating()` on first start; detected JID cached to `/app/auth_info/findings_hub_jid.txt` to avoid repeated API calls on restart.
- `FINDINGS_HUB_GROUP_JID` env var takes priority over file cache and API detection.
- On group-not-found: logs `FINDINGS_HUB_NOT_FOUND` token, publishes `system_alert` to broker, sets `isRunning=false`, does not crash the container.

---

## 4. Internal API Contract (`wa-client-ts`)

All routes listen on container port `3001`.

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | None | Process liveness; returns `{ status, whatsapp_ready }` |
| `GET` | `/ready` | None | Readiness; `200` when WhatsApp connected, `503` otherwise |
| `GET` | `/qr` | None | QR lifecycle state; returns `{ status, qr (base64 PNG or null), session_name }` |
| `POST` | `/backfill-request` | None | Trigger on-demand history fetch; returns `{ request_id }` |
| `POST` | `/join-group` | None | Join group by invite code; rate-limited with `Retry-After` |
| `POST` | `/logout` | None | Logout active session |
| `POST` | `/media/decrypt` | `X-Signature` HMAC-SHA256 | Decrypt and stream media; returns `application/octet-stream` |
| `POST` | `/send-media` | `X-Signature` HMAC-SHA256 | Send outbound media to a target JID; `file_path` must be inside `MEDIA_STORAGE_PATH` |

**`POST /send-media` request body:**

| Field | Type | Required | Description |
|---|---|---|---|
| `session_name` | string | Yes | Must match active session |
| `target_chat_jid` | string | Yes | Destination WhatsApp JID |
| `file_path` | string | Yes | Absolute path inside `MEDIA_STORAGE_PATH` |
| `mimetype` | string | No | MIME type; defaults to `application/octet-stream` |
| `caption` | string | No | Message caption |

**Response:** `{ message_id }` on success.

---

## 5. Data Model

### Schema Ownership

Each service owns exactly one PostgreSQL schema. Cross-service reads use `collector.service_cursors` for cursor-based fan-out. No service writes into another service's schema business tables.

| Schema | Owner Service | Key Tables |
|---|---|---|
| `collector` | collector | `raw_messages`, `users`, `chats`, `user_sightings`, `service_cursors`, `backfill_jobs`, `wa_sessions` |
| `media_archival` | media_archival | `media_files`, `download_failures` |
| `face_recognition` | face_recognition | `identity_entities` (pgvector), `face_embeddings`, `processed_media`, `published_findings` |
| `user_intelligence` | user_intelligence | `user_history`, `user_chat_memberships`, `user_connections` |
| `link_discovery` | link_discovery | `discovered_links`, `queue_rules`, `join_queue` |
| `bulk_sender` | bulk_sender | `send_jobs`, `send_targets`, `sent_items` |

### Core Guarantees

1. Collector message uniqueness: `UNIQUE (message_id, chat_jid)` on `collector.raw_messages`.
2. Cross-service progression: cursor-based via `collector.service_cursors`.
3. Face embeddings: 128-d vectors in pgvector columns with HNSW index (`vector_l2_ops`).
4. Media deduplication: SHA-256 hash stored in `media_archival.media_files.sha256`.
5. Bulk send deduplication: `UNIQUE (job_id, target_chat_jid, file_hash)` on `bulk_sender.sent_items`.

---

## 6. Security and Abuse Prevention

| Control | Implementation |
|---|---|
| Media bridge authentication | HMAC-SHA256 (`X-Signature` header); timing-safe compare via `crypto.timingSafeEqual` |
| Outbound send path traversal guard | `isAllowedMediaPath()` resolves and validates against `MEDIA_STORAGE_PATH` |
| Max payload guard | `MAX_PAYLOAD_BYTES` (default 10 MB) on all collector queue handlers |
| Join-group rate limiting | Per-session counter with `Retry-After` response header |
| External bulk send policy | Operator confirmation required; hard hourly cap; membership-age gate |
| Schema wipe confirmation | Typed confirmation string required; allowlisted targets only |
| Secret hygiene | Pre-build check script validates no `CHANGE_ME_*` defaults in `.env` |
| Secret scanning | TruffleHog GitHub Actions workflow on push |
| Anti-ban throttle | 1 s minimum interval on user-facing WhatsApp API calls |

---

## 7. Reliability and Observability

| Mechanism | Implementation |
|---|---|
| Background task supervision | `shared/task_supervisor.py` — `TaskSupervisor` wraps all background loops; restarts on exception; flap detection (>10 restarts in 10 min logs WARNING) |
| Redis circuit breaker | `shared/circuit_breaker.py` — protects dedup Redis calls; fail-open on OPEN state |
| Broker reconnect | RabbitMQ: `aio_pika` reconnect callbacks re-register all consumers after topology redeclaration. TypeScript: capped exponential backoff (max 30 s, 20 attempts) |
| DB connection pool | `pool_pre_ping=True`; configurable `pool_size`, `max_overflow`, `pool_timeout`, `pool_recycle` |
| Health endpoints | `GET /health` on each Python service metrics port; returns `{"status": "ok"|"degraded", "worker": ..., "broker": ...}` |
| Prometheus metrics | Each Python service exposes `/metrics` on its metrics port (9090–9095) |
| Structured logging | structlog JSON output on all Python services; pino JSON on TypeScript services |
| DLQ monitoring | `dlq.failed` depth polled every 60 s; logged at WARNING when threshold exceeded |
| Media download dead-letter | After `MEDIA_ARCHIVAL_MAX_DOWNLOAD_RETRIES` failures, cursor advances and failure recorded |

---

## 8. Quality Gates

| Gate | Command |
|---|---|
| TypeScript unit tests | `cd services/wa-client-ts && npm test` |
| Python phase tests (C–H) | `python -m pytest tests/py_tests/test_collector_phase_c.py tests/py_tests/test_media_archival_phase_d.py tests/py_tests/test_face_recognition_phase_e.py tests/py_tests/test_user_intelligence_phase_f.py tests/py_tests/test_link_discovery_phase_g.py tests/py_tests/test_bulk_sender_phase_h.py -q` |
| Audit bugfix regression tests | `python -m pytest tests/py_tests/test_audit_bugfix_*.py -q` |
| Biometric benchmark gate | `python -m pytest tests/py_tests/test_biometric_benchmark_gate.py -q` |
| Infrastructure script tests | `python -m pytest tests/py_tests/test_infrastructure_scripts.py -q` |
| Integration harness (optional) | `RUN_INTEGRATION_TESTS=1 python -m pytest tests/py_tests/test_integration_harness.py -q` |

---

## 9. Known Gaps

1. `docker-compose.dev.yml` is aligned with the active split-service topology but does not include all downstream services.
2. Face recognition dlib models are not auto-downloaded; they must be present in the mounted models volume before the worker can process biometric data (non-fatal: worker starts and retries every 60 s).
3. Legacy `processor-py` profile remains for rollback compatibility; it is not started by default.
