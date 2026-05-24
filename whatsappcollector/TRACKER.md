# WhatsApp Collector — Toolkit Status

## Status: PRODUCTION-READY (pending model download on first build)

Last audited: 2026-05-14

---

## Services

| Service | Status | Port(s) |
|---|---|---|
| wa-client-ts | ✅ Operational | 3011 / 3012 |
| collector | ✅ Operational | 8501 / 9090 |
| media_archival | ✅ Operational | 8502 / 9091 |
| face_recognition | ✅ Operational (models auto-downloaded on first build) | 8503 / 9092 |
| user_intelligence | ✅ Operational | 8504 / 9093 |
| link_discovery | ✅ Operational | 8505 / 9094 |
| bulk_sender | ✅ Operational | 8506 / 9095 |
| dashboard_index | ✅ Operational | 8500 |
| processor-py | ⚠️ Legacy — not in docker-compose, can be removed |

---

## Bugs Fixed (2026-05-14)

| # | File | Issue | Fix |
|---|---|---|---|
| B1 | `docker-compose.yml` | `rabbitmq_data` volume defined but never mounted — messages lost on restart | Added `rabbitmq_data:/var/lib/rabbitmq` mount |
| B2 | `.env.template` | `COLLECTOR_DEDUP_TTL_HOURS` not a real config key (correct: `COLLECTOR_DEDUP_TTL_SECONDS`) | Renamed |
| B3 | `.env.template` | `DLIB_MODELS_PATH` not a real config key (correct: `FACE_MODELS_PATH`) | Renamed |
| B4 | `.env.template` | `MEDIA_ARCHIVAL_POLL_INTERVAL_SEC` not a real config key (correct: `MEDIA_ARCHIVAL_POLL_SECONDS`) | Renamed |
| B5 | `.env.template` | `FACE_QUALITY_THRESHOLD` / `VIDEO_MAX_FRAMES` are dead vars with no corresponding config fields | Removed |
| B6 | `.env.template` | Missing: `FACE_PROCESSING_BATCH_SIZE`, `FACE_POLL_SECONDS`, `VIDEO_NOTE_FRAME_RATE`, `MEDIA_ARCHIVAL_MAX_DOWNLOAD_RETRIES`, `MEDIA_REDOWNLOAD_INTERVAL_SECONDS` | Added |
| B7 | `shared/live_config.py` | `LINK_DISCOVERY_JOIN_DELAY_SECONDS`: default=120 exceeded max_value=60 — value could never be set via live config | Corrected `max_value` to 3600 |
| B8 | `services/face_recognition/Dockerfile` | No dlib model download step — `dlib_models` volume empty on first deploy, service looped indefinitely | Added `scripts/download_models.sh` + wget/bzip2 install |
| B9 | `services/processor-py/worker.py` | Signal handler used `create_task(stop())` — stop() could be cancelled before cleanup completed | Replaced with `shutdown_event` + `asyncio.create_task(start())` pattern |
| B10 | `services/wa-client-ts/src/index.ts` | `fetchLatestBaileysVersion()` called on every reconnect — wasted external API call | Cached in module-level `cachedVersion` |

---

## Remaining Decisions Needed

1. **processor-py removal**: The service duplicates `media_archival` + `face_recognition` functionality, is not in `docker-compose.yml`, and has broken relative imports. Recommend removing it. Requires explicit decision.

2. **dashboard_index/index.html**: Pre-generated file committed to repo. Should add to `.gitignore` or delete. Low priority.

---

## Architecture

```
wa-client-ts-1  ─┐
wa-client-ts-2  ─┤→ RabbitMQ/Redis → collector → media_archival
                  │                             → face_recognition
                  │                             → user_intelligence
                  │                             → link_discovery
                  │                             → bulk_sender
                  └→ PostgreSQL (pgvector)
```

- Config tuning: `python tools/config_cli.py list`
- Dashboards: http://localhost:8500
- Broker: RabbitMQ (exchange: `whatsapp.events`, DLQ: `dlq.failed`)
- Model download: runs automatically at `docker compose build face_recognition`
