from __future__ import annotations
import logging
import os
from typing import Any
from fastapi import APIRouter
import database

logger = logging.getLogger(__name__)
router = APIRouter()

_SENSITIVE_KEYS = {"PASSWORD", "TOKEN", "HASH", "SECRET", "KEY"}
_ENV_PREFIXES = ("TG_", "BOT_", "DB_", "REDIS_", "FACE_", "COLLECTOR_", "HUB_", "USER_INTEL", "LINK_")


@router.get("/settings")
async def get_settings() -> dict[str, Any]:
    try:
        rows = await database.fetchall("SELECT config_key, group_name, value_plain, is_sensitive, updated_at FROM collector.config_settings ORDER BY group_name, config_key")
        return {"settings": rows, "error": None}
    except Exception as e:
        logger.error("config_settings_error: %s", e)
        return {"settings": [], "error": str(e)}


@router.get("/env")
async def get_env() -> dict[str, Any]:
    env = []
    for k, v in sorted(os.environ.items()):
        if any(k.startswith(p) for p in _ENV_PREFIXES):
            masked = any(s in k for s in _SENSITIVE_KEYS)
            env.append({"key": k, "value": "•••" if masked else v})
    return {"env": env, "error": None}
