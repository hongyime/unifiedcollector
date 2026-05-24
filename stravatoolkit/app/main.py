from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from app.routers.activities import router as activities_router
from app.routers.athletes import router as athletes_router
from app.routers.backfill import router as backfill_router
from app.routers.coverage import router as coverage_router
from app.routers.dates import router as dates_router
from app.routers.heatmap import router as heatmap_router
from app.routers.analysis import router as analysis_router
from app.routers.status import router as status_router
from app.routers.sync import router as sync_router
from ingestion.config import load_settings


app = FastAPI(title="Strava Sync Playback")


@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    """Handle all unhandled exceptions to prevent stack trace leakage."""
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.include_router(activities_router, prefix="/api/v1")
app.include_router(athletes_router, prefix="/api/v1")
app.include_router(backfill_router, prefix="/api/v1")
app.include_router(coverage_router, prefix="/api/v1")
app.include_router(dates_router, prefix="/api/v1")
app.include_router(heatmap_router, prefix="/api/v1")
app.include_router(analysis_router, prefix="/api/v1")
app.include_router(status_router, prefix="/api/v1")
app.include_router(sync_router, prefix="/api/v1")

settings = load_settings()
assets_dir = settings.frontend_dist_dir / "assets"
index_path = settings.frontend_dist_dir / "index.html"
if assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


@app.get("/")
def root():
    if index_path.exists():
        return FileResponse(index_path)
    return {
        "message": "Strava Sync Playback backend is running.",
        "api": ["/api/v1/status", "/api/v1/dates", "/api/v1/activities?date=YYYY-MM-DD"],
        "frontend_built": False,
    }


@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    requested = settings.frontend_dist_dir / full_path
    if requested.exists() and requested.is_file():
        return FileResponse(requested)
    if index_path.exists():
        return FileResponse(index_path)
    return {
        "message": "Frontend bundle not built yet.",
        "requested_path": full_path,
    }
