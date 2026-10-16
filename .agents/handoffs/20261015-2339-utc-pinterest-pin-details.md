# Pinterest pin detail handoff — 2026-10-15 23:39 UTC

## Objective

Continue the UnifiedCollector Full Plan. Improve Pinterest pin captions/descriptions, destination URLs, repin counts, and comment counts while preserving follow-first coverage and explicit verified-account exclusions. Do not create or navigate browser tabs; keep one extension-managed tab per social. Do not read or print cookie values.

## Current findings

- `pinterest_pins` already stores `description`, `destination_url`, `repin_count`, and `comment_count`.
- `_pinterest_page_kind()` accepts profile/board routes but rejects `/pin/<id>/`; `_parse_native_page()` only parses cards from profile/board pages.
- Pinterest native targets use `browser_native_scrape_queue`. Preferred followed-handle candidates are claimed before lower-priority public discovery. The seeder can add low-volume pin-detail targets after preferred candidates drain.
- Existing parsing drops pin cards with explicit verified-author evidence and keeps unknown badge state eligible. New detail parsing must keep that behavior and retain pin/board ownership only when supported by an existing verified page association.
- Search fix `bf3ffe12` is live. The 15-minute soak completed through 23:35:41 UTC; two dynamic terms advanced, 0 Search 429s, and documents/filetype dorks remain open.
- The local implementation now parses only numeric pin detail routes, filters by exact requested ID, enriches missing fields only from authenticated rows whose IDs also appeared anonymously, and drops explicit verified authors. Detail seeding is capped at 10 active rows, requires a same-platform following edge plus verified pin ownership, and runs only after preferred followed-handle work drains.
- Empty pin shells are rejected by the public-content gate; the route alone is not counted as public data.
- All 31 Pinterest-focused checks and all 53 tests in `tests/collectors/test_native_platform_scrapers.py` pass. `git diff --check` is clean. Commit `113bc3f0` is pushed to `main`; live claim/load and deployment checks remain.
- Deployment check at 23:45 UTC found no unexpired Pinterest claim and 30 running Compose services, but PostgreSQL CPU was 266.76%; defer the Pinterest-only restart until DB load settles. The service bind-mounts `src`, so no image rebuild is needed.

## Implementation plan

1. Add regression tests for numeric pin detail routes, exact-ID parsing, detail-field enrichment, verified-author exclusion, and bounded same-platform followed-pin queue seeding.
2. Parse only the requested pin from detail pages; use public detail data first and accept authenticated enrichment only for pin IDs also visible anonymously.
3. Seed a capped set of detail pages only from pins tied to a Pinterest `following` edge and `owner_verified=true`, after the preferred handle queue drains.
4. Run the focused native scraper tests and `git diff --check`; commit and push to `main` after review.
5. Reload only Pinterest when no native target claim is active; observe its output and rate-limit state without touching Chrome tabs.

## User decisions and guardrails

- Start Pinterest/Snapchat from platform-specific followed identities; broaden only after preferred work drains and capacity remains. Airbnb starts from followed identities' listing links.
- Explicit blue-check evidence excludes an account; unknown remains eligible.
- All Telegram groups and configured accounts are authorized, but four FloodWaits expire at 2026-10-16 06:22:07 UTC; do not restart Telegram before then.
- Do not touch Spider/SpiderFoot. Preserve one extension-managed tab per social.

- At 23:48 UTC the active claim had completed, but PostgreSQL was at 271.42% CPU; keep `collector_pinterest` running and recheck load before deployment.

- `collector_pinterest` restarted at 23:51:13 UTC after a zero-claim check, PostgreSQL at 69.42%, and no Pinterest rate-limit event in the prior 30 minutes. Runtime import confirms the new numeric pin route and detail seed SQL. Post-restart PostgreSQL sampled at 267.71%; monitor through the 15-minute soak ending 00:06:13 UTC Oct 16, with no further restarts while load is elevated.

- Early post-restart check at 23:53 UTC: one `pinterest_pin_details` target completed with no bounded log error/traceback/rate marker. Rolling 12h totals are 48 pins, 2 descriptions, and no destination or repin/comment counts. Continue the soak through 00:06:13 UTC.

- Four-minute soak recheck at 23:55 UTC: one detail row completed, no active claim or error/traceback/rate marker. Pinterest output remains 48 pins/2 described/0 destinations/repin counts/comment counts. PostgreSQL sampled at 394.03%; do not restart other services.
