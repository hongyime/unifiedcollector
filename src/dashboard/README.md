# src/dashboard/

Operations dashboard for the unified collector. Backend and frontend live
together under one root; the analyzer (:8002) owns investigation and identity
resolution — this dashboard covers **collection ops only**.

## Layout

```
src/dashboard/
├── __init__.py
├── api/                # FastAPI backend (formerly api.py; being split under PERF-002)
│   ├── __init__.py     # app factory + routes
│   └── helpers.py      # shared helpers
├── websocket.py        # WebSocket handler for live health / stats
└── frontend/           # React 19 + Vite SPA (formerly at top-level dashboard/frontend/)
    ├── package.json
    ├── vite.config.ts
    ├── tsconfig.json
    ├── index.html
    ├── public/
    └── src/
        ├── App.tsx
        ├── main.tsx
        ├── components/
        ├── features/    # per-source pages: collectors, whatsapp, telegram, …
        ├── hooks/
        ├── services/
        └── utils/
```

Consolidated under one root by
[`docs/plans/dashboard-root-consolidation.md`](../../docs/plans/dashboard-root-consolidation.md)
(STRUCT-006 resolved).

## Backend (`api/` + `websocket.py`)

FastAPI app. Served in production by the `dashboard` container
(`docker/Dockerfile.dashboard`) on port **8700**, mapped to :8001 as well.

Key entry points:

- `src.dashboard.api:app` — the ASGI application (`uvicorn` target).
- `src.dashboard.websocket` — WebSocket route for `/ws` real-time updates.

The container bind-mounts `../src:/app/src`, so a code edit + `docker restart
dashboard` picks up backend changes without an image rebuild.

## Frontend (`frontend/`)

Vite + React 19 SPA. Build output goes to `frontend/dist/` and is COPY'd into
the dashboard image (`docker/Dockerfile.dashboard:8`). Dev is a separate flow
against a locally-running backend.

### Dev commands

Run inside `src/dashboard/frontend/`:

```bash
cd src/dashboard/frontend
npm ci                # or: npm install
npm run dev           # Vite dev server on :5173
npm run build         # tsc && vite build → dist/
npm run preview       # serve dist/ on :4173
npm test              # Vitest smoke suite
```

### Proxy config

`vite.config.ts` proxies API and WebSocket traffic to the backend so the SPA
can run on :5173 while the FastAPI backend runs on :8700:

- `/api` → `http://localhost:8700`
- `/ws`  → `ws://localhost:8700` (WebSocket upgrade)

For dev, start the backend independently (either the whole stack via
`docker compose up -d dashboard` or a local `uvicorn src.dashboard.api:app
--port 8700`) before `npm run dev`.

## Auth

`DASHBOARD_AUTH_DISABLED=true` is the default for the localhost single-user
deployment. Remove/set to `false` before exposing over any network.

## Related

- Backend tests: `tests/dashboard/`
- Compose service definition: `docker/docker-compose.yml` (`dashboard` service)
- Image build: `docker/Dockerfile.dashboard`
- Consolidation plan (executed): `docs/plans/dashboard-root-consolidation.md`
