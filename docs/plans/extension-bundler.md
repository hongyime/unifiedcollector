# Plan: Chrome MV3 extension bundler (esbuild)

Addresses **FE-002**.

## 1. Context — current state

- `extension/` is un-bundled hand-written JS:
  | File | Bytes | LOC (approx) |
  |---|---|---|
  | `content.js` | 178,433 | 4,062 |
  | `background.js` | 134,412 | 2,978 |
  | `inject.js` | 49,697 | ~1,000 |
  | `tabs.js` | 12,760 | ~300 |
  | `popup.js` | 5,269 | ~120 |
  | `platforms.js` | 2,674 | ~30 |
  | `manifest.json` | 2,205 | — |
- No `package.json`, no build step, no source maps.
- `manifest.json` (MV3) declares:
  - Service worker: `background.js`
  - Content scripts: `content.js` on IG/TikTok/Lemon8/X/Twitter/Threads/Facebook/Strava; `inject.js` in MAIN world on the same set
  - Options page: `tabs.html` (loads `tabs.js`)
  - Popup: `popup.html` (loads `popup.js`)
- `platforms.js` and `background.js` **both** declare `globalThis.UC_PLATFORMS` — duplicated array, drift risk. `background.js` has an inline comment (~line 54) explicitly justifying the duplication:
  > "A failed importScripts() during MV3 startup prevents every alarm/listener from registering, so the background worker owns its platform registry directly."
- `content.js` and `background.js` share several concerns un-abstracted: throttle wall logic, log ring buffer, ingest HTTP client, page-recovery state.
- Static tests exist: `tests/extension/test_extension_bundle_static.py` (34,679 B) — parses the raw JS today.

## 2. Motivation

- **File size**: 4,062-LOC `content.js` is hard to navigate and review.
- **Code duplication**: `UC_PLATFORMS` copy-paste; log helpers, ingest URL constants, throttle-wall functions duplicated. Drift is documented (comment above admits it).
- **No shared modules**: content ↔ background overlap is un-factored. A bug fix in one is easily forgotten in the other.
- **No TypeScript**: shape errors (wrong platform object key, misspelled action name) surface only in Chrome DevTools at runtime.
- **No dead-code elimination / minification**: extension ships larger than necessary; slower parse time on service-worker cold start.
- **No source maps**: DevTools stack traces cite a single 4k-LOC line; hard to debug.
- **CI drift**: `test_extension_bundle_static.py` already parses the raw JS. A bundler produces a canonical, deterministic output that CI can hash-check.
- **npm ecosystem access**: Currently vendoring anything (a lightweight schema validator, a phone-parser) means committing source. A bundler unlocks the npm ecosystem safely.

## 3. Target end state

```
extension/
  package.json              # esbuild + typescript devDependencies
  esbuild.config.mjs        # 5 entry points → dist/
  tsconfig.json             # allowJs: true, checkJs: false (progressive TS adoption)
  manifest.json             # references dist/<name>.js
  src/
    content.ts              # ex-content.js
    background.ts           # ex-background.js
    inject.ts               # ex-inject.js
    popup.ts                # ex-popup.js
    tabs.ts                 # ex-tabs.js
    shared/
      platforms.ts          # single source of truth for UC_PLATFORMS
      throttle.ts           # persistent throttle wall
      log.ts                # ring buffer log
      ingest_client.ts      # HTTP client for /social/* endpoints
      cooldown.ts           # anti-ban cooldown sync with ig_ingest /social/ig_cooldown
      storage_helpers.ts    # ls*/chrome.storage.local wrappers
  dist/                     # committed for Chrome Web Store reproducibility
    content.js
    content.js.map
    background.js
    background.js.map
    inject.js
    popup.js
    tabs.js
  popup.html                # unchanged; points to dist/popup.js
  tabs.html                 # unchanged; points to dist/tabs.js
  icons/                    # unchanged
```

**Bundler choice: esbuild.** Rationale:
- MV3 CSP forbids `eval` and `new Function`; esbuild output uses neither by default. Webpack requires care and plugins.
- Zero-config TypeScript.
- ~100× faster than webpack; sub-second full rebuild is a big DX win.
- Output format `iife` per entry — no runtime module loader in the bundle, which is what MV3 wants for content scripts. Service worker can be `iife` too (importScripts avoided per the existing comment).
- No webpack-specific loader/plugin needs (no CSS, no images bundled — those stay static in extension/).

Bundle policy:
- **All 5 entry points build to `dist/`.**
- **`dist/` is committed** (not gitignored) so Chrome Web Store submissions are reproducible and reviewers on GitHub can see what actually ships. CI enforces `git diff --exit-code dist/` after build.
- **Source maps committed for `content.js` and `background.js`** (largest bundles) but excluded from packaged CRX (via a Chrome Web Store `.zipignore` or a manual pack script).

## 4. Sequenced steps (commit-per-step)

