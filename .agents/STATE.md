# UnifiedCollector Agent State

Updated: 2026-08-14 10:21 UTC / 2026-08-14 18:21 SGT

Current task status: Website URL allow policy support is implemented, pushed, and live-container verified. SpiderFoot `daily100` rollout gate is live and was not applied because current source-health stop criteria require `stop_or_rollback`.

Implemented in this slice:
- Added checked-in `config/sources/website.url-policy.txt` with `allow https://*.com.sg`, `allow http://*.com.sg`, and `allow http://*.com`.
- Added `WEBSITE_URL_ALLOW`, `WEBSITE_URL_BLOCK`, and `WEBSITE_URL_POLICY_FILE` pass-through/defaults to the website service in Compose.
- Compose now defaults `WEBSITE_URL_ALLOW` to `https://*.com.sg,http://*.com.sg,http://*.com` so the runtime env is correct even when Compose interpolation does not read the repo-root ignored `.env`.
- Extended `URLFilter` with policy-file parsing, `allow`/`block` actions, `allow_regex`/`block_regex` anchored regex actions, `!pattern` block shorthand, and raw `regex:`/`re:` support.
- Host-only URL wildcard rules like `https://*.com.sg` now match all paths on matching hosts without suffix bleed.
- Block rules are evaluated before allow rules, so broad allowlists do not override explicit sensitive-path blocks.
- Website collector now loads `config/sources/website.url-policy.txt` by default when `WEBSITE_URL_POLICY_FILE` is unset.
- Added `daily100` as an accepted optional-rollout stage in the CLI and dashboard API.

Verification completed:
- Focused pytest suite passed for URL filter, website collector, optional rollout, and dashboard guarded rollout status.
- Compile checks passed for touched Collector modules/tests.
- `docker compose -f docker\docker-compose.yml config --quiet` passed.
- Direct parser readback confirmed `WEBSITE_URL_ALLOW=https://*.com.sg,http://*.com.sg,http://*.com`, path matches are allowed, suffix bleed is blocked, and a policy-file `/admin/` block wins over the broad allowlist.
- Rebuilt/recreated Collector `dashboard`, recreated `collector_website`, and verified both are healthy in Compose.
- Container env readback confirmed `WEBSITE_URL_ALLOW=https://*.com.sg,http://*.com.sg,http://*.com` and `WEBSITE_URL_POLICY_FILE=/app/config/sources/website.url-policy.txt`.
- Container parser readback confirmed SG HTTPS paths and `.com` HTTP paths are allowed, `/admin/` is blocked, and suffix bleed is blocked.
- Host `http://127.0.0.1:8700/health` returned 200.
- Host `http://127.0.0.1:8700/optional-rollout/status?feature=spiderfoot&stage=daily100` returned `can_proceed=false`, `recommended_action=stop_or_rollback`, `candidate_count=100`.

Operational notes:
- Text policy file format is line-based: blank/comment lines are ignored; use `allow <pattern>`, `block <pattern>`, `allow_regex:<anchored-regex>`, `block_regex:<anchored-regex>`, or `!<pattern>`.
- Raw regex must be anchored to `^http://`, `^https://`, or `^https?://`; use wildcard lines for normal domains and paths.
- Do not apply SpiderFoot rollout while current source-health stop criteria remain: latest `daily100` status includes GitHub API transport timeout, TikTok challenge/cooldowns, Lemon8 429, and YouTube quota/access 403 cooldowns.
