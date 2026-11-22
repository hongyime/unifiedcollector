"""Post / comment / user persistence.

Split out of ``__init__.py`` (split step 11); re-exported there.
"""
import logging
from ._handles import _derive_handle_from_caption
from ._handles import _enqueue_lemon8_profile_targets
from ._handles import _enqueue_tiktok_profile_targets
from ._handles import _enqueue_x_profile_targets
from ._handles import _ensure_ig_profile
from ._handles import _owner_account_for_follow
from ._handles import _record_x_post_graph
from ._ids import _VERIFIED_FILTER_PLATFORMS
from ._ids import _explicitly_verified_capture
from ._ids import _verify_url
from ._util import _int
from ._util import _num
from src.core.collection_surface import normalize_collection_surface
import json
import re

logger = logging.getLogger("social_ingest")


# ---------------------------------------------------------------------------
# structured post + comment metadata (captions/likes/comments threads)
# ---------------------------------------------------------------------------
async def _save_posts(pool, platform, posts, collection_surface: str = "unknown") -> int:
    """Upsert post metadata (caption, engagement, hashtags, mentions) into the
    platform's posts table. instagram_posts has the richest schema; threads_posts
    and facebook_posts mirror the essentials. tiktok/lemon8 posts are owned by
    their headless collectors, so we don't double-write them here."""
    if platform not in ("instagram", "threads", "facebook", "x"):
        return 0
    n = 0
    default_surface = normalize_collection_surface(collection_surface)
    async with pool.acquire() as conn:
        for p in posts:
            if platform in _VERIFIED_FILTER_PLATFORMS and _explicitly_verified_capture(p):
                continue
            ppid = str(p.get("platform_post_id") or "")
            if not ppid:
                continue
            post_metadata = p.get("metadata") if isinstance(p.get("metadata"), dict) else {}
            post_surface = normalize_collection_surface(
                p.get("collection_surface") or post_metadata.get("collection_surface") or default_surface
            )
            # stamp a canonical, openable verification URL + correct shortcode into
            # metadata (overrides any wrong/long "code" the extension may have sent).
            vurl = _verify_url(platform, ppid)
            if vurl:
                _md = p.get("metadata")
                if not isinstance(_md, dict):
                    _md = {}
                _md["verify_url"] = vurl
                _md["shortcode"] = vurl.rstrip("/").rsplit("/", 1)[-1]
                p["metadata"] = _md
            try:
                if platform == "instagram":
                    # Root-cause fix (NULL-FK bug): resolve/create the author
                    # profile and attach profile_id at insert time so the post is
                    # never orphaned. COALESCE on UPDATE so a later collection that
                    # already set profile_id is never clobbered back to NULL.
                    profile_id = await _ensure_ig_profile(conn, ppid, p)
                    await conn.execute(
                        """
                        INSERT INTO instagram_posts
                          (id, platform_post_id, profile_id, media_type, caption, hashtags, mentions,
                           location_name, location_lat, location_lng, music_title, music_author,
                           likes_count, comments_count, video_duration,
                           platform_created_at, collected_at, metadata, collection_surface)
                        VALUES (gen_random_uuid(),$1,$2::uuid,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,
                                to_timestamp($15), now(), $16::jsonb, $17)
                        ON CONFLICT (platform_post_id) DO UPDATE SET
                           profile_id=COALESCE(instagram_posts.profile_id, EXCLUDED.profile_id),
                           caption=EXCLUDED.caption, likes_count=EXCLUDED.likes_count,
                           comments_count=EXCLUDED.comments_count, hashtags=EXCLUDED.hashtags,
                           mentions=EXCLUDED.mentions, location_name=EXCLUDED.location_name,
                           location_lat=COALESCE(EXCLUDED.location_lat, instagram_posts.location_lat),
                           location_lng=COALESCE(EXCLUDED.location_lng, instagram_posts.location_lng),
                           music_title=COALESCE(EXCLUDED.music_title, instagram_posts.music_title),
                           music_author=COALESCE(EXCLUDED.music_author, instagram_posts.music_author),
                           collected_at=now(), metadata=EXCLUDED.metadata,
                           collection_surface=CASE WHEN EXCLUDED.collection_surface='unknown'
                                                   THEN instagram_posts.collection_surface
                                                   ELSE EXCLUDED.collection_surface END
                        """,
                        ppid, profile_id, p.get("media_type"), p.get("caption"),
                        p.get("hashtags") or [], p.get("mentions") or [],
                        p.get("location"), _num(p.get("location_lat")), _num(p.get("location_lng")),
                        p.get("music_title"), p.get("music_author"),
                        _int(p.get("likes_count")), _int(p.get("comments_count")),
                        _int(p.get("video_duration")), _num(p.get("taken_at")),
                        json.dumps(p.get("metadata") or {}), post_surface,
                    )
                elif platform == "x":
                    await conn.execute(
                        """
                        INSERT INTO x_posts
                          (platform_post_id, author_username, caption, hashtags, mentions,
                           likes_count, comments_count, reposts_count, quote_count, views_count,
                           media_type, platform_created_at, collected_at, metadata, collection_surface)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,to_timestamp($12),now(),$13::jsonb,$14)
                        ON CONFLICT (platform_post_id) DO UPDATE SET
                           caption=EXCLUDED.caption, likes_count=EXCLUDED.likes_count,
                           comments_count=EXCLUDED.comments_count, reposts_count=EXCLUDED.reposts_count,
                           quote_count=EXCLUDED.quote_count, views_count=EXCLUDED.views_count,
                           hashtags=EXCLUDED.hashtags, mentions=EXCLUDED.mentions,
                           media_type=COALESCE(EXCLUDED.media_type, x_posts.media_type),
                           platform_created_at=COALESCE(EXCLUDED.platform_created_at, x_posts.platform_created_at),
                           collected_at=now(), metadata=x_posts.metadata || EXCLUDED.metadata,
                           collection_surface=CASE WHEN EXCLUDED.collection_surface='unknown'
                                                   THEN x_posts.collection_surface
                                                   ELSE EXCLUDED.collection_surface END
                        """,
                        ppid, p.get("author_username"), p.get("caption"),
                        p.get("hashtags") or [], p.get("mentions") or [],
                        _int(p.get("likes_count")), _int(p.get("comments_count")), _int(p.get("reposts_count")),
                        _int(p.get("quote_count")), _int(p.get("views_count")), p.get("media_type"),
                        _num(p.get("taken_at")), json.dumps(p.get("metadata") or {}), post_surface,
                    )
                    await _record_x_post_graph(conn, ppid, p)
                else:  # threads / facebook
                    table = "threads_posts" if platform == "threads" else "facebook_posts"
                    extra_col = "reposts_count" if platform == "threads" else "shares_count"
                    # Self-heal: the threads/facebook extension scraper sometimes
                    # leaves author_username empty and puts "<handle>\n<time>\n..."
                    # in the caption. Recover the handle so the row isn't orphaned
                    # (was ~56% of threads posts). See _derive_handle_from_caption.
                    if not p.get("author_username"):
                        _h = _derive_handle_from_caption(p.get("caption"))
                        if _h:
                            p["author_username"] = _h
                    await conn.execute(
                        f"""
                        INSERT INTO {table}
                          (platform_post_id, author_username, caption, hashtags, mentions,
                           likes_count, comments_count, {extra_col}, media_type,
                           platform_created_at, collected_at, metadata, collection_surface)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,to_timestamp($10),now(),$11::jsonb,$12)
                        ON CONFLICT (platform_post_id) DO UPDATE SET
                           author_username=COALESCE(NULLIF(EXCLUDED.author_username, ''), {table}.author_username),
                           caption=EXCLUDED.caption, likes_count=EXCLUDED.likes_count,
                           comments_count=EXCLUDED.comments_count,
                           {extra_col}=EXCLUDED.{extra_col}, collected_at=now(),
                           metadata=EXCLUDED.metadata,
                           collection_surface=CASE WHEN EXCLUDED.collection_surface='unknown'
                                                   THEN {table}.collection_surface
                                                   ELSE EXCLUDED.collection_surface END
                        """,
                        ppid, p.get("author_username"), p.get("caption"),
                        p.get("hashtags") or [], p.get("mentions") or [],
                        _int(p.get("likes_count")), _int(p.get("comments_count")),
                        _int(p.get("reposts_count") if platform == "threads" else p.get("shares_count")),
                        p.get("media_type"), _num(p.get("taken_at")),
                        json.dumps(p.get("metadata") or {}), post_surface,
                    )
                n += 1
            except Exception:
                logger.debug("save post failed %s/%s", platform, ppid, exc_info=True)
    return n


