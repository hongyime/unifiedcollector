"""
backend/routers/auth_router.py — Login / logout / whoami.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

import auth
from config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginBody) -> dict[str, Any]:
    settings = get_settings()
    if not settings.dashboard_auth_required:
        return await auth.create_session(body.username, "admin")

    users = auth._credential_map()
    entry = users.get(body.username)
    if entry is None or entry[0] != body.password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )
    password, role = entry
    return await auth.create_session(body.username, role)


@router.post("/logout")
async def logout(
    user: dict = Depends(auth.get_current_user),
) -> dict[str, Any]:
    return {"ok": True}


@router.get("/me")
async def whoami(
    user: dict = Depends(auth.get_current_user),
) -> dict[str, Any]:
    settings = get_settings()
    return {
        "username": user["username"],
        "role": user["role"],
        "auth_required": settings.dashboard_auth_required,
    }
