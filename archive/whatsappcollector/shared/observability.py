"""
shared/observability.py — Structlog logger setup + Prometheus metric helpers.

Usage:
    from shared.observability import get_logger, make_counter, make_histogram

    logger = get_logger(__name__)
    msg_counter = make_counter("messages_processed_total", "Messages processed", ["queue"])
"""
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

import structlog
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

_configured = False


def configure_logging(log_level: str = "INFO") -> None:
    global _configured
    if _configured:
        return

    level = getattr(logging, log_level.upper(), logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )
    logging.basicConfig(level=level)
    _configured = True


def get_logger(name: str = "app") -> structlog.BoundLogger:
    """Return a structlog bound logger, configuring structlog on first call."""
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    configure_logging(log_level)
    return structlog.get_logger(name)


class HealthAndMetricsHandler(BaseHTTPRequestHandler):
    _health_check_fn: Callable[[], dict] = staticmethod(lambda: {"status": "ok"})

    def do_GET(self):
        if self.path == '/health':
            data = self.__class__._health_check_fn()
            status = 200 if data.get('status') == 'ok' else 503
            body = json.dumps(data).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/metrics':
            output = generate_latest()
            self.send_response(200)
            self.send_header('Content-Type', CONTENT_TYPE_LATEST)
            self.send_header('Content-Length', str(len(output)))
            self.end_headers()
            self.wfile.write(output)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress access logs


class _ReuseAddrHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with SO_REUSEADDR enabled.

    Prevents "Address already in use" on container restart when the previous
    process left a TIME_WAIT socket on the same port.  The class attribute must
    be set before __init__ calls server_bind(), so we use a subclass rather
    than patching the instance after construction.
    """
    allow_reuse_address = True


def start_metrics_server(port: int, health_check_fn: Callable[[], dict] | None = None) -> None:
    """Start the Prometheus HTTP metrics endpoint + /health on the given port."""
    if health_check_fn is not None:
        HealthAndMetricsHandler._health_check_fn = staticmethod(health_check_fn)
    server = _ReuseAddrHTTPServer(('', port), HealthAndMetricsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    get_logger("observability").info("metrics_server_started", port=port)


# ---------------------------------------------------------------------------
# Thin wrappers so callers don't need to import prometheus_client directly
# ---------------------------------------------------------------------------

def make_counter(name: str, documentation: str, labelnames: list[str] | None = None) -> Counter:
    return Counter(name, documentation, labelnames or [])


def make_histogram(
    name: str,
    documentation: str,
    labelnames: list[str] | None = None,
    buckets: tuple | None = None,
) -> Histogram:
    kwargs: dict = {}
    if buckets is not None:
        kwargs["buckets"] = buckets
    return Histogram(name, documentation, labelnames or [], **kwargs)


def make_gauge(name: str, documentation: str, labelnames: list[str] | None = None) -> Gauge:
    return Gauge(name, documentation, labelnames or [])
