# UnifiedCollector Agent State

Updated: 2026-08-13 08:13 UTC / 16:13 SGT

Current task complete pending commit/push: browser tab hygiene and Instagram media revisit draining were hardened.

What changed:
- Added `scripts/cdp_ext_tabs.py` so manual/debug extension helpers reuse the primary `pkmdmcklnjdeocoeigmlakhomhhcpafb/tabs.html` control tab instead of spawning duplicate extension tabs.
- Updated extension helper scripts to use the control-tab helper.
- Hardened cleanup to close stale old-ID extension control pages, duplicate `tabs.html` pages, `about:blank`, `chrome://newtab/`, and empty-url loading tabs.
- Fixed `browser-tab-maintenance.ps1` so only the primary extension ID is considered a valid control tab; old extension IDs are closed instead of kept.
- Wired Instagram into the generic browser media revisit flow in `extension/content.js`.
- Added Instagram detail-page DOM media harvesting with post-url tagging for `/p`, `/reel`, `/reels`, `/tv`, and `/stories` URLs.
- Added backend fallback for Instagram revisit rows: synthesize an openable post URL from numeric media IDs and skip queueing CDN-only items that still have no openable post URL.
- Bumped extension manifest and Compose expected version to `1.23.69`.

Verification:
- Focused tests passed: `python -m pytest tests\extension\test_extension_bundle_static.py tests\tools\test_browser_maintenance_scripts.py tests\bridges\test_ig_ingest_vault.py -q`.
- Syntax/config passed: `node --check extension\content.js`, `node --check extension\background.js`, Python `py_compile` for touched helpers and `src\bridges\ig_ingest.py`, and `docker compose -f docker\docker-compose.yml config --quiet`.
- Rebuilt/recreated `ig_ingest`, `dashboard`, and `scheduler`.
- Hard-reloaded extension; post-reload manifest and service-worker ping confirmed `1.23.69`.
- Live dashboard health returned `status=ok`, `source_issues=[]`, expected extension `1.23.69`, browser issues `[]`, and active/content platforms included Instagram, Threads, TikTok, Facebook, X, Strava.
- Live CDP page target readback showed exactly seven page targets: one extension control page plus Instagram, Threads, TikTok, X, Facebook, and Strava; duplicate URL groups were empty.
- Live Chrome tab readback showed all tabs unpinned and no persistent blank/about:blank tab.
- Instagram media ingest after reload wrote fresh `1.23.69` media events. Revisit queue is no longer all-pending; invalid rows started failing as `missing_revisit_url`, and new code prevents/synthesizes those cases going forward.

Next steps:
1. Commit and push focused changes.
2. Continue watching the old Instagram/Threads revisit backlog drain over time; the backlog is large and will not finish instantly.
