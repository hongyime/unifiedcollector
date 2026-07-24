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
import os
import ssl as _ssl
import sys

import asyncpg

ATTEMPTS = 3
CONNECT_TIMEOUT = 6.0
QUERY_TIMEOUT = 4.0
RETRY_SLEEP = 2.0


def _dsn() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql://collector:***@localhost:5432/unifiedcollector",
    )


def _ssl_context():
    mode = os.environ.get("POSTGRES_SSL_MODE", "")
    if not mode or mode == "disable":
        return None
    if mode == "prefer":
        return "prefer"
    ctx = _ssl.create_default_context()
    cert = os.environ.get("POSTGRES_SSL_CERT", "")
    if cert:
        ctx.load_verify_locations(cert)
    elif mode == "require-noverify":
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
    return ctx


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
