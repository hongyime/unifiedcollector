# UnifiedCollector Agent State

Updated: 2026-08-15 14:25 UTC / 2026-08-15 22:25 SGT

Current task status: Docker recovered after reboot, website policy remains live, SpiderFoot `daily100` is applied and gate-clean, and the SpiderFoot sidecar is hardened for bounded parallel processing. Threads browser capture is the remaining blocker: the DB has recent historical Threads data, but the scraper Chrome/CDP profile is currently nonresponsive and source health is degraded.

Implemented in this slice:
- Scoped SpiderFoot optional-rollout gates to `spiderfoot,recon` only, so unrelated GitHub/TikTok/Lemon8/YouTube cooldowns no longer block SpiderFoot rollout.
- Applied SpiderFoot `daily100` live; the gate returned `can_proceed=true`, `recommended_action=advance_stage`, and queued 100 weak-lead recon targets.
- Added SpiderFoot sidecar workers with bounded count, per-worker SpiderFoot HOME/state isolation, process-group timeout cleanup, stale-claim reclaim tuning, and source-table policy support for `instagram_spider_queue`.
- Kept target-level SpiderFoot failures, including timeouts and invalid target values, from marking the whole `spiderfoot` source degraded.
- Raised Docker/WSL local resources outside git: WSL memory 10GB, swap 8GB, 8 CPUs, gradual memory reclaim; Docker Desktop Resource Saver disabled.
- Set ignored runtime env for SpiderFoot to 2 workers, 3 SpiderFoot threads, 180s target timeout, 30m stale reclaim, and `/tmp/spiderfoot-state`.

Verification completed:
- `python -m pytest tests\test_recon.py tests\test_recon_spiderfoot_service.py tests\core\test_optional_rollout.py tests\dashboard\test_coverage_api.py::test_optional_rollout_status_uses_guarded_monitor -q` passed 32 tests.
- Compile checks passed for touched recon/rollout/sidecar modules and tests.
- `docker compose -f docker\docker-compose.yml --profile recon config --quiet` passed.
- `collector_spiderfoot` was recreated and read back `WORKERS=2`, `THREADS=3`, `TIMEOUT=180`; container is healthy.
- Live optional-rollout status now returns `can_proceed=true`, `recommended_action=advance_stage`, `candidate_count=100`, `stop_reason_count=0`, `gate_sources=spiderfoot,recon`.
- Live recon queue after bounded run: completed 49, failed timeout 19, failed invalid target 1, in_progress 2, pending 53.
- Website allow rule still works in-container: `https://*.com.sg`, `http://*.com.sg`, and `http://*.com` allow expected paths; `https://*.com` is not allowed; `/admin/` and suffix-bleed hosts are blocked.
- Docker is ready with 35 containers up; all listed containers were healthy except `nebula-sync` at last snapshot.

Threads status:
- Live DB has `threads_posts=14142`, `threads_posts_24h=943`, `threads_posts_1h=21`, `threads media_items=38027`, and `threads media_items_24h=3479`.
- Threads source health is degraded: browser capture progress was over 3600s stale.
- Browser audit before Chrome repair showed one Threads tab, tab budget OK, but Threads main-world CDP eval timed out and no content-script isolated world was detected.
- Reloading the Threads tab did not restart capture. Restarting standard and recovered scraper Chrome profiles caused hung CDP endpoints on ports 9333/9334; the bad Chrome trees were killed to keep the machine usable. Next recovery should be an OS/user-session-level Chrome profile repair or clean scraper-profile relaunch once Windows releases the stuck CDP state.

Operational notes:
- Website policy text file remains `config/sources/website.url-policy.txt`. Use wildcard rules for normal domains/paths; regex rules are supported only as anchored `allow_regex:` / `block_regex:`.
- SpiderFoot remains weak-lead only. Do not promote recon observations to identity truth without independent hard corroboration.
