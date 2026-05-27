# Unified Collector — Production Schedule

**Last updated:** 2026-05-26
**Status:** target schedule for once unified collector is fully ported.
**Scheduler:** APScheduler in `scheduler` container; jobs publish to RabbitMQ
`collection.start` exchange; collectors consume and ACK on completion.

---

## Tier 1 — Continuous / High-frequency (low ban risk)

| Collector | Trigger | Cadence | Notes |
|-----------|---------|---------|-------|
| matrix (Beeper) | persistent /sync | continuous | long-poll, no cron |
| whatsapp | event-driven via wa_bridge | continuous | Baileys → RabbitMQ |
| telegram | APScheduler | every 5 min | 4 accounts in parallel, FloodWait-aware |
| github | APScheduler | every 30 min | 5000/hr PAT budget |
| youtube | APScheduler | every 1 hour | 10k API units/day |
| strava | APScheduler | every 2 hours | own + followed activities only |
| website | APScheduler | every 1 hour | depth-priority queue self-throttles |
| search | APScheduler | every 6 hours | Tor circuit rotation expensive |

---

## Tier 2 — Cautious (high ban risk)

| Collector | Trigger | Cadence | Notes |
|-----------|---------|---------|-------|
| instagram | APScheduler | every 4 hours | 5 accounts, sliding window, emergency cooldown gate |
| tiktok | APScheduler | every 3 hours | Cookie-based, fingerprint volatile |
| lemon8 | APScheduler | every 6 hours | ByteDance family — same risk as TikTok |

---

## Tier 3 — Maintenance (system-level)

| Job | Cadence | Time (SGT) | Purpose |
|-----|---------|------------|---------|
| telegram_membership_refresh | daily | 03:00 | iter_dialogs across 4 accounts |
| spider_queue_pruner | daily | 04:00 | drops queue entries >30d old |
| backup_postgres | daily | 02:00 | already running |
| backup_z_drive_inventory | daily | 05:00 | manifest of media files |
| disk_usage_check | every 6h | — | alerts if Z: > 90% |
| dlq_consumer | continuous | — | already running |
| face_processor | every 15 min | — | processes media queue |
| url_filter_refresh | weekly | Mon 01:00 | block-list update |
| parity_matrix_regen | weekly | Sun 02:00 | full repo scan |

---

## Tier 4 — Manual / Operator-triggered

| Job | When | Notes |
|-----|------|-------|
| beeper_backfill_runner | one-shot | initial historical pull of 2055 rooms |
| github_full_spider | as-needed | seed expansion from toolkit DB |
| toolkit_archive_cleaner | post-95% parity | archives a toolkit folder |

---

## Concurrency Rules

1. Max 1 instance of each collector at a time. Lock via Redis `SETNX` with TTL.
2. Instagram + TikTok + Lemon8 must NEVER run simultaneously across accounts that
   share a public IP — pattern detection risk.
3. Strava + Lemon8 OK to overlap (different exit IPs / SDKs).
4. matrix_collector + native telegram + native whatsapp run continuously regardless
   of other collectors.
5. Backup jobs (02:00-05:00) must NOT overlap with collectors except continuous ones.

---

## Failure / DLQ Behaviour

- Failed runs → DLQ exchange. dlq_consumer service auto-retries with exponential
  backoff up to 5 attempts.
- If a collector hits 3 consecutive 429s → emergency cooldown for that account
  (per-account, via human_rate_limiter post-Wave-0 refactor).
- If a Beeper bridge desyncs → matrix_collector logs the gap, continues. Native
  collector covers WA + TG so we don't lose data on bridge outage.

---

## Health Check Endpoints

| Service | Endpoint | Expected |
|---------|----------|----------|
| collector | `/health` | 200 + last-success timestamps per source |
| scheduler | `/health` | 200 + APScheduler job count |
| matrix_collector | `/health` | 200 + last_sync_at |
| dashboard | `/health` | 200 |
| postgres | `pg_isready` | accepting connections |
| rabbitmq | `:15672/api/aliveness-test/%2F` | 200 |
| redis | `redis-cli ping` | PONG |
| tor | `:9051` AUTHENTICATE | accept |

Dashboard surfaces all of these as green/yellow/red badges.
