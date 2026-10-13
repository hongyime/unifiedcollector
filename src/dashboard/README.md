# Collector operations dashboard

The dashboard exposes Collector health, coverage, targets, media, source
controls and operational queues. UnifiedAnalyzer has its own interface for
investigation and identity resolution.

## Layout

- `api/` — FastAPI application and route modules. The ASGI app is
  `src.dashboard.api:app`.
- `websocket.py` — live health/stat WebSocket support.
- `frontend/` — React 19 + Vite single-page application.

The `dashboard` service serves the application on port 8700; Compose also maps
host port 8001 for the existing local URL. Its `../src:/app/src` bind mount
makes backend source available inside the running container. The frontend is
built into the dashboard image.

## Frontend development

Run from `src/dashboard/frontend/`:

```sh
npm ci
npm run dev
npm run build
npm run preview
npm test
```

Vite proxies `/api` and `/ws` to the backend on port 8700. Start the Compose
dashboard or a local ASGI server before using the dev server.

## Authentication and references

`DASHBOARD_AUTH_DISABLED=true` is the local single-user default. Set it to
`false` before exposing the dashboard over a network.

- Backend tests: `tests/dashboard/`
- Compose definition: [`docker/docker-compose.yml`](../../docker/docker-compose.yml)
- Dashboard image: [`docker/Dockerfile.dashboard`](../../docker/Dockerfile.dashboard)
