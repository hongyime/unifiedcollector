"""Handle parsing and profile-target enqueueing.

Split out of ``__init__.py`` (split step 11); re-exported there.
"""
import logging
import json
import re

logger = logging.getLogger("social_ingest")


def _derive_handle_from_caption(caption):
    """Recover a threads/facebook author handle when the extension left
    author_username empty but dumped the whole post block into the caption
    ("<handle>\\n<relative time>\\n<caption>"). Returns the first line only when
    it's a clean handle (no spaces) — this rejects repost headers like
    "x reposted 1h ago". Mirrors tmp/backfill_threads_author.py so new posts
    self-heal instead of landing with NULL author. Returns None if unsure."""
    if not caption:
        return None
    first = caption.split("\n", 1)[0].strip()
    return first if _HANDLE_RE.match(first) else None


def _ig_author_uid(ppid: str) -> str | None:
    """Recover the Instagram author's numeric user id from a platform_post_id of
    the shape `<media_id>_<author_uid>` (IDENTITY_KEYS.md). Returns None when the
    id has no embedded uid (a bare media id / shortcode). This trailing id equals
    instagram_profiles.platform_user_id."""
    if not ppid:
        return None
    uid = ppid.rsplit("_", 1)[-1] if "_" in ppid else ""
    return uid if uid.isdigit() else None


def _x_handle(value) -> str | None:
    handle = str(value or "").strip().lstrip("@").lower()
    return handle if _X_HANDLE_RE.match(handle) else None


def _tiktok_handle(value) -> str | None:
    handle = str(value or "").strip().lstrip("@")
    if not handle or "/" in handle or "?" in handle or "#" in handle:
        return None
    lowered = handle.lower()
    if lowered in {
        "foryou", "following", "explore", "search", "live", "messages",
        "login", "signup", "upload", "discover", "tag", "music", "video",
    }:
        return None
    return handle if _TIKTOK_HANDLE_RE.match(handle) else None


def _lemon8_handle(value) -> str | None:
    handle = str(value or "").strip().lstrip("@")
    if not handle or "/" in handle or "?" in handle or "#" in handle:
        return None
    lowered = handle.lower()
    if lowered in {"feed", "foryou", "fashion", "beauty", "food", "travel", "home", "topic", "search"}:
        return None
    return handle if _LEMON8_HANDLE_RE.match(handle) else None


def _owner_account_for_follow(platform, context, owner):
    from . import TIKTOK_FOLLOW_OWNER_FALLBACK
    ctx_l = (context or "").lower()
    if ctx_l not in ("follow", "follower"):
        return None, None
    owner_account = None
    if isinstance(owner, dict):
        owner_account = (owner.get("username") or owner.get("id") or "") or None
    elif isinstance(owner, str):
        owner_account = owner.strip().lstrip("@") or None
    if owner_account:
        owner_account = str(owner_account).strip().lstrip("@") or None
    if not owner_account and platform == "tiktok":
        owner_account = TIKTOK_FOLLOW_OWNER_FALLBACK
    direction = "follower" if ctx_l == "follower" else "following"
    return owner_account, direction


async def _executemany_with_fallback(conn, query: str, rows: list[tuple], label: str) -> int:
    if not rows:
        return 0
    try:
        await conn.executemany(query, rows)
        return len(rows)
    except Exception:
        logger.debug("%s batch failed count=%d; retrying rows", label, len(rows), exc_info=True)
    written = 0
    for args in rows:
        try:
            await conn.execute(query, *args)
            written += 1
        except Exception:
            logger.debug("%s row failed key=%s", label, args[0], exc_info=True)
    return written


