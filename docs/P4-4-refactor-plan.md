# UNIFIEDCOLLECTOR — Consolidated "Finish Everything" Plan (one-shot)

Single execution plan folding in: P4-4 (refactor giants), the mid-tier collector
debt, P4-3 (dir cleanup), and the 5 residual blockers. Sequenced by risk.
Items are tagged [AUTO] (agent executes autonomously, ralph-style) or
[NEEDS-YOU] (requires a human action the agent cannot do).

Evidence basis (measured this session):
- Collector LOC: instagram 2663, telegram 2617 (the giants); lemon8 1665,
  youtube 1376, strava 1349, website 1182, github 1136, tiktok 1132, search 1015
  (mid-tier, mostly single-concern); whatsapp 759, beeper 694 (fine).
- archive/ and telegramcollector/ have ZERO real python imports from src/ (only
  doc-comment "ported from" provenance). whatsappcollector/services/wa-client-ts
  IS the build context for both wa_bridge containers (compose lines 261/293) = LIVE.

================================================================================
PHASE A — QUICK WINS / RESIDUAL BLOCKERS (do first; hours, not days)
================================================================================

A1 [AUTO] collection_targets dead-entry prune (blocker #5)
  Repeated 404s (lemon8 streets.of.singapore, youtube UC-... not found) burn
  cycles. Add a periodic GC: mark targets 'dead' after N consecutive failures,
  skip them in the worker loop, log a weekly summary.
  - Add failure_count + last_error columns to collection_targets (migration via
    the P0 runner + ledger).
  - In BaseCollector / worker: on collect failure increment failure_count; on
    success reset to 0. Skip targets with failure_count >= DEAD_TARGET_THRESHOLD
    (env, default 10) and status='dead'.
  - Scheduler GC (reuse the _gc_* hourly pattern from P3-7): log N dead targets.
  - Verify: throwaway-DB DDL test, then watch the worker skip a known-dead target.

A2 [AUTO] youtube hard-timeout recovery — actually prove it (blocker #1)
  P3-1 isolation is proven; the 660s outer_timeout release is only code-read.
  - Drop YT_DLP_TIMEOUT (or the youtube collector's timeout arg) to ~60s via env
    in the collector_youtube service so outer_timeout becomes ~120s, observable.
  - Let one wedge happen; confirm the log line
    "subprocess_downloader: hard timeout (...s) - abandoning wedged subprocess"
    fires and the youtube worker resumes (or the container healthcheck recycles).
  - Restore the timeout to production value. Document the proven recovery in the
    commit. (restart:unless-stopped already self-heals regardless.)

A3 [AUTO] P4-3 finish the cleanup safely (blockers #2 + #3)
  Confirmed: archive/ + telegramcollector/ have no live imports; only doc-comment
  provenance. whatsappcollector/ is LIVE infra — DO NOT TOUCH.
  - Relocate (do NOT hard-delete) archive/ and telegramcollector/ OUT of the repo
    tree to C:\unifiedcollector_reference\ (preserves the "ported from" source the
    comments reference, removes it from repo tooling scope).
  - Add to .gitignore: archive/, telegramcollector/ (in case any stragglers).
  - tmp/, scratch fix_*.py: confirm gitignored (done P1-4 era) else add.
  - Re-run the repo-wide tooling (git ls-files, search) and confirm it no longer
    times out (>180s -> fast). That timeout was the symptom; this is the fix.
  - Verify: full stack still boots + collects after relocation (nothing imported it).

A4 [NEEDS-YOU] register the backup scheduled task (blocker #4)
  Agent cannot self-elevate. ONE elevated PowerShell command:
    powershell -ExecutionPolicy Bypass -File scripts\register-backup-task.ps1
  Then confirm: Get-ScheduledTask -TaskName UnifiedCollectorBackup
  Restore path already proven (P3-5). This only activates the daily 03:30 schedule.
  [AUTO follow-up] After you register it, agent runs Start-ScheduledTask once and
  checks backups\backup_task.log + a fresh dump appears.

================================================================================
PHASE B — MID-TIER COLLECTOR DEBT (shared extraction, NOT per-file packaging)
================================================================================
Rationale: lemon8/youtube/strava/website/github/tiktok/search are 1000-1700 LOC
SINGLE-CONCERN files. Packaging each is churn. The real debt is DUPLICATED logic
across all collectors. Extract the shared patterns into src/core/ so EVERY
collector thins out at once — higher leverage than splitting one big file.

B1 [AUTO] Audit duplication across all 9 mid+large collectors
  grep for repeated patterns: cookie/proxy handling, retry/backoff, pagination
  loops, httpx client construction, rate-limit sleeps, media-write boilerplate.
  Produce a duplication map (which helpers recur in >=3 collectors).

B2 [AUTO] Extract the top shared helpers into src/core/ (one per commit)
  Likely candidates (confirm via B1): http transport factory, paginated-fetch
  helper, backoff/retry decorator, proxy resolver. Each: extract, unit-test
  (pure where possible), migrate call sites in 1-2 collectors, CI gate, deploy,
  watch a cycle, commit. Then roll to remaining collectors.
  NOTE: keep behavior identical; this is de-duplication, not redesign.

================================================================================
PHASE C — P4-4 REFACTOR THE TWO GIANTS (highest risk; last)
================================================================================
Only telegram.py + instagram.py. They mix 6 concerns each; the mid-tier doesn't.
Behavior-preserving extraction into per-platform packages. Public names +
import paths stay stable (re-export from package __init__) so worker --source X,
get_collector(), and CI never break. One concern per commit; CI gate + one live
collection cycle watched after EACH; deploy before the next.

Test harness FIRST (the safety net):
  - Capture 2-3 real API payloads per platform to tests/fixtures/ (PII-scrubbed).
  - Unit-test the pure parsers against them to LOCK behavior before moving code.
  - Persist tests use the throwaway-DB pattern proven in P3-5.

telegram.py extraction order (low -> high risk):
  1. PURE -> telegram/parse.py: _detect_message_type, _extract_file_info; errors.py
  2. PERSIST -> telegram/persist.py: _upsert_chat/_sender/_message/_user_full,
     _write_realtime_message, _capture_message_reaction_counts, _capture_poll
  3. SPIDER -> telegram/spider.py: _spider_enqueue, _process_spider_queue,
     _spider_discussion_group, _enqueue_forward_edges, _enumerate_reactors_and_enqueue
  4. MEDIA -> telegram/media.py: download_media, _handle_photo, _handle_document,
     download_message_media, _collect_profile_photo
  5. REALTIME -> telegram/realtime.py: _on_new_message/_edited/_deleted/_chat_action/
     _user_update/_raw_reactions, collect_realtime
  (TelegramWorker auth/session already a separate class — leave as-is, good seam.)

instagram.py extraction order (low -> high risk):
  1. PURE -> instagram/parse.py: _parse_browser_cookies, _extract_post_edges_from_payload,
     _time_of_day_multiplier, _detect_challenge_kind
  2. PERSIST -> instagram/persist.py: _upsert_profile, _upsert_post, _persist_relationships
  3. TRANSPORT -> instagram/transport.py: _get_tls_rotator, _get_curl_cffi_kwargs,
     _headers, _get_proxy, _build_playwright_storage_state, _playwright_fetch_url
  4. LIMITS -> instagram/limits.py: _check_daily_quota, _record_daily_action,
     _micro_pause, _content_aware_delay, _handle_rate_limit, _quota_*
  5. MEDIA -> instagram/media.py: download_media, _download_node, _collect_stories,
     _collect_highlights
  6. AUTH (RISKIEST, LAST) -> instagram/auth.py: _login_account, _try_cookie_login,
     _password_login, _resolve_2fa_code, _consume_2fa_dropfile, _login_from_cookies,
     _resolve_challenge_code, _consume_challenge_dropfile, _build_ig_session_capsule,
     _is_session_alive, _check_session_age, _save_session_meta
     -> schedule when a live login cycle can be watched.

================================================================================
PER-STEP CHECKLIST (applies to every code change in B and C)
================================================================================
[ ] ast.parse OK
[ ] ruff E9/F821 clean (the CI gate)
[ ] import smoke: python -c "from src.collectors import get_collector; get_collector('<src>')"
[ ] docker cp + restart affected collector (or rebuild if many files)
[ ] watch ONE full collection cycle; confirm media still writes
[ ] one concern per commit; push; THEN next concern

================================================================================
ONE-SHOT EXECUTION ORDER
================================================================================
1. A1 dead-target prune       [AUTO]
2. A2 prove youtube timeout    [AUTO]
3. A3 relocate dead dirs       [AUTO]
4. A4 register backup task     [NEEDS-YOU one elevated cmd] -> then [AUTO] verify
5. B1 duplication audit        [AUTO]
6. B2 extract shared core      [AUTO, iterative]
7. C  refactor giants          [AUTO, iterative, test-harness-first, watch each cycle]

Risk gates: A* are safe + fast. B is medium (shared-core regressions hit multiple
collectors — deploy + watch after each). C is the only high-risk phase; never
batch its commits, always watch a live cycle between concerns.