async def _save_comments(pool, platform, post_pid, comments) -> int:
    """Upsert comment threads into instagram_comments, linked to their post."""
    if platform != "instagram":
        return 0
    n = 0
    async with pool.acquire() as conn:
        for c in comments:
            cpid = str(c.get("platform_comment_id") or "")
            if not cpid:
                continue
            try:
                await conn.execute(
                    """
                    INSERT INTO instagram_comments
                      (id, platform_comment_id, post_id, author_username, author_platform_id,
                       text, like_count, parent_comment_id, is_reply, platform_created_at, collected_at)
                    VALUES (gen_random_uuid(),$1,
                       (SELECT id FROM instagram_posts WHERE platform_post_id=$2),
                       $3,$4,$5,$6,$7,$8,to_timestamp($9),now())
                    ON CONFLICT (platform_comment_id) DO UPDATE SET
                       text = EXCLUDED.text, like_count = EXCLUDED.like_count, collected_at = now()
                    """,
                    cpid, str(post_pid or ""), c.get("author_username"), c.get("author_platform_id"),
                    c.get("text"), _int(c.get("like_count")), c.get("parent_comment_id"),
                    bool(c.get("is_reply")), _num(c.get("created_at")),
                )
                n += 1
            except Exception:
                logger.debug("save comment failed %s", cpid, exc_info=True)
    return n


