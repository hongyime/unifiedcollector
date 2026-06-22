"""Container healthcheck — must stay LIGHT and TOLERANT.

The Docker healthcheck runs this as a fresh subprocess every 60s. The old version
called get_pool(), which builds a full asyncpg pool each time; under heavy DB load
(e.g. a big backfill saturating connections) that connect would time out and flip
healthy collectors to "unhealthy" even though they were working fine.

This version opens ONE short-lived connection with a tight timeout and a couple of
retries, so transient connection pressure doesn't cause false negatives — but a
genuinely-down DB still fails (correct). Tuned to finish well under the compose
healthcheck timeout (30s).
"""
import asyncio
import sys

import asyncpg

from src.db.connection import _dsn, _ssl_context

ATTEMPTS = 3
CONNECT_TIMEOUT = 6.0
QUERY_TIMEOUT = 4.0
RETRY_SLEEP = 2.0


async def _check():
    ssl = _ssl_context()
    kwargs = {}
    if ssl is not None and ssl != "prefer":
        kwargs["ssl"] = ssl
    last = None
    for attempt in range(ATTEMPTS):
        try:
            conn = await asyncio.wait_for(asyncpg.connect(_dsn(), **kwargs), timeout=CONNECT_TIMEOUT)
            try:
                await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=QUERY_TIMEOUT)
                return
            finally:
                await conn.close()
        except Exception as e:  # transient connection pressure / timeout -> retry
            last = e
            if attempt < ATTEMPTS - 1:
                await asyncio.sleep(RETRY_SLEEP)
    raise last if last else RuntimeError("healthcheck failed")


def health_check():
    try:
        asyncio.run(_check())
    except Exception as e:
        print(f"UNHEALTHY: {e}", file=sys.stderr)
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    health_check()
