"""Bounded automatic collector recovery for sustained host-memory pressure."""
from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from datetime import timedelta

from src.scheduler.handlers.base import SchedulerContext
from src.scheduler.handlers.host_memory_alert import _read_meminfo

logger = logging.getLogger(__name__)

# Realtime capacity is reserved: these sources are never automatically paused.
_ELIGIBLE = (
    "unifiedcollector_collector_airbnb", "unifiedcollector_collector_snapchat",
    "unifiedcollector_collector_pinterest", "unifiedcollector_collector_website",
    "unifiedcollector_collector_search", "unifiedcollector_collector_exposure",
    "unifiedcollector_collector_github", "unifiedcollector_collector_lemon8",
    "unifiedcollector_collector_instagram", "unifiedcollector_collector_tiktok",
    "unifiedcollector_collector_youtube", "unifiedcollector_collector_bluesky",
    "unifiedcollector_collector_strava", "unifiedcollector_collector_facebook",
    "unifiedcollector_collector_x", "unifiedcollector_collector_threads",
)


def _available_pct(info: dict[str, int]) -> float | None:
    total, available = info.get("MemTotal", 0), info.get("MemAvailable", 0)
    return available / total * 100 if total > 0 else None


class MemoryRecoveryHandler:
    """Queue one cancellable collector pause/resume through the narrow executor."""

    name = "memory_recovery"

    def __init__(self) -> None:
        self._last_run = 0.0

    async def should_run(self, ctx: SchedulerContext) -> bool:
        interval = ctx.get_env_int("MEMORY_RECOVERY_INTERVAL_SECONDS", 60, min_value=30)
        return _time.monotonic() - self._last_run >= interval

    async def run(self, ctx: SchedulerContext) -> None:
        self._last_run = _time.monotonic()
        pct = _available_pct(_read_meminfo())
        if pct is None:
            return
        pause_at = ctx.get_env_float("MEMORY_RECOVERY_PAUSE_AT_PCT", 8.0, min_value=1.0)
        resume_at = ctx.get_env_float("MEMORY_RECOVERY_RESUME_AT_PCT", 18.0, min_value=pause_at + 1.0)
        sustained = ctx.get_env_int("MEMORY_RECOVERY_SUSTAINED_SECONDS", 300, min_value=60)
        grace = ctx.get_env_int("MEMORY_RECOVERY_CANCEL_GRACE_SECONDS", 60, min_value=30)
        cooldown = ctx.get_env_int("MEMORY_RECOVERY_COOLDOWN_SECONDS", 1800, min_value=300)
        circuit = ctx.get_env_int("MEMORY_RECOVERY_CIRCUIT_SECONDS", 7200, min_value=600)
        metrics = {"available_pct": round(pct, 2), "pause_at_pct": pause_at, "resume_at_pct": resume_at}
        async with ctx.pool.acquire() as db:
            async with db.transaction():
                state = await db.fetchrow("SELECT * FROM memory_recovery_state WHERE singleton=true FOR UPDATE")
                if state is None:
                    await db.execute("INSERT INTO memory_recovery_state(singleton) VALUES(true)")
                    state = {"pressure_since": None, "recovery_since": None, "paused_target": None,
                             "cooldown_until": None, "circuit_open_until": None}
                if pct <= pause_at:
                    await db.execute("UPDATE memory_recovery_state SET pressure_since=COALESCE(pressure_since,now()), recovery_since=NULL, updated_at=now() WHERE singleton=true")
                elif pct >= resume_at:
                    await db.execute("UPDATE memory_recovery_state SET recovery_since=COALESCE(recovery_since,now()), pressure_since=NULL, updated_at=now() WHERE singleton=true")
                else:
                    await db.execute("UPDATE memory_recovery_state SET pressure_since=NULL,recovery_since=NULL,updated_at=now() WHERE singleton=true")
                state = await db.fetchrow("SELECT * FROM memory_recovery_state WHERE singleton=true FOR UPDATE")
                await self._settle_operations(db, state, grace, cooldown, circuit)
                pending = await db.fetchval("SELECT EXISTS(SELECT 1 FROM memory_recovery_actions WHERE status IN ('awaiting_cancel','queued'))")
                if pending or (state["circuit_open_until"] and state["circuit_open_until"] > ctx.now):
                    return
                action = target = None
                if pct <= pause_at and not state["paused_target"] and state["pressure_since"] and ctx.now - state["pressure_since"] >= timedelta(seconds=sustained):
                    target = await self._next_target(db)
                    action = "pause"
                elif pct >= resume_at and state["paused_target"] and state["recovery_since"] and ctx.now - state["recovery_since"] >= timedelta(seconds=sustained) and (not state["cooldown_until"] or state["cooldown_until"] <= ctx.now):
                    target, action = state["paused_target"], "resume"
                if not action or not target:
                    return
                from src.notifications import collector_bot
                token = await collector_bot.create_memory_recovery_card(db, target, action, metrics, grace)
                row = await db.fetchrow(
                    """INSERT INTO memory_recovery_actions(target,action,status,reason,metrics,card_token,expires_at)
                       VALUES($1,$2,'awaiting_cancel',$3,$4::jsonb,$5,now()+$6::interval) RETURNING id""",
                    target, action, "sustained memory pressure" if action == "pause" else "sustained memory recovery",
                    json.dumps(metrics), token, f"{grace * 2} seconds",
                )
                asyncio.create_task(self._announce(ctx.pool, token, target, action, metrics))
                logger.warning("memory recovery action %s awaiting cancel for %s id=%s", action, target, row["id"])

    async def _next_target(self, db) -> str | None:
        for target in _ELIGIBLE:
            if not await db.fetchval("SELECT EXISTS(SELECT 1 FROM memory_recovery_actions WHERE target=$1 AND status IN ('awaiting_cancel','queued'))", target):
                return target
        return None

    async def _settle_operations(self, db, state, grace: int, cooldown: int, circuit: int) -> None:
        rows = await db.fetch("""SELECT action,status,card_token,operation_id,target,created_at,expires_at
                                 FROM memory_recovery_actions WHERE status IN ('awaiting_cancel','queued')
                                 ORDER BY created_at FOR UPDATE""")
        from src.control_executor import _signature
        for row in rows:
            if row["status"] == "awaiting_cancel":
                ignored = await db.fetchval("SELECT resolution='ignore' FROM control_bot_cards WHERE token=$1", row["card_token"])
                now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc)
                if ignored or row["expires_at"] <= now:
                    await db.execute("UPDATE memory_recovery_actions SET status='cancelled',completed_at=now() WHERE card_token=$1", row["card_token"])
                    await db.execute("UPDATE memory_recovery_state SET circuit_open_until=now()+$1::interval,updated_at=now() WHERE singleton=true", f"{circuit} seconds")
                    continue
                if now - row["created_at"] < timedelta(seconds=grace):
                    continue
                key = f"memory-recovery:{row['card_token']}:{row['action']}"
                signature = _signature(key, row["action"], row["target"])
                if not signature:
                    await db.execute("UPDATE memory_recovery_actions SET status='failed',completed_at=now() WHERE card_token=$1", row["card_token"])
                    continue
                operation = await db.fetchrow(
                    """INSERT INTO control_bot_operations(idempotency_key,card_token,action,target,actor_id,actor_username,signature,status,expires_at)
                       VALUES($1,$2,$3,$4,0,'memory-recovery',$5,'queued',$6)
                       ON CONFLICT(idempotency_key) DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING id""",
                    key, row["card_token"], row["action"], row["target"], signature, row["expires_at"],
                )
                await db.execute("UPDATE memory_recovery_actions SET status='queued',operation_id=$2 WHERE card_token=$1", row["card_token"], operation["id"])
                await db.execute("UPDATE control_bot_cards SET resolved_at=now(),resolution='queued' WHERE token=$1 AND resolved_at IS NULL", row["card_token"])
            else:
                operation = await db.fetchrow("SELECT status FROM control_bot_operations WHERE id=$1", row["operation_id"])
                if not operation or operation["status"] not in {"succeeded", "failed", "rejected"}:
                    continue
                ok = operation["status"] == "succeeded"
                await db.execute("UPDATE memory_recovery_actions SET status=$2,completed_at=now() WHERE operation_id=$1", row["operation_id"], "succeeded" if ok else "failed")
                if ok and row["action"] == "pause":
                    await db.execute("UPDATE memory_recovery_state SET paused_target=$1,cooldown_until=now()+$2::interval,updated_at=now() WHERE singleton=true", row["target"], f"{cooldown} seconds")
                elif ok:
                    await db.execute("UPDATE memory_recovery_state SET paused_target=NULL,cooldown_until=now()+$1::interval,updated_at=now() WHERE singleton=true", f"{cooldown} seconds")
                else:
                    await db.execute("UPDATE memory_recovery_state SET circuit_open_until=now()+$1::interval,updated_at=now() WHERE singleton=true", f"{circuit} seconds")

    async def _announce(self, pool, token: str, target: str, action: str, metrics: dict) -> None:
        from src.notifications import telegram
        from src.notifications.collector_bot import build_memory_recovery_keyboard
        text = (f"⚠️ <b>Automatic memory recovery scheduled</b>\n"
                f"Will <b>{action}</b> <code>{target}</code> after the cancel window.\n"
                f"Available memory: <b>{metrics['available_pct']:.1f}%</b>")
        result = await telegram.send_with_keyboard(text, build_memory_recovery_keyboard(token))
        if result.get("ok"):
            from src.notifications.collector_bot import record_card_message
            async with pool.acquire() as db:
                await record_card_message(db, token, (result.get("result") or {}).get("message_id"))
        else:
            logger.warning("memory recovery card delivery failed: %s", result.get("error"))


__all__ = ["MemoryRecoveryHandler", "_available_pct"]
