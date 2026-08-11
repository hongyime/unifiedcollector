# UnifiedCollector Agent State

Updated: 2026-08-11 15:41 SGT

Current task: continue hardening UnifiedCollector toward robust collection/recon operations.

Recent completed work:
- SpiderFoot recon sidecar is live and verified under the `recon` Compose profile.
- Dashboard browser-health optional timeout noise was fixed, tested, rebuilt, deployed, committed, and pushed as `0ec39ea0 fix: suppress optional browser health noise`.

In-progress work:
- Completing dirty recon changes that add collector-derived recon seeding:
  - `src/core/recon_seed.py`
  - `src/core/recon_spiderfoot.py`
  - `src/main.py`
  - `tests/test_recon.py`

Current evidence:
- `python -m pytest tests/test_recon.py tests/test_recon_spiderfoot_service.py -q` passed with 17 tests.
- Live Docker core services checked recently: dashboard, spiderfoot, postgres, realtime_feed, watchdog are up/healthy.
- `docker exec unifiedcollector_collector python -m src.main recon-seed --dry-run --json --limit 20 --per-source-limit 5` worked against live DB and returned 20 candidates.
- A broken dirty Docker change was found and corrected: SpiderFoot must use `src.recon_spiderfoot_service`, not `src.main`, because the slim SpiderFoot image does not install full collector dependencies.
- `.gitignore` now allows `.agents/` so this state can be committed as required by AGENTS.md.
- Lazy `src.core` exports were verified with `from src.core import check_drive, ProfilePhotoTracker`.
- `python -m pytest tests/test_recon.py tests/test_recon_spiderfoot_service.py tests/dashboard/test_recon_api.py tests/verify_clean_boot.py -q` passed with 19 tests.
- `python -m pytest tests/dashboard/test_extension_health.py tests/dashboard/test_source_matrix.py -q` passed after browser optional diagnostic suppression.
- Dashboard was rebuilt/restarted and live `/health?include_sources=true` returned `status=ok`, `source_issues=0`, no browser diagnostic errors, and active browser content.
- `src.recon_seed_service` works as an optional one-shot entrypoint and redacts dry-run samples.

Next steps:
1. Commit and push the dashboard health-noise fix.
2. Commit and push the collector-derived recon seed work.
3. Re-check git cleanliness and core service health.
