"""DB-pool bootstrap and lifecycle helpers for the ig_ingest bridge.

Extracted from ``__init__.py`` per PERF-003 step 3
(``docs/plans/perf-file-splits.md`` §4B). The pool bootstraps lazily on the
first non-fastlane request (see ``middleware.db_pool_middleware``) so browser
tabs don't block on boot while Postgres is still cold.

Public surface:

- ``_PoolRef``       — mutable holder that survives aiohttp's frozen-app
                       constraint.
- ``_startup_state`` — inspect current startup dict on ``app``.
- ``_set_startup_error`` / ``_set_startup_pending`` — mutate the dict.
- ``_ensure_app_pool`` — idempotent bootstrap; raises ``TimeoutError`` if it
                        can't reach Postgres inside the configured budget.
- ``_schedule_app_task`` — fire-and-forget background task with error
                           containment.

All these names are re-exported from ``__init__.py`` for test back-compat.
"""
import asyncio
import logging

from src.db.connection import get_pool

from .constants import SOCIAL_INGEST_DB_INIT_TIMEOUT_SECONDS


logger = logging.getLogger("social_ingest")


class _PoolRef:
    """Mutable DB-pool holder.

    aiohttp freezes top-level app state after startup. The ingest bridge prepares
    DB schema in a background task so browser tabs are not blocked on boot; keep
    the top-level key stable and mutate this holder instead.
    """

    def __init__(self):
        self.pool = None

    def __bool__(self):
        return self.pool is not None

    def acquire(self):
        if self.pool is None:
            raise RuntimeError("db_pool_not_ready")
        return self.pool.acquire()


def _startup_state(app):
    state = app.get("startup_state")
    if isinstance(state, dict):
        return state
    return {"error": app.get("startup_error"), "pending": bool(app.get("startup_pending"))}


def _set_startup_error(app, value) -> None:
    _startup_state(app)["error"] = value


def _set_startup_pending(app, value: bool) -> None:
    _startup_state(app)["pending"] = bool(value)


async def _ensure_app_pool(app):
    holder = app.get("pool")
    if isinstance(holder, _PoolRef):
        if holder.pool is not None:
            return holder
        async with asyncio.timeout(SOCIAL_INGEST_DB_INIT_TIMEOUT_SECONDS):
            holder.pool = await get_pool()
        _set_startup_error(app, None)
        return holder
    if holder is not None:
        return holder
    async with asyncio.timeout(SOCIAL_INGEST_DB_INIT_TIMEOUT_SECONDS):
        app["pool"] = await get_pool()
    _set_startup_error(app, None)
    return app["pool"]


def _schedule_app_task(app, coro, label: str) -> None:
    """Run best-effort bridge work without holding the browser request open."""
    async def _runner():
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("%s background task failed", label, exc_info=True)

    task = asyncio.create_task(_runner())
    app.setdefault("tasks", set()).add(task)
    task.add_done_callback(app["tasks"].discard)