# ---------------------------------------------------------------------------
# universal user registry — every user/id we encounter anywhere (follows graph,
# comment authors, tagged users, post authors, reactors) lands in social_users.
# ---------------------------------------------------------------------------
# A Threads handle IS the same Meta account's Instagram handle. So real people we
# see on Threads (your Following feed, comment authors, etc.) are scrapeable on
# Instagram — which IS target/profile-driven. Cross-seed them into the IG spider
# queue. Gated to non-"foryou" contexts so we don't import algorithmic brand spam.
async def _cross_seed_instagram(conn, usernames, source) -> int:
    clean = sorted({
        (uname or "").strip().lstrip("@")
        for uname in usernames
        if (uname or "").strip().lstrip("@") and len((uname or "").strip().lstrip("@")) <= 30
    })
    if not clean:
        return 0
    try:
        added = int(await conn.fetchval(
            """
            WITH input(username) AS (
                SELECT DISTINCT unnest($1::text[])
            ),
            inserted AS (
                INSERT INTO instagram_spider_targets (username, hop, discovered_from)
                SELECT username, 1, $2
                FROM input
                ON CONFLICT (username) DO NOTHING
                RETURNING 1
            )
            SELECT count(*) FROM inserted
            """,
            clean,
            source,
        ) or 0)
    except Exception:
        logger.debug("cross-seed ig target batch failed", exc_info=True)
        added = 0
        for uname in clean:
            try:
                res = await conn.execute(
                    """
                    INSERT INTO instagram_spider_targets (username, hop, discovered_from)
                    VALUES ($1, 1, $2)
                    ON CONFLICT (username) DO NOTHING
                    """,
                    uname, source,
                )
                if res.endswith("1"):
                    added += 1
            except Exception:
                logger.debug("cross-seed ig target failed %s", uname, exc_info=True)
    if added:
        logger.info("cross-seed instagram <- %s: +%d real handles", source, added)
    return added


def _normalize_user_rows(platform, users):
    """Normalize raw user payloads into social_users row dicts, dropping verified captures."""
    rows = []
    for u in users:
        if isinstance(u, str):
            u = {"username": u}
        if not isinstance(u, dict):
            continue
        if platform in _VERIFIED_FILTER_PLATFORMS and _explicitly_verified_capture(u):
            continue
        user_id = u.get("user_id") or u.get("pk") or u.get("id")
        username = (u.get("username") or "").strip().lstrip("@") or None
        uid = str(user_id) if user_id else username
        if not uid:
            continue
        rows.append({
            "uid": uid,
            "user_id": str(user_id) if user_id else None,
            "username": username,
            "display_name": u.get("display_name") or u.get("full_name") or None,
            "profile_photo_url": (
                u.get("profile_pic_url")
                or u.get("profile_photo_url")
                or u.get("avatar_url")
                or None
            ),
            "source_record_id": str(
                u.get("source_record_id") or u.get("post_id")
                or u.get("comment_id") or ""
            ).strip() or None,
        })
    return rows


async def _upsert_social_users(conn, platform, rows, user_args):
    """Batch upsert social_users; per-row fallback. Returns (n, successful_rows)."""
    successful_rows = rows
    n = 0
    try:
        await conn.executemany(_SOCIAL_USERS_UPSERT_SQL, user_args)
        n = len(rows)
    except Exception:
        logger.debug("record users batch failed %s/%d", platform, len(rows), exc_info=True)
        successful_rows = []
        for r, args in zip(rows, user_args, strict=False):
            try:
                await conn.execute(_SOCIAL_USERS_UPSERT_SQL, *args)
                successful_rows.append(r)
                n += 1
            except Exception:
                logger.debug("record user failed %s", r["uid"], exc_info=True)
    return n, successful_rows


