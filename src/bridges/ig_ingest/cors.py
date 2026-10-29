"""CORS helpers for the ig_ingest bridge.

Extracted from ``__init__.py`` as part of the PERF-003 package split
(see ``docs/plans/perf-file-splits.md`` §4B). The public shape is preserved:
``__init__.py`` re-exports these names so downstream imports (tests,
docker command, other modules) continue to work.

``_cors`` sets Access-Control-* headers on every response the bridge
returns; ``handle_options`` is the preflight responder.
"""
from aiohttp import web


def _cors(resp: web.Response) -> web.Response:
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-UC-Bridge-Secret"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


async def handle_options(request):
    return _cors(web.Response(status=204))
