# Unified Collector — Port Plan

Generated from PARITY_MATRIX.json. Priority = how much toolkit code is
NOT yet reflected in `src/collectors/<src>.py` (toolkit_loc / unified_loc).


## Priority order

| Rank | Source | Toolkit LOC | Unified LOC | Ratio | Status |
|-----:|--------|-----------:|-----------:|------:|--------|
| 1 | **instagram** | 53,746 | 1,235 | 43.5x | MASSIVE underport |
| 2 | **telegram** | 22,181 | 609 | 36.4x | MASSIVE underport |
| 3 | **search** | 5,282 | 159 | 33.2x | MASSIVE underport |
| 4 | **whatsapp** | 20,496 | 623 | 32.9x | MASSIVE underport |
| 5 | **tiktok** | 15,245 | 616 | 24.7x | heavy underport |
| 6 | **strava** | 11,874 | 575 | 20.7x | heavy underport |
| 7 | **lemon8** | 16,814 | 1,262 | 13.3x | moderate underport |
| 8 | **github** | 3,266 | 446 | 7.3x | light underport |
| 9 | **youtube** | 0 | 763 | 0.0x | near parity |

## Cross-cutting features (port to `src/core/`, reuse N times)

These features appear missing across multiple sources. Implementing each
ONCE in `src/core/` yields N×LOC of avoided duplication.

| Feature | Missing in | Sources |
|---------|-----------:|---------|
| `media-download` | 7 | github, instagram, lemon8, strava, telegram, tiktok, whatsapp |
| `rate-limit` | 5 | instagram, lemon8, search, strava, tiktok |
| `dedupe/hash` | 5 | github, instagram, lemon8, telegram, tiktok |
| `account-pool` | 4 | instagram, lemon8, telegram, tiktok |
| `circuit-breaker` | 2 | telegram, whatsapp |
| `auth/session` | 1 | instagram |
| `spider/discover` | 1 | github |
| `tor-proxy` | 1 | search |

## Recommended sequencing

**Wave 1 — port cross-cutting features once into `src/core/`:**

- `AccountPool` enhancements (cooldown manager, daily quota tracking)
  — already partially present, extend with quota.
- `AdaptiveRateLimiter` (success/failure-driven backoff)
  — currently absent; replicate from instagram/strava/tiktok/lemon8 toolkits
  (4 separate copies → 1 canonical).
- `BrowserDownloader` (gallery-dl + cookie injection wrapper)
  — present in tiktok/instagram toolkits, extend `core/`.

**Wave 2 — port per-source features in priority order:**

1. **instagram** — see per-source candidate list in PARITY_MATRIX.md

2. **telegram** — see per-source candidate list in PARITY_MATRIX.md

3. **search** — see per-source candidate list in PARITY_MATRIX.md

4. **whatsapp** — see per-source candidate list in PARITY_MATRIX.md

5. **tiktok** — see per-source candidate list in PARITY_MATRIX.md


**Wave 3 — decommission archived toolkits** once unified collector reaches
≥80% feature parity for that source (manual sign-off per source).


## Caveats

- The 'missing' substring heuristic is ~70% accurate. Symbols may exist
  under different names. Manual review required before deletion.

- `uncategorized` bucket dominates several sources because keyword bucketer
  is conservative. Inspect by hand for true features vs. plumbing.

- Test files were not filtered out — `Test*` classes inflate counts for
  tiktok/whatsapp; treat those as 'has good test coverage in toolkit'.

- youtube has no toolkit folder (built directly in unified) — already complete.

- github (3.3k LOC) and lemon8 (16.8k LOC) are smallest deltas — start here
  to build muscle memory, then tackle instagram/whatsapp/telegram (the heavy ones).
