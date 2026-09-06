"""App factory for the ig_ingest bridge.

Extracted from ``__init__.py`` per PERF-003 step 11
(``docs/plans/perf-file-splits.md`` §4B). ``__init__.py`` re-exports
``app`` so external imports (`from src.bridges.ig_ingest import app`) and
``python -m src.bridges.ig_ingest`` (via ``__main__.py``) both continue
to work.

``build_app()`` (also aliased as ``make_app()`` for back-compat) wires:

- ``aiohttp.web.Application`` with ``client_max_size`` + the three
  middlewares from ``.middleware``.
- Preflight ``OPTIONS`` responder from ``.cors``.
- All route registrations, delegated to handlers that live in dedicated
  modules (``.targets``, ``.discover``, ``.ingest``, ``.x_profile``,
  ``.revisit``, ``.dm``, ``.strava``, ``.cookies``, ``.telemetry``,
  ``.cooldown``) and to legacy handlers still in ``__init__.py``
  (``target_status_handler``, ``posts_handler``, etc.).
- ``on_startup`` / ``on_cleanup`` hooks that live in ``__init__.py``.
"""
from aiohttp import web

from .constants import SOCIAL_INGEST_CLIENT_MAX_MB
from .cookies import cookies_handler
from .cooldown import ig_cooldown
from .cors import handle_options
from .discover import discover, discover_ig
from .dm import (
    dm_decoded_handler,
    dm_frame_handler,
    dm_hook_heartbeat_handler,
    dm_probe_handler,
    dm_sample_handler,
)
from .ingest import (
    browser_media_candidates,
    ingest,
    ingest_ig,
    ingest_upload,
    ingest_upload_binary,
)
from .middleware import (
    db_pool_middleware,
    lane_isolation_middleware,
    request_timeout_middleware,
)
from .revisit import (
    browser_revisit_result,
    browser_revisit_target,
    tiktok_revisit_result,
    tiktok_revisit_target,
)
from .strava import (
    strava_route_queue_handler,
    strava_route_visit_handler,
    strava_streams_handler,
)
from .targets import get_targets, get_targets_ig
from .telemetry import browser_heartbeat_handler, sw_crash_handler
from .x_profile import x_profile_target_next, x_profile_target_result


def build_app():
    """Assemble the aiohttp ``Application`` with routes + middlewares."""
    # Handlers still living in ``__init__.py`` (target_status_handler,
    # posts_handler, comments_handler, users_handler, profile_handler,
    # seed_handler, dms_handler, health, _on_startup, _on_cleanup) are
    # imported lazily to avoid the circular that would form if this file
    # were imported at ``__init__.py`` load time.
    from . import (
        _on_cleanup,
        _on_startup,
        comments_handler,
        dms_handler,
        health,
        posts_handler,
        profile_handler,
        seed_handler,
        target_status_handler,
        users_handler,
    )

    app = web.Application(
        client_max_size=SOCIAL_INGEST_CLIENT_MAX_MB * 1024 * 1024,
        middlewares=[request_timeout_middleware, lane_isolation_middleware, db_pool_middleware],
    )
    app.router.add_route("OPTIONS", "/{tail:.*}", handle_options)
    # generic multi-platform
    app.router.add_get("/social/targets", get_targets)
    app.router.add_get("/social/ig_cooldown", ig_cooldown)
    app.router.add_post("/social/ingest", ingest)
    app.router.add_post("/social/ingest-upload", ingest_upload)
    app.router.add_post("/social/ingest-upload-binary", ingest_upload_binary)
    app.router.add_post("/social/browser-media-candidates", browser_media_candidates)
    app.router.add_post("/social/discover", discover)
    app.router.add_post("/social/target-status", target_status_handler)
    app.router.add_post("/social/posts", posts_handler)
    app.router.add_post("/social/comments", comments_handler)
    app.router.add_post("/social/users", users_handler)
    app.router.add_post("/social/profile", profile_handler)
    app.router.add_post("/social/seed", seed_handler)
    app.router.add_post("/social/dms", dms_handler)
    app.router.add_post("/social/cookies", cookies_handler)
    app.router.add_post("/social/dm-frame", dm_frame_handler)
    app.router.add_post("/social/dm-sample", dm_sample_handler)
    app.router.add_post("/social/dm-probe", dm_probe_handler)
    app.router.add_post("/social/dm-heartbeat", dm_hook_heartbeat_handler)
    app.router.add_post("/social/dm-decoded", dm_decoded_handler)
    app.router.add_get("/social/x-profile-target", x_profile_target_next)
    app.router.add_post("/social/x-profile-target-result", x_profile_target_result)
    app.router.add_get("/social/browser-revisit-target", browser_revisit_target)
    app.router.add_post("/social/browser-revisit-result", browser_revisit_result)
    app.router.add_get("/social/tiktok-revisit-target", tiktok_revisit_target)
    app.router.add_post("/social/tiktok-revisit-result", tiktok_revisit_result)
    app.router.add_get("/social/strava-route-queue", strava_route_queue_handler)
    app.router.add_post("/social/strava-route-visit", strava_route_visit_handler)
    app.router.add_post("/social/strava-streams", strava_streams_handler)
    app.router.add_post("/social/browser-heartbeat", browser_heartbeat_handler)
    app.router.add_post("/social/sw-crash", sw_crash_handler)
    # instagram back-compat aliases
    app.router.add_get("/ig/targets", get_targets_ig)
    app.router.add_post("/ig/ingest", ingest_ig)
    app.router.add_post("/ig/discover", discover_ig)
    app.router.add_get("/health", health)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


# Back-compat alias — the original module exported ``make_app``. Keep it so
# any legacy caller (or a future test) can still call it.
make_app = build_app
