"""TGC Dashboard — FastAPI backend + React frontend."""
from __future__ import annotations
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import database
from routers import system as system_router
from routers import collector as collector_router
from routers import faces as faces_router
from routers import users as users_router
from routers import links as links_router
from routers import config as config_router

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.init_pool()
    system_router.start_broadcast_task()
    yield
    system_router.stop_broadcast_task()
    await database.close_pool()


app = FastAPI(title="TGC Dashboard", version="2.0.0", lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://localhost:8500"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

app.include_router(system_router.router, prefix="/api/system", tags=["system"])
app.include_router(collector_router.router, prefix="/api/collector", tags=["collector"])
app.include_router(faces_router.router, prefix="/api/faces", tags=["faces"])
app.include_router(users_router.router, prefix="/api/users", tags=["users"])
app.include_router(links_router.router, prefix="/api/links", tags=["links"])
app.include_router(config_router.router, prefix="/api/config", tags=["config"])


@app.get("/api/ping")
async def ping():
    return {"status": "ok", "service": "tgc-dashboard"}


@app.websocket("/ws/health")
async def ws_health_root(websocket: WebSocket):
    await system_router.ws_health(websocket)


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
ASSETS_DIR = os.path.join(STATIC_DIR, "assets")

if os.path.isdir(ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    index = os.path.join(STATIC_DIR, "index.html")
    if os.path.isfile(index):
        return FileResponse(index)
    return JSONResponse({"error": "frontend not built"}, status_code=503)

logger.info("serving static from %s", STATIC_DIR)
