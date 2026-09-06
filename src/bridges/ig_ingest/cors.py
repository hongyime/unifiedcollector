"""CORS helpers for the ig_ingest bridge.

Extracted from ``__init__.py`` as part of the PERF-003 package split
(see ``docs/plans/perf-file-splits.md`` §4B). The public shape is preserved:
``__init__.py`` re-exports these names so downstream imports (tests,
docker command, other modules) continue to work.

``_cors`` sets Access-Control-* headers on every response the bridge
returns; ``handle_options`` is the wildcard preflight responder.
"""
from aiohttp import web


def _cors(resp: web.Response) -> web.Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    # Chrome's Private Network Access (PNA) blocks HTTPS pages from fetching
    # loopback (127.0.0.1) unless the server opts in with this header. Some
    # origins (observed: https://www.lemon8-app.com) get PNA-enforced for
    # extension content-script fetches even though the extension declares
    # http://127.0.0.1/* in host_permissions — so the extension's direct-fetch
    # heartbeat fallback fails silently with `ERR net::ERR_FAILED` /
    # "Permission was denied for this request to access the `loopback` address
    # space." Opting in unblocks lemon8 while remaining safe for other
    # platforms (we never accept cross-origin credentialed requests: fetches
    # from content.js don't send cookies).
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp


async def handle_options(request):
    return _cors(web.Response(status=204))
