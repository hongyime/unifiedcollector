# UnifiedCollector Agent State

Updated: 2026-08-13 13:10 UTC / 21:10 SGT

Current task complete and pushed: added the Collector seen-target registry and surfaced its counters through coverage/dashboard APIs.

What changed:
- Added `collector_seen_targets` migration with canonical `(platform, target_type, target_key)` registry rows, evidence counts, source provenance, first/last seen timestamps, backfill timestamps, next-backfill hints, and progress status.
- Added `src.core.seen_targets` to populate the registry from existing Collector facts: `social_users`, `follow_edges`, `collection_targets`, platform profile tables, spider/profile queues, Threads/Facebook post authors, discovered links, and search results.
- Added `/seen/targets` dashboard API with optional bounded refresh and safe summary counters.
- Extended `collection_coverage_snapshots` and `/coverage/collectors` with seen-target totals: total, backfilled, pending, fresh, stale, and newly discovered.
- Kept `collection_targets` as the execution queue. Coverage snapshots read the seen registry by default; full registry refresh is explicit so normal coverage runs do not stall on all-source upserts.

Verification:
- Focused Collector tests passed: `python -m pytest tests\core\test_seen_targets.py tests\test_collection_coverage.py tests\dashboard\test_coverage_api.py -q` -> 9 passed.
- Syntax/config passed: `python -m py_compile src\core\seen_targets.py src\core\collection_coverage.py src\dashboard\api.py`; `docker compose -f docker\docker-compose.yml config --quiet`.
- Rebuilt/recreated `dashboard` and `scheduler`.
- Live `/seen/targets?source=instagram&limit=3&refresh=true` wrote and reported 6,385 Instagram seen targets: 1,795 backfilled, 237 pending, 427 fresh, 1,368 stale, 183 newly discovered.
- Live `coverage-snapshot --json` completed and wrote 32 snapshot rows; Instagram coverage includes the same seen counters.
- Live `/coverage/collectors` returns the new seen counters, and dashboard/scheduler containers are healthy.
- Implementation commit pushed: `0002f97e feat: add collector seen target registry`.

Next steps:
1. Populate additional platforms with explicit `/seen/targets?source=<platform>&refresh=true` refreshes or a bounded rollout job when desired.
