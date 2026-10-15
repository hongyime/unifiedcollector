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
- All 31 Pinterest-focused checks and all 53 tests in `tests/collectors/test_native_platform_scrapers.py` pass. `git diff --check` is clean. Code is still uncommitted; live claim/load and deployment checks remain.
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