async def _upsert_follow_edges(conn, platform, rows, edge_args, owner_account, direction) -> None:
    """Upsert follow_edges + normalized spider_graph_edges. Never raises."""
    if not edge_args:
        return
    try:
        await conn.executemany(_FOLLOW_EDGES_UPSERT_SQL, edge_args)
    except Exception:
        logger.debug("record follow edges batch failed %s/%d", platform, len(edge_args), exc_info=True)
        for args in edge_args:
            try:
                await conn.execute(_FOLLOW_EDGES_UPSERT_SQL, *args)
            except Exception:
                logger.debug("record follow edge failed %s", args[2], exc_info=True)
    if edge_args and owner_account and direction:
        try:
            from src.core.spider_graph_store import shared_spider_enabled

            if shared_spider_enabled(platform):
                graph_args = [
                    (
                        platform,
                        owner_account,
                        r["uid"],
                        direction,
                        "browser_visible_follow",
                        0,
                        json.dumps({"target_username": r["username"]}),
                    )
                    for r in rows
                ]
                await conn.executemany(
                    """
                    INSERT INTO spider_graph_edges (
                        platform, source_node_id, target_node_id, edge_type,
                        discovery_type, hop_distance, source_metadata,
                        first_seen_at, last_seen_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, NOW(), NOW())
                    ON CONFLICT (platform, source_node_id, target_node_id, edge_type)
                    DO UPDATE SET
                        discovery_type = EXCLUDED.discovery_type,
                        source_metadata = spider_graph_edges.source_metadata || EXCLUDED.source_metadata,
                        last_seen_at = NOW()
                    """,
                    graph_args,
                )
        except Exception:
            logger.debug(
                "record normalized visible graph failed platform=%s",
                platform,
                exc_info=True,
            )


async def _enqueue_profile_targets(conn, platform, usernames, ctx, context_value, target_metadata) -> None:
    """Enqueue per-platform profile targets for the recorded users. Never raises."""
    try:
        if platform == "x" and usernames:
            if ctx == "follow":
                source, priority = "following", 95
            elif ctx == "follower":
                source, priority = "follower", 85
            elif ctx == "author":
                source, priority = "author", 75
            else:
                source, priority = ctx[:64], 60
            await _enqueue_x_profile_targets(conn, usernames, source, priority, target_metadata)
        elif platform == "tiktok" and usernames:
            if ctx in {"follow", "following"}:
                source, priority = "following", 2
            elif ctx == "follower":
                source, priority = "follower", 3
            elif ctx in {"author", "profile", "post"}:
                source, priority = ctx, 3
            else:
                source, priority = ctx[:50], 4
            await _enqueue_tiktok_profile_targets(conn, usernames, source, priority, target_metadata)
        elif platform == "lemon8" and usernames:
            if ctx in {"author", "profile", "post"}:
                source, priority = ctx, 2
            elif ctx in {"follow", "following"}:
                source, priority = "following", 2
            elif ctx == "follower":
                source, priority = "follower", 3
            else:
                source, priority = ctx[:50], 4
            await _enqueue_lemon8_profile_targets(conn, usernames, source, priority, target_metadata)
    except Exception:
        logger.debug("post-user enqueue failed platform=%s context=%s", platform, context_value, exc_info=True)