async def _enqueue_tiktok_profile_targets(
    conn,
    handles,
    source: str,
    priority: int,
    metadata: dict | None = None,
) -> int:
    if not handles:
        return 0
    rows = [
        (handle, source[:50], int(priority))
        for raw in handles
        if (handle := _tiktok_handle(raw))
    ]
    query = """
    INSERT INTO tiktok_spider_queue
        (platform_user_id, username, source, priority, status, collected_at)
    VALUES ($1, $1, $2, $3, 'pending', now())
    ON CONFLICT (platform_user_id) DO UPDATE SET
        username = COALESCE(tiktok_spider_queue.username, EXCLUDED.username),
        source = CASE
            WHEN tiktok_spider_queue.source = 'manual'
                THEN tiktok_spider_queue.source
            ELSE EXCLUDED.source
        END,
        priority = LEAST(
            COALESCE(tiktok_spider_queue.priority, EXCLUDED.priority),
            EXCLUDED.priority
        ),
        status = CASE
            WHEN tiktok_spider_queue.status IN ('completed', 'failed')
                THEN 'pending'
            ELSE tiktok_spider_queue.status
        END,
        collected_at = now()
    """
    return await _executemany_with_fallback(conn, query, rows, "enqueue tiktok profile targets")


async def _enqueue_lemon8_profile_targets(
    conn,
    handles,
    source: str,
    priority: int,
    metadata: dict | None = None,
) -> int:
    if not handles:
        return 0
    rows = [
        (handle, source[:50], int(priority))
        for raw in handles
        if (handle := _lemon8_handle(raw))
    ]
    query = """
    INSERT INTO lemon8_spider_queue
        (platform_user_id, source, priority, status, collected_at)
    VALUES ($1, $2, $3, 'pending', now())
    ON CONFLICT (platform_user_id) DO UPDATE SET
        source = CASE
            WHEN lemon8_spider_queue.source = 'manual'
                THEN lemon8_spider_queue.source
            ELSE EXCLUDED.source
        END,
        priority = LEAST(
            COALESCE(lemon8_spider_queue.priority, EXCLUDED.priority),
            EXCLUDED.priority
        ),
        status = CASE
            WHEN lemon8_spider_queue.status IN ('completed', 'failed')
                THEN 'pending'
            ELSE lemon8_spider_queue.status
        END,
        collected_at = now()
    """
    return await _executemany_with_fallback(conn, query, rows, "enqueue lemon8 profile targets")


def _x_post_url(author: str | None, post_id: str | None, metadata: dict | None = None) -> str | None:
    metadata = metadata if isinstance(metadata, dict) else {}
    for key in ("verify_url", "url", "post_url"):
        url = metadata.get(key)
        if isinstance(url, str) and url.startswith(("https://x.com/", "https://twitter.com/")):
            return url
    author_h = _x_handle(author)
    if author_h and post_id:
        return f"https://x.com/{author_h}/status/{post_id}"
    return None


async def _enqueue_x_profile_targets(conn, handles, source: str, priority: int, metadata: dict | None = None) -> int:
    if not handles:
        return 0
    meta_json = json.dumps(metadata or {})
    rows = [
        (handle, source[:64], int(priority), meta_json)
        for raw in handles
        if (handle := _x_handle(raw))
    ]
    query = """
    INSERT INTO x_profile_targets (username, source, priority, status, metadata)
    VALUES ($1, $2, $3, 'pending', $4::jsonb)
    ON CONFLICT (username) DO UPDATE SET
        priority = GREATEST(x_profile_targets.priority, EXCLUDED.priority),
        source = CASE
            WHEN x_profile_targets.source = 'manual' THEN x_profile_targets.source
            ELSE EXCLUDED.source
        END,
        status = CASE
            WHEN x_profile_targets.status IN ('unavailable', 'failed') THEN 'pending'
            ELSE x_profile_targets.status
        END,
        next_visit_at = LEAST(x_profile_targets.next_visit_at, now()),
        metadata = x_profile_targets.metadata || EXCLUDED.metadata,
        updated_at = now()
    """
    return await _executemany_with_fallback(conn, query, rows, "enqueue x profile targets")


async def _record_x_edge(
    conn,
    *,
    source_username: str | None,
    target_username: str,
    post_id: str | None,
    edge_type: str,
    strength: int,
    evidence_url: str | None,
    metadata: dict | None = None,
) -> None:
    target = _x_handle(target_username)
    if not target:
        return
    source = _x_handle(source_username) if source_username else None
    try:
        await conn.execute(
            """
            INSERT INTO x_edges
                (source_username, target_username, post_id, edge_type, strength, evidence_url, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            ON CONFLICT DO NOTHING
            """,
            source,
            target,
            str(post_id) if post_id else None,
            str(edge_type or "seen")[:64],
            int(strength),
            evidence_url,
            json.dumps(metadata or {}),
        )
    except Exception:
        logger.debug("record x edge failed %s -> %s", source, target, exc_info=True)


