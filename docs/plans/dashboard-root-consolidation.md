# Plan: Dashboard root consolidation

Addresses **STRUCT-006** — the dashboard exists at two roots.

## 1. Context — current state

Two independent roots:

- **`dashboard/frontend/`** (top-level) — Vite + React SPA.
  - `package.json`: React 19.2, Vite 8.2, TanStack Query 5.101, TanStack Table 9, React Router 8.3, Tailwind 4.3, TypeScript 7.
  - `vite.config.ts`: proxies `/api` → `http://localhost:8700`, `/ws` → `ws://localhost:8700`.
  - `src/App.tsx` (5,533 B): 40+ routes across `features/{collectors,coverage,health,settings,targets,schedules,runs,graph,recon,media,stories,whatsapp,social,accounts,platform,strava,instagram,tiktok,threads,youtube,github,lemon8,beeper,telegram,auth}`.
  - `src/{components,hooks,services,utils,features}`.
  - `public/{favicon.svg, icons.svg}`.

- **`src/dashboard/`** — Python FastAPI backend.
  - `api.py` (492,416 B; being restructured under PERF-002).
  - `websocket.py` (5,153 B).
  - `__init__.py` (empty).

Docker build:
- `docker/Dockerfile.dashboard` (338 B, minimal) drives the `dashboard` service in compose (line 1101).

## 2. Motivation

- Two roots create discoverability friction: new contributors ask "which one is the dashboard?" — the answer is both.
- Frontend/backend contract changes (add API route → add page consuming it) span two roots, hard to bundle in one PR review.
- `git grep dashboard` returns hits from both trees mixed with historical references.
- Docker build context and `.dockerignore` must include two branches, which is easy to break.
- Downstream: PERF-002 will introduce `src/dashboard/api/` as a package. If frontend stays at top level, the ambiguity persists post-PERF-002 refactor.
- No functional harm today — this is a cleanliness/maintainability change that also enables tighter co-location of frontend fixtures with the API they mock against.

## 3. Target end state

**Direction: move the frontend under `src/dashboard/`** (not the other way around). Rationale:

- Python imports across the codebase are hardcoded to `src.dashboard.*` — cannot easily move.
- The top-level `dashboard/` directory contains only `frontend/`; removing it is a lighter change than rewriting Python import paths.
- Keeps a single `src/dashboard/README.md` as the canonical entry point.

```
src/dashboard/
  README.md                        # explains backend/frontend split
  __init__.py
  websocket.py
  api/                             # from PERF-002 (may or may not be merged before this plan runs)
  frontend/                        # moved from top-level dashboard/frontend/
    package.json
    vite.config.ts
    tsconfig.json
    index.html
    public/
    src/
```

Top-level `dashboard/` is deleted.

## 4. Sequenced steps (commit-per-step)

1. `chore(dashboard): git mv dashboard/frontend to src/dashboard/frontend` — single atomic move preserving git history via `git mv`. Nothing else changes.
2. `chore(dashboard): update vite.config.ts if any base/paths reference the old location` — inspect and adjust if needed (vite.config as of now uses no absolute paths beyond `/api`/`/ws` proxy targets, so this is likely a no-op).
3. `chore(dashboard): remove empty top-level dashboard/ directory` (guarded by `git status --porcelain dashboard/` returning empty).
4. `chore(docker): update Dockerfile.dashboard build context and COPY paths, plus docker/docker-compose.yml volume mounts, to reference src/dashboard/frontend`.
5. `chore(scripts): update any script or workflow referencing dashboard/frontend/` — grep across `scripts/`, `.github/workflows/`, top-level `*.md`; expected hits: dashboard build/test scripts, boot-verify. Adjust each in this single commit.
6. `docs(dashboard): update README.md, docs/README.md, and add src/dashboard/README.md` — new README explains the backend/frontend layout, dev commands (`npm run dev` from `src/dashboard/frontend`), and proxy configuration.

## 5. Rollback per step

- Step 1 (`git mv`): rollback = revert commit. Git tracks the rename, so `git log --follow` on any moved file continues to work in both directions.
- Step 4 (docker/compose): if the dashboard container fails to build post-move, revert step 4 to restore old context paths — step 1's move remains but the container works using the old-path COPY. This gives a working intermediate state during rollback.
- Steps 2, 5, 6: pure config/text; revert restores previous behavior.

Full-plan rollback: revert all six commits in reverse order.

## 6. Test strategy

- **Frontend build parity**: capture `dashboard/frontend/dist/` byte-for-byte before step 1 (`npm ci && npm run build`). After step 1, rebuild from the new location and compare file lists + sizes. Any drift is a bug.
- **Backend test parity**: `pytest tests/dashboard` runs before step 1 (baseline) and after step 4 (final). Must be identical outcome.
- **Docker build**: `docker compose build dashboard` after step 4. Then `docker compose up -d dashboard`, `curl http://localhost:8700/` — must return the SPA index.html (200 OK).
- **Dev-server smoke**: from `src/dashboard/frontend`, `npm run dev` must start Vite on :5173 with the `/api` proxy working (`curl http://localhost:5173/api/health` proxies to :8700).
- **Manual click-through**: after step 6, click through 5 representative routes in the SPA (dashboard, collectors, coverage, whatsapp/users, telegram/stats) to confirm no path-based regressions.

## 7. Effort estimate + confidence

- **2–3 dev days.** Confidence: **HIGH**.
- Mechanical move + config/documentation updates. Main risk: hardcoded absolute paths in `scripts/` (PowerShell scripts frequently embed `C:\unifiedcollector\dashboard\...`). Mitigation: step 5 explicit grep step.
- Zero code logic changes; no chance of runtime regression.
