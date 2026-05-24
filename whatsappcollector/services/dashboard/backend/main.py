"""
backend/main.py — FastAPI application entrypoint for the WAC Dashboard.

Mounts all routers under /api/*, serves the WebSocket at /ws/health,
and serves the built React frontend as static files at /.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

import auth
import database
from config import get_settings
from routers import system as system_router
from routers import collector as collector_router
from routers import media as media_router
from routers import faces as faces_router
from routers import users as users_router
from routers import links as links_router
from routers import bulk as bulk_router
from routers import config as config_router
from routers import auth_router

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    logger.info("dashboard_starting")
    await database.init_pool()
    system_router.start_broadcast_task()
    yield
    logger.info("dashboard_shutting_down")
    system_router.stop_broadcast_task()
    await database.close_pool()
    await auth.close_redis()  # close auth Redis client
    await config_router.close_redis_client()  # close config Redis client


app = FastAPI(
    title="WAC Dashboard",
    description="WhatsApp Collector unified operations dashboard",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS for Vite dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:8700",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:8700",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Health ping (must be registered before static mount) ──────────────────────
@app.get("/api/ping")
async def ping():
    return {"status": "ok", "service": "wac-dashboard"}


# ── Auth (unauthenticated — must come before protected routers) ────────────────
app.include_router(auth_router.router, prefix="/api/auth", tags=["auth"])

# ── API Routers (all protected) ────────────────────────────────────────────────
from fastapi import Depends
from auth import get_current_user

app.include_router(system_router.router, prefix="/api/system", tags=["system"],
                   dependencies=[Depends(get_current_user)])
app.include_router(collector_router.router, prefix="/api/collector", tags=["collector"],
                   dependencies=[Depends(get_current_user)])
app.include_router(media_router.router, prefix="/api/media", tags=["media"],
                   dependencies=[Depends(get_current_user)])
app.include_router(faces_router.router, prefix="/api/faces", tags=["faces"],
                   dependencies=[Depends(get_current_user)])
app.include_router(users_router.router, prefix="/api/users", tags=["users"],
                   dependencies=[Depends(get_current_user)])
app.include_router(links_router.router, prefix="/api/links", tags=["links"],
                   dependencies=[Depends(get_current_user)])
app.include_router(bulk_router.router, prefix="/api/bulk", tags=["bulk"],
                   dependencies=[Depends(get_current_user)])
app.include_router(config_router.router, prefix="/api/config", tags=["config"],
                   dependencies=[Depends(get_current_user)])


# ── WebSocket at /ws/health ────────────────────────────────────────────────────
@app.websocket("/ws/health")
async def ws_health_root(websocket: WebSocket, token: str | None = None):
    """WebSocket endpoint — token passed as ?token=<bearer> query param."""
    settings = get_settings()
    if settings.dashboard_auth_required:
        if not token or not await auth.resolve_token(token):
            await websocket.close(code=4401)
            return
    await system_router.ws_health(websocket)


# ── Static files + SPA fallback ───────────────────────────────────────────────
# StaticFiles(html=True) only serves index.html for "/" — it does NOT fall
# back for deep SPA routes like "/collector". Use a catch-all route instead
# that serves real files first, then index.html for everything else.
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

if os.path.isdir(STATIC_DIR):
    # Serve /assets/* and other real files efficiently via mount
    assets_dir = os.path.join(STATIC_DIR, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> Response:
        """SPA catch-all: serve static file if it exists, else return index.html."""
        candidate = os.path.join(STATIC_DIR, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        index = os.path.join(STATIC_DIR, "index.html")
        if os.path.isfile(index):
            return FileResponse(index)
        return Response("Not Found", status_code=404)

    logger.info("spa_fallback_registered static_dir=%s", STATIC_DIR)
else:
    logger.warning("static_dir_not_found: %s — frontend not served", STATIC_DIR)
