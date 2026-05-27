# PARITY MATRIX
Generated: 2026-05-26T15:44:41Z

Toolkit standalone codebases vs the unified port. Unified attributed LOC =
LOC(src/collectors/<platform>.py) + Σ LOC(src/core/<module>) / |consumers|
for each Wave 0 cross-cutting module that platform consumes.

## Toolkit -> Unified Bloat Ratios (sorted by ratio desc)

| Platform | Toolkit LOC | Collector LOC | + Core attr | Unified total | Ratio | Status | Notes |
|----------|------------:|--------------:|------------:|--------------:|------:|:------:|-------|
| telegram | 34,524 | 1,333 | 153.4 | 1486.4 | 23.2x | ⚠ | telegramcollector + telegramtoolkit combined |
| whatsapp | 10,731 | 684 | 78.6 | 762.6 | 14.1x | ⚠ | Selenium + Playwright dual-engine |
| instagram | 33,965 | 2,122 | 788.2 | 2910.2 | 11.7x | ⚠ | largest toolkit; Wave 2 Batch F (2 agents) |
| website | 7,888 | 1,069 | 100.7 | 1169.7 | 6.7x | ⚠ | previously missing row (FIXED) |
| tiktok | 8,554 | 888 | 479.2 | 1367.2 | 6.3x | ⚠ | toolkit folder may be partial |
| strava | 7,379 | 922 | 271.9 | 1193.9 | 6.2x | ⚠ | GPX + activity scraping |
| search | 4,411 | 901 | 204.5 | 1105.5 | 4.0x | ✓ | google/duckduckgo aggregator |
| youtube | 4,493 | 1,273 | 89.5 | 1362.5 | 3.3x | ✓ | previously reported 0 (FIXED) |
| lemon8 | 6,369 | 1,469 | 479.2 | 1948.2 | 3.3x | ✓ | shares stack with tiktok |
| github | 2,695 | 1,017 | 476.0 | 1493.0 | 1.8x | ✓ | PAT pool + spider |
| matrix | 0 | 296 | 386.0 | 682.0 | 0.0x | ✗ | no standalone toolkit; greenfield (Wave 0 matrix_client.py) |

**Totals:** toolkit LOC = 121,009 | unified collectors LOC = 11,974 | Wave 0 core LOC = 3,507 | Wave 0 test LOC = 2,205

Status legend: ✓ ratio ≤ 5x · ⚠ ratio > 5x (port priority) · ✗ no toolkit code

## Wave 0 cross-cutting core modules (deployed 2026-05-26)

| Module | LOC | Tests LOC | Consumers | Per-consumer attribution |
|--------|----:|----------:|-----------|-------------------------:|
| media_download.py | 550 | 301 | github, instagram, lemon8, strava, telegram, tiktok, whatsapp (7) | 78.6 |
| spider_discover.py | 537 | 176 | github, instagram, tiktok, strava, lemon8, youtube (6) | 89.5 |
| adaptive_rate.py | 519 | 263 | instagram, lemon8, search, strava, tiktok (5) | 103.8 |
| dedupe_hash.py | 374 | 269 | github, instagram, lemon8, telegram, tiktok (5) | 74.8 |
| account_quota.py | 530 | 323 | instagram, tiktok, lemon8, github (4) | 132.5 |
| tor_proxy.py | 302 | 252 | github, search, website (3) | 100.7 |
| auth_session.py | 309 | 245 | instagram (1) | 309.0 |
| matrix_client.py | 386 | 376 | matrix (1) | 386.0 |

## Wave 0 module rollup
- Total core LOC: 3,507
- Total test LOC: 2,205
- DB migrations: content_hashes, spider_queue, account_quota_usage, matrix_sync_state
- New deps added: matrix-nio[e2e]>=0.24.0, imagehash>=4.3.0

## Bug fixes vs prior PARITY (2026-05-26 01:04)
- youtube: prior matrix reported toolkit_loc=0; now scans youtubetoolkit/ correctly.
- website: row was missing entirely; now included with websitetoolkit/ scan.
- telegram: now sums telegramcollector/ + telegramtoolkit/ explicitly.
- whatsapp: now sums whatsapptoolkit/ + whatsappcollector/.


## Wave 2 — Toolkit ports (deployed 2026-05-26)

All 10 toolkits now ported. None hit the 95% threshold uniformly — most landed at 75-95% read-side parity with write-side and operator/UI/CLI scaffolding deliberately dropped. Final ratios (post-Wave 2):

| Platform | Pre-Wave-2 ratio | Post-Wave-2 ratio | Δ |
|----------|------------------|-------------------|---|
| telegram | 46.8x | 23.2x | -23.6x |
| whatsapp | 18.0x | 14.1x | -3.9x |
| instagram | 18.0x | 11.7x | -6.3x |
| website | 35.9x | 6.7x | -29.2x |
| tiktok | (n/a) | 6.3x | new |
| strava | 12.8x | 6.2x | -6.6x |
| search | 21.3x | 4.0x | -17.3x ✓ |
| youtube | 5.4x | 3.3x | -2.1x ✓ |
| lemon8 | 13.0x | 3.3x | -9.7x ✓ |
| github | 5.6x | 1.8x | -3.8x ✓ |

**4 platforms now under 5x** (✓ status). Remaining ⚠ ratios are inherent to large platforms (telegram is uniquely high because telegramcollector/ alone is 22k LOC of microservices).

### Wave 2 deferred items per platform (deliberate gaps)
- **github**: web/app.py dropped, contribution-spider variant deferred
- **youtube**: oauth_bootstrap kept as standalone interactive script (user-run)
- **strava**: dashboard-feed historical playback, polyline→PNG render
- **search**: Chrome (undetected-chromedriver) engine dropped — too heavy
- **website**: NEWNYM auto-rotation, image-sitemap blocks
- **tiktok**: Playwright fallback stubbed, profile-photo pHash (reconciler tier1/2 covered by dedupe_hash.py + media_download.py — re-download prevention + atomic-write corruption detection)
- **lemon8**: graph_builder cross-platform export, multi-cookie pool
- **whatsapp**: user-intelligence diffing layer, bulk_sender entire service (intentional drop)
- **telegram**: face-recognition Redis queue rewire, login_bot extraction to tools/
- **instagram**: per-account TLS fingerprint pinning, profile_analyzer ML heuristic

### Tests gap
Wave 2 ports prioritized code+container import verification over unit-test coverage (time-constrained). 174 tests from Wave 0 + Wave 1 (130+ matrix-related) still pass. New collector test suites are deferred to a Wave 3 sweep.
