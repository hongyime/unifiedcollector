"""PostgresIdleTxnAlertHandler — Telegram alert on stuck idle-in-transaction sessions.

Companion to the pool-factory `idle_in_transaction_session_timeout` guard
added in `src/db/connection.py` (2026-09-07). Postgres will kill sessions
that stay idle-in-transaction longer than 5 minutes by default, but the
underlying leak is a code defect that should get fixed at the source. This
handler surfaces the leak to the operator so a bounce isn't the fix.

Original context: the drain-rate audit caught 6+ sessions stuck idle-in-
transaction for >20 minutes, all executing an analyzer query against
github_commits. Trace: unifiedanalyzer's build_timeline holds a collector-
pool cursor open while awaiting an analyzer-pool executemany, which
Postgres classifies as idle-in-transaction from the collector's side.

Cadence: `PG_IIT_ALERT_INTERVAL_SECONDS` (default 3600 = 1h, min 300).
Threshold: `PG_IIT_ALERT_AGE_MINUTES` (default 10, min 1).

Semantics: same as BridgeUnpairedAlertHandler. Last-fire timestamp updates
only after a successful alert send, so a healthy stretch re-probes cheaply
every tick without spamming Telegram.
"""
from __future__ import annotations

import logging
import time as _time

from src.scheduler.handlers.base import SchedulerContext

logger = logging.getLogger(__name__)


class PostgresIdleTxnAlertHandler:
    """Alert when Postgres has sessions stuck 'idle in transaction' past a threshold."""

    name = "postgres_idle_txn_alert"

    def __init__(self) -> None:
        self._last_alert: float = 0.0

    async def should_run(self, ctx: SchedulerContext) -> bool:
        interval = ctx.get_env_int(
            "PG_IIT_ALERT_INTERVAL_SECONDS", 3600, min_value=300,
        )
        return _time.monotonic() - self._last_alert >= interval

    async def run(self, ctx: SchedulerContext) -> None:
        age_minutes = ctx.get_env_int(
            "PG_IIT_ALERT_AGE_MINUTES", 10, min_value=1,
        )
        try:
            async with ctx.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT pid,
                           COALESCE(NULLIF(application_name, ''), '<unnamed>') AS app,
                           COALESCE(client_addr::text, 'local') AS client,
                           EXTRACT(EPOCH FROM (NOW() - state_change))/60 AS age_min,
                           LEFT(COALESCE(query, ''), 80) AS query_snippet
                    FROM pg_stat_activity
                    WHERE state = 'idle in transaction'
                      AND state_change < NOW() - ($1 || ' minutes')::interval
                    ORDER BY state_change ASC
                    """,
                    str(age_minutes),
                )
        except Exception:
            logger.debug("postgres_idle_txn probe query failed", exc_info=True)
            return
        if not rows:
            return
        self._last_alert = _time.monotonic()
        try:
            from src.notifications import telegram as tg
            lines = [
                "⚠️ <b>Postgres idle-in-transaction leak</b>",
                f"{len(rows)} session(s) stuck idle-in-transaction >{age_minutes}m:",
            ]
            for r in rows[:8]:
                lines.append(
                    f"• pid={r['pid']} app={r['app']} client={r['client']} "
                    f"age={r['age_min']:.1f}m"
                )
                # Query snippet on its own line so long ones don't blow up
                # the message envelope; HTML-escape aggressive characters.
                snippet = str(r["query_snippet"]).replace("&", "&amp;").replace("<", "&lt;")
                lines.append(f"  <code>{snippet}</code>")
            if len(rows) > 8:
                lines.append(f"… and {len(rows) - 8} more.")
            lines.append(
                "\nDB pool factory kills sessions past "
                "<code>DB_IDLE_IN_TRANSACTION_TIMEOUT_MS</code> "
                "(default 5m) — this alert exists to catch the leak "
                "at its source before Postgres has to reap."
            )
            await tg.send("\n".join(lines))
            logger.info(
                "postgres_idle_txn alert sent (n=%d, age_threshold=%dm)",
                len(rows), age_minutes,
            )
        except Exception:
            logger.warning("postgres_idle_txn alert send failed", exc_info=True)


__all__ = ["PostgresIdleTxnAlertHandler"]
