# UnifiedCollector Agent State

Updated: 2026-08-14 10:02 UTC / 2026-08-14 18:02 SGT

Current task status: Website URL allow policy support is implemented, the user-provided allow list is present in local ignored `.env`, and focused tests pass. SpiderFoot optional rollout was checked but not applied because the latest live rollout reports had active stop criteria; Docker Desktop is currently not responding well enough for a fresh container recreate or live dashboard readback.

Implemented in this slice:
- Added checked-in `config/sources/website.url-policy.txt` with `allow https://*.com.sg`, `allow http://*.com.sg`, and `allow http://*.com`.
- Added `WEBSITE_URL_ALLOW`, `WEBSITE_URL_BLOCK`, and `WEBSITE_URL_POLICY_FILE` pass-through/defaults to the website service in Compose.
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

Operational notes:
- Text policy file format is line-based: blank/comment lines are ignored; use `allow <pattern>`, `block <pattern>`, `allow_regex:<anchored-regex>`, `block_regex:<anchored-regex>`, or `!<pattern>`.
- Raw regex must be anchored to `^http://`, `^https://`, or `^https?://`; use wildcard lines for normal domains and paths.
- Do not apply SpiderFoot rollout while recent source-health stop criteria remain. Prior live dry-run/five reports showed GitHub transport timeout, TikTok challenge/cooldowns, Lemon8 429, and YouTube quota/access cooldowns.
- Next runtime step after Docker Desktop recovers: rebuild/recreate `collector_website` and `dashboard`, then verify `/health` and `/optional-rollout/status?feature=spiderfoot&stage=daily100`.
