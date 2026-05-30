"""Centralised logging configuration (P2-3).

Two problems this fixes:

1. ROOT CAUSE of the event-loop freeze. The previous setup attached a plain
   ``logging.StreamHandler`` to stdout and emitted from inside the asyncio event
   loop. ``StreamHandler.emit`` writes synchronously; when the Docker json-file
   log pipe buffer fills (a burst of httpx request lines was enough), the write
   blocks — and because it runs on the event-loop thread, the ENTIRE collector
   freezes, not just logging. Silencing httpx to WARNING (main.py) reduced the
   volume but left the architecture fragile: any future log burst re-triggers it.

   Fix: a ``QueueHandler`` + ``QueueListener``. The event loop only does a
   non-blocking ``queue.put_nowait`` (memory, never blocks on I/O). A dedicated
   background thread (the listener) drains the queue and does the actual stdout
   write. Log I/O is now fully decoupled from the event loop — a stalled pipe
   can at worst grow the queue, never freeze collection.

2. Structured JSON logs with source/account/target context, so operators can
   filter by collector instead of grepping free text. Opt-in via LOG_JSON=true
   (defaults to the human-readable text format for local dev).

Usage (call once at process start, replacing logging.basicConfig):

    from src.core.logging_config import configure_logging
    configure_logging()

Per-record context (optional):

    logger.info("collected", extra={"source": "telegram", "target": "@chan"})
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import sys
from datetime import datetime, timezone

_listener: logging.handlers.QueueListener | None = None

# Standard LogRecord attributes — anything NOT in here that appears on a record
# is treated as structured context and included in the JSON output.
_STD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Pull any extra={...} context (source/account/target/etc.).
        for k, v in record.__dict__.items():
            if k not in _STD_ATTRS and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: int | None = None) -> None:
    """Install a non-blocking queue-based logging pipeline on the root logger.

    Idempotent: safe to call more than once (tears down a prior listener first).
    """
    global _listener

    if level is None:
        level_name = os.getenv("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)

    use_json = os.getenv("LOG_JSON", "false").strip().lower() in {"1", "true", "yes", "on"}

    root = logging.getLogger()
    # Tear down any existing handlers / prior listener (idempotency).
    if _listener is not None:
        try:
            _listener.stop()
        except Exception:
            pass
        _listener = None
    for h in list(root.handlers):
        root.removeHandler(h)

    # The real sink (runs on the listener's background thread, off the event loop).
    sink = logging.StreamHandler(sys.stdout)
    if use_json:
        sink.setFormatter(JsonFormatter())
    else:
        sink.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        ))

    # Unbounded in-memory queue: put_nowait never blocks the event loop.
    log_queue: queue.Queue = queue.Queue(-1)
    qhandler = logging.handlers.QueueHandler(log_queue)

    root.addHandler(qhandler)
    root.setLevel(level)

    _listener = logging.handlers.QueueListener(
        log_queue, sink, respect_handler_level=True
    )
    _listener.start()
    # Flush + stop the listener thread on interpreter exit so buffered records
    # aren't lost on shutdown, without threading shutdown_logging() through every
    # exit path in main.py.
    import atexit
    atexit.register(shutdown_logging)

    # httpx/httpcore stay at WARNING — not because stdout blocks anymore (it
    # doesn't), but because per-request INFO lines are pure noise at this volume.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def shutdown_logging() -> None:
    """Flush and stop the listener thread (call on graceful shutdown)."""
    global _listener
    if _listener is not None:
        try:
            _listener.stop()
        finally:
            _listener = None