async def _persist_shared_discovery(conn, platform, successful_rows, ctx, context_value, owner_account) -> None:
    """Persist shared-spider discovery targets for x/threads/facebook. Never raises."""
    if platform not in {"x", "threads", "facebook"} or not successful_rows:
        return
    try:
        from src.core.spider_discovery_store import persist_discovery_target
        from src.core.spider_graph_store import shared_spider_enabled, bounded_limit

        if shared_spider_enabled(platform):
            max_targets = bounded_limit(
                "SOCIAL_SHARED_DISCOVERY_MAX_PER_CAPTURE", 50, maximum=100
            )
            confidence_by_context = {
                "follow": 0.95,
                "following": 0.95,
                "follower": 0.95,
                "profile": 0.9,
                "author": 0.75,
                "comment": 0.65,
                "foryou": 0.6,
            }
            context_key = ctx
            discovery_type = (
                "browser_foryou_author"
                if context_key == "foryou"
                else f"browser_{context_key}_account"
            )[:64]
            post_tables = {
                "x": "x_posts",
                "threads": "threads_posts",
                "facebook": "facebook_posts",
            }
            for r in successful_rows[:max_targets]:
                target_id = r["user_id"] or r["username"]
                if not target_id:
                    continue
                source_record_id = r.get("source_record_id")
                source_table = post_tables[platform] if source_record_id else "social_users"
                if not source_record_id:
                    source_record_id = f"{platform}:{r['uid']}"
                await persist_discovery_target(
                    conn,
                    platform,
                    str(target_id),
                    target_type="account",
                    discovery_type=discovery_type,
                    source_table=source_table,
                    source_record_id=source_record_id,
                    confidence=confidence_by_context.get(context_key, 0.55),
                    priority=60,
                    source_metadata={
                        "capture_context": context_value,
                        "owner_account": owner_account,
                        "username": r["username"],
                    },
                    max_pending=bounded_limit(
                        f"{platform.upper()}_SHARED_SPIDER_MAX_PENDING",
                        300,
                        maximum=3000,
                    ),
                )
    except Exception:
        logger.debug(
            "browser shared discovery write skipped platform=%s context=%s",
            platform,
            context_value,
            exc_info=True,
        )


async def _record_users(pool, platform, users, context, owner=None) -> int:
    if not users:
        return 0
    context_value = str(context or "seen")[:64]
    cross_handles = []  # threads handles to push into the IG spider queue
    # PER-ACCOUNT follow graph: when the extension sends an owner (the logged-in
    # account) with a follow/follower context, also record a directional edge in
    # follow_edges so each of your accounts' graphs is distinct (multi-account).
    owner_account, direction = _owner_account_for_follow(platform, context, owner)
    owner_account = str(owner_account) if owner_account else None
    rows = _normalize_user_rows(platform, users)
    if not rows:
        return 0

    user_args = [
        (
            platform,
            r["uid"],
            r["user_id"],
            r["username"],
            r["display_name"],
            r["profile_photo_url"],
            [context_value],
        )
        for r in rows
    ]
    edge_args = [
        (platform, owner_account, r["uid"], direction, r["username"])
        for r in rows
        if owner_account and direction
    ]
    n = 0
    async with pool.acquire() as conn:
        n, successful_rows = await _upsert_social_users(conn, platform, rows, user_args)
        await _upsert_follow_edges(conn, platform, rows, edge_args, owner_account, direction)
        usernames = [r["username"] for r in successful_rows if r["username"]]
        ctx = re.sub(r"[\s_-]+", "", context_value.lower())
        target_metadata = {
            "source": "social_users",
            "context": context_value,
            "owner_account": owner_account,
        }
        await _enqueue_profile_targets(conn, platform, usernames, ctx, context_value, target_metadata)
        await _persist_shared_discovery(conn, platform, successful_rows, ctx, context_value, owner_account)
        if platform == "threads" and ctx != "foryou":
            cross_handles.extend(usernames)
        if cross_handles:
            await _cross_seed_instagram(conn, cross_handles, "threads:" + (context_value or "feed"))
    return n




_SOCIAL_USERS_UPSERT_SQL = """
INSERT INTO social_users (platform, uid, platform_user_id, username, display_name, profile_photo_url, contexts)
VALUES ($1,$2,$3,$4,$5,$6,$7)
ON CONFLICT (platform, uid) DO UPDATE SET
   last_seen = now(),
   times_seen = social_users.times_seen + 1,
   username = COALESCE(EXCLUDED.username, social_users.username),
   platform_user_id = COALESCE(EXCLUDED.platform_user_id, social_users.platform_user_id),
   display_name = COALESCE(EXCLUDED.display_name, social_users.display_name),
   profile_photo_url = COALESCE(EXCLUDED.profile_photo_url, social_users.profile_photo_url),
   contexts = (SELECT array_agg(DISTINCT c) FROM unnest(social_users.contexts || EXCLUDED.contexts) AS c)
"""


_FOLLOW_EDGES_UPSERT_SQL = """
INSERT INTO follow_edges
    (platform, owner_account, target_uid, direction, target_username, first_seen, last_seen)
VALUES ($1, $2, $3, $4, $5, now(), now())
ON CONFLICT (platform, owner_account, target_uid, direction) DO UPDATE SET
    last_seen = now(),
    target_username = COALESCE(EXCLUDED.target_username, follow_edges.target_username)
"""


# Public aliases — routes/generic.py imports these names.
record_users = _record_users
save_comments = _save_comments
save_posts = _save_posts
