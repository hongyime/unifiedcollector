"""
backend/routers/config.py — Live config management endpoints.

Reads PARAMETER_REGISTRY from shared/live_config.py.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter
from pydantic import BaseModel

from config import get_settings

# Allow import of shared/live_config.py (mounted at /app/shared in Docker)
sys.path.insert(0, "/app/shared")
# Also try relative path for local dev
sys.path.insert(0, "/app")

try:
    from live_config import PARAMETER_REGISTRY, ParameterMeta
except ImportError:
    try:
        import importlib.util
        import os
        shared_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared", "live_config.py")
        spec = importlib.util.spec_from_file_location("live_config", shared_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        PARAMETER_REGISTRY = mod.PARAMETER_REGISTRY
        ParameterMeta = mod.ParameterMeta
    except Exception as e:
        logging.getLogger(__name__).warning("Could not import PARAMETER_REGISTRY: %s", e)
        PARAMETER_REGISTRY = {}
        ParameterMeta = None

logger = logging.getLogger(__name__)
router = APIRouter()

_redis_client: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis | None:
    global _redis_client
    if _redis_client is None:
        try:
            settings = get_settings()
            _redis_client = aioredis.Redis.from_url(
                settings.redis_url, decode_responses=True
            )
        except Exception as exc:
            logger.warning("config_redis_init_failed: %s", exc)
    return _redis_client


async def close_redis_client() -> None:
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


def _meta_to_dict(meta: Any) -> dict:
    """Serialize a ParameterMeta to JSON-compatible dict."""
    return {
        "key": meta.key,
        "service": meta.service,
        "type": meta.python_type.__name__ if hasattr(meta.python_type, "__name__") else str(meta.python_type),
        "default": meta.default,
        "description": meta.description,
        "min_value": meta.min_value,
        "max_value": meta.max_value,
        "options": meta.options,
        "requires_restart": meta.requires_restart,
        "multi_select": meta.multi_select,
        "known_values": meta.known_values,
    }


@router.get("")
async def get_config() -> dict[str, Any]:
    """Return all config params with their current Redis override (if any)."""
    redis = _get_redis()
    result: dict[str, Any] = {}

    for service_name, params in PARAMETER_REGISTRY.items():
        # Fetch all overrides for this service from Redis
        overrides: dict[str, str] = {}
        if redis is not None:
            try:
                overrides = await redis.hgetall(f"live_config:{service_name}") or {}
            except Exception as exc:
                logger.warning("config_redis_fetch_error service=%s: %s", service_name, exc)

        service_params = []
        for meta in params:
            entry = _meta_to_dict(meta)
            entry["current_value"] = overrides.get(meta.key)  # None means "using default"
            entry["has_override"] = meta.key in overrides
            service_params.append(entry)

        result[service_name] = service_params

    return {"config": result, "error": None}


class SetValueBody(BaseModel):
    value: str


@router.post("/{service}/{key}")
async def set_config_value(service: str, key: str, body: SetValueBody) -> dict[str, Any]:
    """Set a Redis override for a config parameter."""
    if service not in PARAMETER_REGISTRY:
        return {"ok": False, "error": f"Unknown service: {service}"}

    redis = _get_redis()
    if redis is None:
        return {"ok": False, "error": "Redis not available"}

    # Validate key exists for service
    param_map = {m.key: m for m in PARAMETER_REGISTRY[service]}
    if key not in param_map:
        return {"ok": False, "error": f"Unknown parameter: {key} for service {service}"}

    meta = param_map[key]

    # Basic type validation
    try:
        if meta.python_type is bool:
            if body.value.lower() not in ("true", "false", "1", "0", "yes", "no"):
                raise ValueError(f"Invalid bool: {body.value}")
        elif meta.python_type is int:
            coerced = int(body.value)
            if meta.min_value is not None and coerced < meta.min_value:
                raise ValueError(f"Below minimum {meta.min_value}")
            if meta.max_value is not None and coerced > meta.max_value:
                raise ValueError(f"Above maximum {meta.max_value}")
        elif meta.python_type is float:
            coerced = float(body.value)
            if meta.min_value is not None and coerced < meta.min_value:
                raise ValueError(f"Below minimum {meta.min_value}")
            if meta.max_value is not None and coerced > meta.max_value:
                raise ValueError(f"Above maximum {meta.max_value}")
        if meta.options and body.value not in meta.options:
            raise ValueError(f"Not in allowed options: {meta.options}")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        await redis.hset(f"live_config:{service}", key, body.value)
        return {"ok": True, "error": None}
    except Exception as exc:
        logger.error("set_config_value_error: %s", exc)
        return {"ok": False, "error": str(exc)}


@router.delete("/{service}/{key}")
async def delete_config_value(service: str, key: str) -> dict[str, Any]:
    """Remove a Redis override, reverting to the default."""
    if service not in PARAMETER_REGISTRY:
        return {"ok": False, "error": f"Unknown service: {service}"}

    redis = _get_redis()
    if redis is None:
        return {"ok": False, "error": "Redis not available"}

    try:
        await redis.hdel(f"live_config:{service}", key)
        return {"ok": True, "error": None}
    except Exception as exc:
        logger.error("delete_config_value_error: %s", exc)
        return {"ok": False, "error": str(exc)}
