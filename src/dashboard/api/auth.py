"""JWT / bcrypt auth for the dashboard: config, deps, and the /auth/* routes.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 package split
(step 4 of ``docs/plans/perf-file-splits.md`` sub-plan 4A). Kept as the smallest
cohesive route slice for the first cut. All public names are re-exported from
``src.dashboard.api.__init__`` for back-compat (``tests/dashboard/test_targets_dedupe.py``
and dozens of ``Depends(require_role(...))`` sites in ``__init__.py``).

Import-time behavior: this module fails closed on a missing/default
``DASHBOARD_JWT_SECRET`` — matches the pre-refactor behavior in ``__init__.py``.
"""
from __future__ import annotations

import asyncio as _asyncio
import os
import secrets as _secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from src.db.connection import get_pool


# ---------------------------------------------------------------------------
# Config — read once at import time (matches pre-refactor semantics).
# ---------------------------------------------------------------------------

JWT_SECRET = os.getenv("DASHBOARD_JWT_SECRET", "")
if not JWT_SECRET or JWT_SECRET == "changeme-in-production":
    # Fail closed: never allow the dashboard to run with a known/empty signing key.
    raise RuntimeError(
        "DASHBOARD_JWT_SECRET env var is not set (or still default). "
        "Generate one: python -c 'import secrets;print(secrets.token_urlsafe(48))'"
    )
JWT_EXPIRY_HOURS = int(os.getenv("DASHBOARD_JWT_EXPIRY_HOURS", "8"))
ADMIN_USERNAME = os.getenv("DASHBOARD_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("DASHBOARD_ADMIN_PASSWORD", "")

security = HTTPBearer(auto_error=False)

_ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}

# Localhost convenience: when DASHBOARD_AUTH_DISABLED is truthy, every request is
# treated as an authenticated admin. Intended for single-user localhost-only
# deployments where prompting for a bearer token is pure friction. Leave UNSET
# (or false) for any network-exposed deployment -- the JWT flow stays fully intact.
_AUTH_DISABLED = os.getenv("DASHBOARD_AUTH_DISABLED", "").lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# FastAPI dependency helpers.
# ---------------------------------------------------------------------------

async def get_current_user(creds: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    if _AUTH_DISABLED:
        return {"username": "localhost", "role": "admin"}
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    username = payload.get("sub")
    role = payload.get("role", "viewer")
    if not username or role not in _ROLE_RANK:
        raise HTTPException(status_code=401, detail="Malformed token")
    return {"username": username, "role": role}


def require_role(min_role: str):
    if min_role not in _ROLE_RANK:
        raise ValueError(f"Unknown role: {min_role}")
    threshold = _ROLE_RANK[min_role]

    async def check(user: dict = Depends(get_current_user)):
        if _ROLE_RANK.get(user["role"], -1) < threshold:
            raise HTTPException(status_code=403, detail="Insufficient role")
        return user
    return check


# ---------------------------------------------------------------------------
# Router — mounted onto the top-level app via app.include_router(router).
# ---------------------------------------------------------------------------

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
async def login(req: LoginRequest):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT password_hash, role FROM dashboard_users WHERE username = $1",
            req.username,
        )

    role = None
    if row:
        try:
            stored_hash = row["password_hash"]
            if isinstance(stored_hash, str):
                stored_hash = stored_hash.encode()
            if bcrypt.checkpw(req.password.encode(), stored_hash):
                role = row["role"]
        except (ValueError, TypeError):
            role = None
    elif (
        ADMIN_PASSWORD
        and req.username == ADMIN_USERNAME
        and _secrets.compare_digest(req.password, ADMIN_PASSWORD)
    ):
        role = "admin"

    if role is None:
        # Constant-ish failure path — sleep a bit so success/fail paths are similar.
        await _asyncio.sleep(0.25)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = jwt.encode(
        {
            "sub": req.username,
            "role": role,
            "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    return {"token": token, "username": req.username, "role": role}


@router.get("/auth/me")
async def auth_me(user: dict = Depends(require_role("viewer"))):
    return user