async def _record_x_post_graph(conn, post_id: str, post: dict) -> None:
    author = _x_handle(post.get("author_username"))
    mentions = [_x_handle(m) for m in (post.get("mentions") or [])]
    mentions = [m for m in mentions if m and m != author]
    metadata = post.get("metadata") if isinstance(post.get("metadata"), dict) else {}
    evidence_url = _x_post_url(author, post_id, metadata)
    enqueue = []
    if author:
        enqueue.append(author)
        await _record_x_edge(
            conn,
            source_username=None,
            target_username=author,
            post_id=post_id,
            edge_type="seen_author",
            strength=20,
            evidence_url=evidence_url,
            metadata={"source": "x_post_ingest"},
        )
    enqueue.extend(mentions)
    if enqueue:
        await _enqueue_x_profile_targets(
            conn,
            enqueue,
            "x_post",
            75,
            {"source": "x_posts_ingest", "post_id": post_id},
        )
    for mention in mentions:
        await _record_x_edge(
            conn,
            source_username=author,
            target_username=mention,
            post_id=post_id,
            edge_type="mention",
            strength=70,
            evidence_url=evidence_url,
            metadata={"source": "x_post_mentions"},
        )
    relation_specs = (
        ("reply", post.get("in_reply_to_screen_name") or metadata.get("in_reply_to_screen_name"), 80),
        ("quote", post.get("quoted_author_username") or metadata.get("quoted_author_username"), 75),
        ("repost", post.get("retweeted_author_username") or metadata.get("retweeted_author_username"), 60),
    )
    for edge_type, target, strength in relation_specs:
        target_h = _x_handle(target)
        if target_h:
            await _enqueue_x_profile_targets(
                conn,
                [target_h],
                f"x_{edge_type}",
                max(70, strength),
                {"source": "x_posts_relation", "post_id": post_id, "edge_type": edge_type},
            )
            await _record_x_edge(
                conn,
                source_username=author,
                target_username=target_h,
                post_id=post_id,
                edge_type=edge_type,
                strength=strength,
                evidence_url=evidence_url,
                metadata={"source": "x_post_relation"},
            )


async def _ensure_ig_profile(conn, ppid: str, p: dict) -> str | None:
    """Resolve (or create a minimal stub for) the instagram_profiles row for a
    post's author, keyed on the numeric uid embedded in platform_post_id, and
    return its id (uuid). This is the root-cause fix for the recurring NULL-FK
    bug: this ingest path historically OMITTED profile_id, so ~19k posts landed
    with NULL profile_id and were invisible to every consumer that joins
    instagram_posts.profile_id -> instagram_profiles.id (analyzer timelines,
    /geo). We now attach the FK at insert time.

    A stub is created (ON CONFLICT DO NOTHING) when the author profile hasn't
    been collected yet, so spidered-from-post authors still get a stable FK; the
    real profile collector later enriches the same row (unique platform_user_id).
    Returns None only when the uid can't be recovered (bare media id) — those
    posts remain genuinely unattributable, same as before."""
    uid = _ig_author_uid(ppid)
    if not uid:
        return None
    row = await conn.fetchrow(
        "SELECT id FROM instagram_profiles WHERE platform_user_id = $1", uid
    )
    if row:
        return str(row["id"])
    # No profile row yet — create a minimal stub keyed on the uid. Use any author
    # username the extension happened to send; the profile collector fills the
    # rest later (matched on the unique platform_user_id).
    username = (p.get("author_username") or p.get("username") or None)
    new_id = await conn.fetchval(
        """
        INSERT INTO instagram_profiles (platform_user_id, username)
        VALUES ($1, $2)
        ON CONFLICT (platform_user_id) DO UPDATE SET
            username = COALESCE(instagram_profiles.username, EXCLUDED.username)
        RETURNING id
        """,
        uid, username,
    )
    return str(new_id) if new_id else None


_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{2,30}$")


_X_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,20}$")


_LEMON8_HANDLE_RE = re.compile(r"^[A-Za-z0-9_.-]{2,64}$")


_TIKTOK_HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]{1,30}$")
