"""HostMemoryAlertHandler — Telegram alert on host/VM memory pressure.

Reads ``/proc/meminfo`` (available inside any Linux container on this host;
it reflects the shared WSL2 VM's total memory picture, not the container's
own cgroup limit) and fires a Telegram alert when the percentage of available
memory falls below a configurable threshold.

Background: on 2026-09-27 the shared Docker Desktop WSL2 VM hit 99.7% RAM
utilisation, OOM-killed ``unifiedcollector_postgres``, and nobody was notified
for 46 hours until symptoms cascaded. This handler closes that gap.

Cadence: ``HOST_MEMORY_ALERT_INTERVAL_SECONDS`` (default 3600 = 1h, min 300).
Threshold: ``HOST_MEMORY_ALERT_THRESHOLD_PCT`` (default 15, min 1) — alert
fires when ``MemAvailable / MemTotal * 100`` drops below this value.

Semantics: identical to ``BridgeUnpairedAlertHandler`` and
``PostgresIdleTxnAlertHandler``. The last-fire timestamp is updated **only
after a successful alert send**. If memory is healthy the timer stays put and
the next tick re-probes cheaply. This means the handler never spams Telegram
while memory stays low — it fires once per interval at most.

``/proc/meminfo`` is read synchronously (it is a virtual kernel file, not a
real disk read; the kernel fills it in microseconds). No asyncpg pool access
is needed — this handler is intentionally DB-free so it can fire even when
Postgres itself is the victim of an OOM kill.
"""
from __future__ import annotations

import logging
import time as _time

from src.scheduler.handlers.base import SchedulerContext
from src.notifications import telegram

logger = logging.getLogger(__name__)

_PROC_MEMINFO = "/proc/meminfo"


def _parse_meminfo(text: str) -> dict[str, int]:
    """Parse /proc/meminfo into a {key: kB_value} dict.

    Only lines of the form ``Key:   <integer> kB`` are included; lines
    without a unit (e.g. ``HugePages_Total: 0``) are skipped.
    """
    result: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            key = parts[0].rstrip(":")
            try:
                result[key] = int(parts[1])
            except ValueError:
                pass
    return result


def _read_meminfo(path: str = _PROC_MEMINFO) -> dict[str, int]:
    """Read and parse /proc/meminfo. Returns empty dict on any error."""
    try:
        with open(path) as fh:
            return _parse_meminfo(fh.read())
    except Exception:
        logger.debug("host_memory_alert: could not read %s", path, exc_info=True)
        return {}


class HostMemoryAlertHandler:
    """Send a Telegram alert when host/VM available memory falls below threshold.

    Reads /proc/meminfo which, inside a Docker container on a Linux host or
    WSL2 VM, reflects the *shared* VM-wide memory — not the container's own
    cgroup limit. This is the signal that caught the 2026-09-27 OOM incident
    46 hours too late.
    """

    name = "host_memory_alert"

    def __init__(self) -> None:
        # 0.0 means "no alert has fired yet"; combined with a large
        # ``_time.monotonic()`` this lets the first tick probe immediately.
        self._last_alert: float = 0.0

    async def should_run(self, ctx: SchedulerContext) -> bool:
        interval = ctx.get_env_int(
            "HOST_MEMORY_ALERT_INTERVAL_SECONDS", 3600, min_value=300,
        )
        return _time.monotonic() - self._last_alert >= interval

    async def run(self, ctx: SchedulerContext) -> None:
        threshold_pct = ctx.get_env_int(
            "HOST_MEMORY_ALERT_THRESHOLD_PCT", 15, min_value=1,
        )

        info = _read_meminfo()
        if not info:
            # /proc/meminfo unavailable (e.g. non-Linux CI host). Log and
            # return without updating _last_alert so the next tick retries.
            logger.debug("host_memory_alert: /proc/meminfo unavailable, skipping")
            return

        mem_total = info.get("MemTotal", 0)
        mem_available = info.get("MemAvailable", 0)

        if mem_total <= 0:
            logger.debug("host_memory_alert: MemTotal is zero or missing, skipping")
            return

        available_pct = mem_available / mem_total * 100.0

        if available_pct >= threshold_pct:
            # Deliberately do NOT update ``_last_alert`` so the next tick
            # re-probes. Only a successful alert resets the throttle.
            return

        # Memory is below threshold — update throttle timestamp BEFORE the
        # send attempt so a flaky Telegram outage doesn't cause a spam burst
        # on the next tick. (Mirrors the semantics of PostgresIdleTxnAlertHandler.)
        self._last_alert = _time.monotonic()

        mem_total_gb = mem_total / (1024 * 1024)
        mem_available_gb = mem_available / (1024 * 1024)
        mem_used_gb = (mem_total - mem_available) / (1024 * 1024)

        try:
            await telegram.send(
                f"🔴 <b>Host memory pressure</b>\n"
                f"Available memory has dropped to <b>{available_pct:.1f}%</b> "
                f"(threshold: {threshold_pct}%).\n"
                f"Total: {mem_total_gb:.1f} GB · "
                f"Used: {mem_used_gb:.1f} GB · "
                f"Available: {mem_available_gb:.1f} GB\n"
                f"This is the shared WSL2 VM memory — low values risk OOM-killing "
                f"any container on the host (postgres, collector, etc.).\n"
                f"Check: <code>docker stats --no-stream</code> to find the "
                f"heaviest consumers."
            )
            logger.info(
                "host_memory_alert sent (available_pct=%.1f%%, threshold=%d%%)",
                available_pct, threshold_pct,
            )
        except Exception:
            logger.warning("host_memory_alert send failed", exc_info=True)


__all__ = ["HostMemoryAlertHandler"]