1. `feat(extension): add package.json + tsconfig.json + esbuild.config.mjs` — new files only. No source changes. `npm run build` produces empty `dist/` (no entries yet). Extension continues to run from top-level `.js` files.
2. `feat(extension): configure esbuild.config.mjs with 5 entries (content, background, inject, popup, tabs) — sources still at extension/*.js, outputs to dist/*.js` — `npm run build` now produces bundled dist/ output that is functionally identical to top-level scripts (single-file bundle of a single-file source is essentially a copy + minify). Manifest still points to top-level files — dist/ is unused.
3. `chore(extension): flip manifest.json to reference dist/*.js for all 5 entries; keep top-level *.js in place (unused fallback)` — reload extension in Chrome; smoke-test on Instagram + one other platform. If green, proceed.
4. `refactor(extension): move extension/content.js → extension/src/content.js (verbatim); rebuild; delete top-level content.js`.
5. `refactor(extension): move extension/background.js → extension/src/background.js (verbatim); rebuild; delete top-level background.js`.
6. `refactor(extension): move inject.js, popup.js, tabs.js, platforms.js → extension/src/; rebuild; delete top-level copies`.
7. `refactor(extension): extract shared/platforms — dedupe UC_PLATFORMS between src/background.js and src/shared/platforms.js` — first real code sharing. background.js now imports from shared. **Critical**: keep the inline registry approach — import happens at bundle time, no runtime importScripts. This addresses the exact concern in the existing comment ("failed importScripts prevents listener registration").
8. `refactor(extension): extract shared/throttle — throttle wall functions (setWall, wallLeftMs, applyThrottleWall, DEFAULT_THROTTLE_BACKOFF_MINS) from src/content.js`.
9. `refactor(extension): extract shared/log — persistent log ring buffer used by content, background, popup`.
10. `refactor(extension): extract shared/ingest_client — HTTP client for /social/* endpoints`.
11. `refactor(extension): extract shared/cooldown — anti-ban cooldown coordination`.
12. `refactor(extension): extract shared/storage_helpers — lsGet/lsSet/lsNum/lsBump wrappers`.
13. `refactor(extension): [pilot] convert src/shared/platforms.js → platforms.ts; enable strict typechecking on that file only`.
14. `refactor(extension): convert remaining shared/*.js → *.ts` (one file per commit, four commits).
15. `refactor(extension): convert content.js/background.js to .ts progressively` — this is a longer sub-sequence, one commit per file, with strict types only for imports; body may remain `// @ts-nocheck` initially and clean up gradually.
16. `chore(ci): add extension build check to .github/workflows/` — new job: `cd extension && npm ci && npm run build && git diff --exit-code dist/`. Fails PR if dist/ is stale.
17. `chore(extension): update tests/extension/test_extension_bundle_static.py to parse dist/*.js after step 3; add a bundle-size budget assertion (fail if content.js grows >10% between commits)`.

## 5. Rollback per step

- **Steps 1–2**: pure addition; revert = safe.
- **Step 3 (manifest flip)**: if browser-side smoke test fails, revert this single commit and top-level *.js are re-referenced by manifest — extension keeps working. Because top-level files are untouched through step 3, rollback is 100% safe.
- **Steps 4–6 (moves)**: `git mv` rollbacks are trivial. Watch for one subtle thing: manifest paths must match the file locations after the move. If step 4 lands but manifest wasn't updated (mistake), Chrome fails to load the extension — reload manifest fixes it, no dist/ regeneration required if step 3 already flipped to dist/ paths.
- **Steps 7–12 (shared extraction)**: each extraction is atomic (move + rewire imports in one commit). Revert restores duplication. Any single revert leaves a working extension.
- **Steps 13–15 (TypeScript conversion)**: `.js → .ts` moves are pure; strict typechecking is opt-in per file, so failures during conversion don't block the rest of the build.
- **Step 16 (CI check)**: revert restores workflow to pre-check state. No runtime impact.

## 6. Test strategy

### Automated

- **Existing** `tests/extension/test_extension_bundle_static.py` — must pass after every step (update parsing target from top-level to `dist/` in step 3).
- **New bundle-size budget**: after step 17, CI fails if `dist/content.js` or `dist/background.js` grows more than 10% vs. previous commit. Protects against accidental dependency bloat.
- **Bundle determinism**: `npm run build` twice; `git diff --exit-code dist/` must be clean. Guards against non-deterministic build output.

### Manual (browser)

For each of steps 3, 6, 7, 15:
- Load unpacked extension in Chrome.
- Smoke on Instagram: verify the content-script "running" state, one successful `/social/ingest` POST captured in DevTools network tab.
- Smoke on TikTok: verify the following-tab scraper cycle fires (existing debug tooling in `scripts/ping_sw*.py`, `scripts/inspect_sw.py` can be used).
- Smoke on X/Threads/Facebook/Strava: at minimum load the site with extension enabled and confirm no console errors.
- Verify `chrome://extensions` shows no service-worker errors (this is where the "failed importScripts" problem historically manifested).

### Regression harness

The repo already has:
- `scripts/check_ext_version.py` — verify version reporting.
- `scripts/verify_tab_group_join.py` — verify tab-group behavior.
- `scripts/force_reload_ext.py`, `scripts/hard_reload_ext.py` — reload helpers.
- `scripts/browser-tab-maintenance.ps1` — long-running maintenance loop.

Run these against the bundled build after step 3, step 7, and step 15.

## 7. Effort estimate + confidence

- **4–6 dev days.** Confidence: **MEDIUM-HIGH**.
- esbuild config is trivial. Steps 1–6 are ~1 day.
- Step 7 (shared platforms extraction) is the highest-risk single commit — deduping the `UC_PLATFORMS` array carries the exact risk the existing code comment warns about ("failed importScripts prevents every alarm/listener from registering"). Mitigation: because esbuild produces IIFE, the registry is inlined at build time; there is *no runtime importScripts call at all*. This is the key insight that makes the extraction safe.
- Steps 13–15 (TypeScript adoption) are open-ended — the estimate assumes progressive `@ts-nocheck` for large files initially. A full-strict TS conversion of `content.ts` and `background.ts` is a separate ~5-day project not counted here.
- Chrome MV3 has quirks (CSP, MAIN-world for inject.js, service-worker lifecycle) — manual browser smoke tests at every risky commit are non-negotiable.
