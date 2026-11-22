"""Browser capture archiving.

Split out of ``__init__.py`` (split step 11); re-exported there.
"""
import logging
from ._ids import _norm_platform
from .constants import KNOWN_PLATFORMS
from .constants import _SAFE
import time

logger = logging.getLogger("social_ingest")


async def _archive_browser_capture(pool, platform: str, endpoint: str, body: dict) -> None:
    """Best-effort vault raw archive for extension/browser evidence.

    Do not call this for credential endpoints. Cookies stay in credential files,
    never in raw payloads or sidecars.
    """
    from . import write_raw_payload
    if endpoint == "cookies":
        return
    if not isinstance(body, dict):
        return
    source = _norm_platform(platform)
    if source not in KNOWN_PLATFORMS:
        return
    subject = _SAFE.sub("_", _browser_capture_subject(endpoint, body))[:96] or endpoint
    artifact_id = f"extension/{endpoint}/{subject}/{time.time_ns()}"
    owner = body.get("owner")
    if isinstance(owner, dict):
        collection_account = owner.get("username") or owner.get("id")
    else:
        collection_account = owner if isinstance(owner, str) else None
    metadata = {
        "ingest_path": "extension",
        "endpoint": endpoint,
        "platform": source,
        "collection_account": collection_account,
        "extension_version": body.get("extension_version"),
        "request_url": body.get("request_url") or body.get("url"),
        "http_status": body.get("http_status"),
        "body_keys": sorted(str(k) for k in body.keys()),
    }
    target_tables = _BROWSER_CAPTURE_TARGET_TABLES.get(endpoint, {}).get(source, [])
    result = write_raw_payload(
        source=source,
        artifact_id=artifact_id,
        payload=body,
        metadata=metadata,
        target_tables=target_tables,
        extension="json.gz" if endpoint in _BROWSER_CAPTURE_COMPRESSED_ENDPOINTS else "json",
    )
    if result.ok:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    DELETE FROM dead_letter_queue
                    WHERE source = $1
                      AND entity_id = $2
                      AND status IN ('pending', 'in_progress')
                      AND error_message LIKE 'browser raw archive failed:%'
                    """,
                    source,
                    subject,
                )
        except Exception:
            logger.debug(
                "browser raw archive stale-DLQ cleanup failed platform=%s endpoint=%s subject=%s",
                source,
                endpoint,
                subject,
                exc_info=True,
            )
        return
    logger.warning(
        "browser raw archive failed platform=%s endpoint=%s artifact=%s: %s",
        source,
        endpoint,
        artifact_id,
        result.error,
    )
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO dead_letter_queue (source, entity_id, content_id, error_message)
                VALUES ($1, $2, $3, $4)
                """,
                source,
                subject,
                artifact_id,
                f"browser raw archive failed: {result.error}"[:500],
            )
    except Exception:
        logger.debug(
            "browser raw archive DLQ insert failed platform=%s endpoint=%s",
            source,
            endpoint,
            exc_info=True,
        )


def _browser_capture_subject(endpoint: str, body: dict) -> str:
    if endpoint == "profile":
        profile = body.get("profile") if isinstance(body.get("profile"), dict) else {}
        return str(
            profile.get("username")
            or profile.get("user_id")
            or profile.get("platform_user_id")
            or "profile"
        ).lstrip("@")
    if endpoint == "posts":
        posts = body.get("posts") if isinstance(body.get("posts"), list) else []
        first = posts[0] if posts and isinstance(posts[0], dict) else {}
        return str(first.get("platform_post_id") or first.get("content_id") or f"{len(posts)}_posts")
    if endpoint == "comments":
        return str(body.get("post_id") or "comments")
    if endpoint == "strava_streams":
        return str(body.get("activity_id") or body.get("platform_activity_id") or "strava_streams")
    return endpoint


_BROWSER_CAPTURE_TARGET_TABLES = {
    "profile": {
        "instagram": ["instagram_profiles"],
        "x": ["x_profiles"],
        "facebook": ["facebook_profiles"],
    },
    "posts": {
        "instagram": ["instagram_posts"],
        "threads": ["threads_posts"],
        "facebook": ["facebook_posts"],
        "x": ["x_posts"],
    },
    "comments": {
        "instagram": ["instagram_comments"],
    },
    "strava_streams": {
        "strava": ["strava_activities", "strava_gps_streams"],
    },
}


_BROWSER_CAPTURE_COMPRESSED_ENDPOINTS = {"profile", "posts", "comments"}
