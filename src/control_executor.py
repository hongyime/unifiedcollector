"""Narrow, DB-backed Docker executor for approved control-room operations."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import signal

import aiohttp

logger = logging.getLogger(__name__)

# Only public/messaging collector workers are controllable. Infrastructure,
# browsers, bridges, sessions, dashboards, and the scheduler are deliberately absent.
ALLOWED_SERVICES = frozenset({
    "unifiedcollector_collector_airbnb", "unifiedcollector_collector_beeper",
    "unifiedcollector_collector_bluesky", "unifiedcollector_collector_exposure",
    "unifiedcollector_collector_facebook",
    "unifiedcollector_collector_github", "unifiedcollector_collector_instagram",
    "unifiedcollector_collector_lemon8",
    "unifiedcollector_collector_pinterest", "unifiedcollector_collector_search",
    "unifiedcollector_collector_snapchat", "unifiedcollector_collector_strava",
    "unifiedcollector_collector_telegram", "unifiedcollector_collector_tiktok",
    "unifiedcollector_collector_threads",
    "unifiedcollector_collector_whatsapp", "unifiedcollector_collector_website",
    "unifiedcollector_collector_x",
    "unifiedcollector_collector_youtube",
})


def _signature(key: str, action: str, target: str) -> str | None:
    secret = os.getenv("CONTROL_EXECUTOR_SECRET", "")
    if not secret:
        return None
    return hmac.new(secret.encode(), f"{key}|{action}|{target}".encode(), hashlib.sha256).hexdigest()


def _valid_operation(operation: dict) -> bool:
    expected = _signature(operation["idempotency_key"], operation["action"], operation["target"] or "")
    return bool(expected and operation.get("signature") and hmac.compare_digest(expected, operation["signature"]))


async def _docker_action(action: str, target: str) -> tuple[bool, str]:
    if target not in ALLOWED_SERVICES or action not in {"restart", "pause", "resume"}:
        return False, "target or action is not allowlisted"
    path = {"restart": f"/containers/{target}/restart?t=15", "pause": f"/containers/{target}/pause",
            "resume": f"/containers/{target}/unpause"}[action]
    try:
        connector = aiohttp.UnixConnector(path=os.getenv("DOCKER_SOCK", "/var/run/docker.sock"))
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(f"http://docker{path}") as response:
                if response.status in {204, 304}:
                    return True, f"HTTP {response.status}"
                return False, f"HTTP {response.status}: {(await response.text())[:200]}"
    except Exception as exc:  # executor must report, not lose the requested action
        return False, f"{type(exc).__name__}: {exc}"


async def _claim(pool) -> dict | None:
    async with pool.acquire() as db:
        async with db.transaction():
            row = await db.fetchrow(
                """SELECT * FROM control_bot_operations WHERE status='queued' AND action IN ('restart','pause','resume')
                   AND expires_at > now() ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"""
            )
            if row is None:
                return None
            row = dict(row)
            if not _valid_operation(row):
                await db.execute(
                    "UPDATE control_bot_operations SET status='rejected',completed_at=now(),result=$2::jsonb WHERE id=$1",
                    row["id"], json.dumps({"detail": "invalid executor signature"}),
                )
                return None
            await db.execute("UPDATE control_bot_operations SET status='running',started_at=now() WHERE id=$1", row["id"])
            return row


async def _complete(pool, operation: dict, ok: bool, detail: str) -> None:
    status = "succeeded" if ok else "failed"
    async with pool.acquire() as db:
        await db.execute(
            "UPDATE control_bot_operations SET status=$2,completed_at=now(),result=$3::jsonb WHERE id=$1",
            operation["id"], status, json.dumps({"detail": detail}),
        )
        await db.execute(
            """INSERT INTO control_bot_audit(event,actor_id,card_token,operation_id,detail)
               VALUES($1,$2,$3,$4,$5::jsonb)""",
            f"executor_{status}", operation["actor_id"], operation["card_token"], operation["id"],
            json.dumps({"action": operation["action"], "target": operation["target"], "detail": detail}),
        )
        await db.execute(
            """INSERT INTO control_bot_outbox(operation_id,chat_id,message_id,payload)
               SELECT $1, chat_id, message_id, jsonb_build_object('status',$2,'detail',$3,
                       'source',payload->>'source','action',$4)
               FROM control_bot_cards WHERE token=$5 AND message_id IS NOT NULL
               ON CONFLICT(operation_id) DO NOTHING""",
            operation["id"], status, detail, operation["action"], operation["card_token"],
        )


async def run() -> None:
    from src.db.connection import close_pool, get_pool
    pool = await get_pool()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    while not stop.is_set():
        operation = await _claim(pool)
        if operation is None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
            continue
        ok, detail = await _docker_action(operation["action"], operation["target"])
        await _complete(pool, operation, ok, detail)
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] control-executor: %(message)s")
    asyncio.run(run())
