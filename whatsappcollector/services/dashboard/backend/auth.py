"""
backend/auth.py — Simple token-based authentication for the dashboard.

Login → POST /api/auth/login {username, password}
        → {token, role, expires_at}

Protected endpoints check: Authorization: Bearer <token>
Tokens are stored in Redis with 8h TTL.
When DASHBOARD_AUTH_REQUIRED=false, all requests are permitted (dev mode).
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone, timedelta
from typing import Any

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import get_settings

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)
_TOKEN_TTL_SECONDS = 8 * 3600  # 8 hours
_REDIS_PREFIX = "dash:session:"

_redis: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis | None:
    global _redis
    if _redis is None:
        try:
            settings = get_settings()
            _redis = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
        except Exception as exc:
            logger.warning("auth_redis_init_failed: %s", exc)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


def _credential_map() -> dict[str, tuple[str, str]]:
    """Return {username: (password, role)} from env config."""
    s = get_settings()
    users: dict[str, tuple[str, str]] = {}
    if s.dashboard_viewer_username and s.dashboard_viewer_password:
        users[s.dashboard_viewer_username] = (s.dashboard_viewer_password, "viewer")
    if s.dashboard_operator_username and s.dashboard_operator_password:
        users[s.dashboard_operator_username] = (s.dashboard_operator_password, "operator")
    if s.dashboard_admin_username and s.dashboard_admin_password:
        users[s.dashboard_admin_username] = (s.dashboard_admin_password, "admin")
    return users


async def create_session(username: str, role: str) -> dict[str, Any]:
    """Create a session token, persist to Redis, return token metadata."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=_TOKEN_TTL_SECONDS)
    redis = _get_redis()
    if redis is not None:
        try:
            await redis.setex(
                f"{_REDIS_PREFIX}{token}",
                _TOKEN_TTL_SECONDS,
                f"{username}:{role}",
            )
        except Exception as exc:
            logger.error("auth_session_store_failed: %s", exc)
            # Fall through — in-memory fallback won't survive restart but keeps the service up
    _in_memory_sessions[token] = (username, role, expires_at)
    return {"token": token, "role": role, "expires_at": expires_at.isoformat()}


async def resolve_token(token: str) -> tuple[str, str] | None:
    """Return (username, role) for a valid token, or None."""
    # Try Redis first
    redis = _get_redis()
    if redis is not None:
        try:
            value = await redis.get(f"{_REDIS_PREFIX}{token}")
            if value:
                username, role = value.split(":", 1)
                return username, role
        except Exception as exc:
            logger.debug("auth_redis_lookup_failed: %s", exc)

    # Fall back to in-memory store
    entry = _in_memory_sessions.get(token)
    if entry:
        _, _, expires_at = entry
        if datetime.now(timezone.utc) < expires_at:
            return entry[0], entry[1]
        del _in_memory_sessions[token]
    return None


# In-memory fallback (survives Redis outage within a single process lifetime)
_in_memory_sessions: dict[str, tuple[str, str, datetime]] = {}


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict[str, str]:
    """FastAPI dependency — returns {username, role} or raises 401."""
    settings = get_settings()
    if not settings.dashboard_auth_required:
        return {"username": "dev", "role": "admin"}

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await resolve_token(credentials.credentials)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    username, role = result
    return {"username": username, "role": role}


def require_role(minimum_role: str):
    """Dependency factory — raises 403 if user's role is below minimum."""
    _levels = {"viewer": 1, "operator": 2, "admin": 3}

    async def _check(user: dict = Depends(get_current_user)) -> dict:
        if _levels.get(user.get("role", ""), 0) < _levels.get(minimum_role, 99):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {minimum_role} role",
            )
        return user

    return _check
