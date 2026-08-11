# UnifiedCollector Agent State

Updated: 2026-08-11 16:14 SGT

Current task: SpiderFoot recon operational setup completed safely; continue broader collector monitoring as needed.

Recent completed work:
- SpiderFoot recon sidecar is live and verified under the `recon` Compose profile.
- Dashboard browser-health optional timeout noise was fixed, tested, rebuilt, deployed, committed, and pushed as `0ec39ea0 fix: suppress optional browser health noise`.
- Additional browser optional diagnostic suppression was committed and pushed as `531298f7 fix: suppress optional browser diagnostics`.
- Collector-derived recon seeding was committed and pushed as `70212a23 feat: seed collector recon targets`.

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
- Final live check after push: dashboard/spiderfoot/postgres/realtime_feed/watchdog and collector services are healthy; `/health?include_sources=true` returned `status=ok`, `source_issues=0`, and no browser diagnostic errors.
- WhatsApp partial bridge health was patched so one ready bridge plus one optional empty QR slot keeps WhatsApp source status `live` instead of making `/health` degraded.
- Recon observation storage now uses `value_hash` for idempotent SpiderFoot upserts; live DB migration `20260811_fix_recon_observation_value_hash.sql` is applied with checksum `f37e6c38e7ae`.
- Collector-derived username recon targets are scoped to `RECON_USERNAME_MODULES` (`sfp_accounts` by default) so username targets do not run DNS/WHOIS modules.
- Live SpiderFoot sidecar processed a target after the value-hash migration and wrote 44 observations.
- Committed and pushed `54c69a7e fix: harden spiderfoot recon observations`.
- Final live check: worktree clean, `unifiedcollector_spiderfoot` healthy, `/health?include_sources=true` status `ok` with 0 source issues, and all 106 recon observations had `value_hash` populated.

Next steps:
1. Continue broader collector robustness audit from current live health.
2. Facebook browser content was the latest remaining source issue after the WhatsApp fix; inspect extension/browser collection before suppressing anything.
3. Watch DB lock pressure from long GitHub COPY/backfill work; recon commands may log deferred base-schema migration while backup/backfill holds locks, but runtime recon operations still complete.
