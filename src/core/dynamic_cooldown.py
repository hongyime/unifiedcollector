"""Persistent adaptive cooldowns for source/account request pressure.

The cursor format intentionally matches the existing Instagram cooldown rows:
``expiry_epoch:streak`` in ``service_cursors.last_processed_id``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
import random
import re
import time

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CooldownState:
    service: str
    active: bool
    seconds_remaining: int
    expires_at_epoch: float
    streak: int
    scope: str | None = None
    account: str | None = None

    @property
    def cursor(self) -> str:
        return f"{int(self.expires_at_epoch)}:{int(self.streak)}"

    @property
    def expires_at(self) -> datetime:
        return datetime.fromtimestamp(self.expires_at_epoch, tz=timezone.utc)


def _slug(value: str | None, *, max_len: int = 16) -> str | None:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return text[:max_len] or None


def dynamic_cooldown_service(source: str, scope: str | None = None, account: str | None = None) -> str:
    source_slug = _slug(source, max_len=18) or "source"
    scope_slug = _slug(scope, max_len=16)
    base = f"{source_slug}_rate_limit"
    if not scope_slug and not account:
        return base[:50]
    digest = hashlib.sha256(f"{scope or ''}:{account or ''}".encode("utf-8")).hexdigest()[:10]
    if scope_slug:
        service = f"{base}:{scope_slug}:{digest}"
    else:
        service = f"{base}:{digest}"
    if len(service) <= 50:
        return service
    return f"{base}:{digest}"[:50]


def parse_cooldown_cursor(raw: str | None, *, now: float | None = None) -> tuple[float, int]:
    if not raw:
        return 0.0, 0
    now = time.time() if now is None else now
    expiry, streak = parse_raw_cooldown_cursor(raw)
    if expiry <= now:
        return 0.0, 0
    return expiry, streak


def parse_raw_cooldown_cursor(raw: str | None) -> tuple[float, int]:
    if not raw:
        return 0.0, 0
    try:
        left, _, right = str(raw).partition(":")
        expiry = float(left)
        streak = int(float(right)) if right else 0
    except (TypeError, ValueError):
        return 0.0, 0
    return expiry, max(0, streak)


async def get_dynamic_cooldown(
    pool,
    *,
    source: str,
    scope: str | None = None,
    account: str | None = None,
    include_source_cursor: bool = False,
) -> CooldownState | None:
    if pool is None:
        return None
    now = time.time()
    services = [dynamic_cooldown_service(source, scope, account)]
    if include_source_cursor:
        source_service = dynamic_cooldown_service(source)
        if source_service not in services:
            services.append(source_service)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT service, last_processed_id
                FROM service_cursors
                WHERE service = ANY($1::text[])
                  AND status = 'blocked'
                """,
                services,
            )
    except Exception:
        logger.debug("dynamic cooldown read failed for %s/%s", source, scope, exc_info=True)
        return None
    best: CooldownState | None = None
    for row in rows:
        expiry, streak = parse_cooldown_cursor(row["last_processed_id"], now=now)
        if expiry <= now:
            continue
        state = CooldownState(
            service=row["service"],
            active=True,
            seconds_remaining=max(0, int(expiry - now)),
            expires_at_epoch=expiry,
            streak=streak,
            scope=scope,
            account=account,
        )
        if best is None or state.expires_at_epoch > best.expires_at_epoch:
            best = state
    return best


async def record_dynamic_cooldown(
    pool,
    *,
    source: str,
    scope: str | None = None,
    account: str | None = None,
    base_seconds: int = 900,
    max_seconds: int = 14_400,
    multiplier: float = 2.0,
    jitter_ratio: float = 0.15,
    write_source_cursor: bool = False,
    memory_seconds: int = 0,
) -> CooldownState:
    now = time.time()
    base_seconds = max(1, int(base_seconds or 1))
    max_seconds = max(base_seconds, int(max_seconds or base_seconds))
    memory_seconds = max(0, int(memory_seconds or 0))
    service = dynamic_cooldown_service(source, scope, account)
    prior_expiry = 0.0
    prior_streak = 0
    prior_raw_expiry = 0.0
    prior_raw_streak = 0
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT last_processed_id FROM service_cursors WHERE service = $1",
                    service,
                )
                raw_cursor = row["last_processed_id"] if row else None
                prior_raw_expiry, prior_raw_streak = parse_raw_cooldown_cursor(raw_cursor)
                prior_expiry, prior_streak = parse_cooldown_cursor(raw_cursor, now=now)
        except Exception:
            logger.debug("dynamic cooldown prior read failed for %s", service, exc_info=True)
    if prior_expiry > now:
        streak = prior_streak + 1
    elif memory_seconds and prior_raw_expiry > 0 and now - prior_raw_expiry <= memory_seconds:
        streak = prior_raw_streak + 1
    else:
        streak = 1
    raw_seconds = float(base_seconds) * (float(multiplier) ** max(0, streak - 1))
    if jitter_ratio > 0:
        raw_seconds *= random.uniform(1.0 - jitter_ratio, 1.0 + jitter_ratio)
    seconds = int(min(max_seconds, max(base_seconds, raw_seconds)))
    expiry = now + seconds
    state = CooldownState(
        service=service,
        active=True,
        seconds_remaining=seconds,
        expires_at_epoch=expiry,
        streak=streak,
        scope=scope,
        account=account,
    )
    if pool is not None:
        services = [service]
        if write_source_cursor:
            source_service = dynamic_cooldown_service(source)
            if source_service not in services:
                services.append(source_service)
        try:
            async with pool.acquire() as conn:
                for cursor_service in services:
                    await conn.execute(
                        """
                        INSERT INTO service_cursors
                            (service, last_processed_id, last_processed_at, status)
                        VALUES ($1, $2, NOW(), 'blocked')
                        ON CONFLICT (service) DO UPDATE
                        SET last_processed_id = EXCLUDED.last_processed_id,
                            last_processed_at = NOW(),
                            status = 'blocked'
                        """,
                        cursor_service,
                        state.cursor,
                    )
        except Exception:
            logger.debug("dynamic cooldown persist failed for %s", service, exc_info=True)
    return state
