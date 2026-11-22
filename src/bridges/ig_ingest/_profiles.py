"""Profile persistence, capability receipts, access tracking.

Split out of ``__init__.py`` (split step 11); re-exported there.
"""
import logging
from ._ids import _VERIFIED_FILTER_PLATFORMS
from ._ids import _explicitly_verified_capture
from ._util import _int
from src.core.profile_media import approved_profile_image_url
from urllib.parse import urlparse
import json
try:
    from src.core.profile_access import ProfileAccessRepository
except Exception:  # pragma: no cover
    ProfileAccessRepository = None  # type: ignore[assignment]
try:
    from src.core.user_change_tracker import UserChangeTracker, INSTAGRAM_TRACKED_FIELDS
except Exception:  # pragma: no cover
    UserChangeTracker = None  # type: ignore[assignment]
    INSTAGRAM_TRACKED_FIELDS = None  # type: ignore[assignment]

logger = logging.getLogger("social_ingest")


async def _save_snapchat_profile(pool, platform, p, uname) -> bool:
    from . import _record_profile_capability_receipts

    metadata = p.get("metadata") if isinstance(p.get("metadata"), dict) else {}
    bitmoji_url = str(p.get("bitmoji_url") or "").strip()
    page_url = str(metadata.get("page_url") or "")
    page = urlparse(page_url)
    expected_path = f"/add/{uname}"
    if (
        metadata.get("source") != "snapchat_public_profile_dom_canary"
        or page.scheme != "https"
        or (page.hostname or "").lower() not in {"snapchat.com", "www.snapchat.com"}
        or page.path.rstrip("/") != expected_path
        or not approved_profile_image_url(bitmoji_url, ("sc-cdn.net", "snapchat.com", "snapchatcdn.com"))
    ):
        return False
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO snapchat_profiles (username, bitmoji_url, raw_payload, probed_at)
            VALUES ($1,$2,$3::jsonb,now())
            ON CONFLICT (username) DO UPDATE SET
               bitmoji_url=EXCLUDED.bitmoji_url,
               raw_payload=EXCLUDED.raw_payload,
               probed_at=now()
            """,
            uname, bitmoji_url, json.dumps({"metadata": metadata}),
        )
    await _record_profile_capability_receipts(pool, platform)
    return True


async def _save_tiktok_profile(pool, platform, p, uname, pic) -> bool:
    from . import _record_profile_capability_receipts, _record_users

    async with pool.acquire() as conn:
        pid = str(p.get("platform_user_id") or p.get("user_id") or uname).strip().lstrip("@")
        await conn.execute(
            """
            INSERT INTO tiktok_profiles
              (id, platform_user_id, username, nickname, avatar_url, bio, followers_count, updated_at)
            VALUES (gen_random_uuid(),$1,$2,$3,$4,$5,$6,now())
            ON CONFLICT (platform_user_id) DO UPDATE SET
               username=COALESCE(NULLIF(EXCLUDED.username, ''), tiktok_profiles.username),
               nickname=COALESCE(EXCLUDED.nickname, tiktok_profiles.nickname),
               avatar_url=COALESCE(EXCLUDED.avatar_url, tiktok_profiles.avatar_url),
               bio=COALESCE(EXCLUDED.bio, tiktok_profiles.bio),
               followers_count=COALESCE(EXCLUDED.followers_count, tiktok_profiles.followers_count),
               updated_at=now()
            """,
            pid, uname, p.get("display_name") or p.get("full_name"), pic, p.get("bio"),
            _int(p.get("followers_count")),
        )
    await _record_users(pool, platform, [{"user_id": pid, "username": uname,
                                          "display_name": p.get("display_name") or p.get("full_name"),
                                          "profile_pic_url": pic}], "profile")
    await _record_profile_capability_receipts(pool, platform)
    return True


async def _save_x_profile(pool, platform, p, uname, pic) -> bool:
    from . import _record_profile_capability_receipts, _record_users

    async with pool.acquire() as conn:
        # Preserve the exact handle casing already present in x_posts when
        # possible; entity_platform_links for X were historically keyed on
        # x_posts.author_username.
        canonical = await conn.fetchval(
            """
            SELECT author_username
            FROM x_posts
            WHERE lower(author_username) = lower($1)
            ORDER BY collected_at DESC NULLS LAST
            LIMIT 1
            """,
            uname,
        )
        pid = str(p.get("platform_user_id") or p.get("user_id") or canonical or uname).strip().lstrip("@")
        username = str(canonical or uname).strip().lstrip("@")
        await conn.execute(
            """
            INSERT INTO x_profiles
              (id, platform_user_id, username, display_name, bio, followers_count,
               following_count, posts_count, is_verified, is_private, profile_pic_url,
               external_url, location, joined_text, collected_at, updated_at, metadata)
            VALUES (gen_random_uuid(),$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,now(),now(),$14::jsonb)
            ON CONFLICT (platform_user_id) DO UPDATE SET
               username=COALESCE(NULLIF(EXCLUDED.username, ''), x_profiles.username),
               display_name=COALESCE(EXCLUDED.display_name, x_profiles.display_name),
               bio=COALESCE(EXCLUDED.bio, x_profiles.bio),
               followers_count=COALESCE(EXCLUDED.followers_count, x_profiles.followers_count),
               following_count=COALESCE(EXCLUDED.following_count, x_profiles.following_count),
               posts_count=COALESCE(EXCLUDED.posts_count, x_profiles.posts_count),
               is_verified=COALESCE(EXCLUDED.is_verified, x_profiles.is_verified),
               is_private=COALESCE(EXCLUDED.is_private, x_profiles.is_private),
               profile_pic_url=COALESCE(EXCLUDED.profile_pic_url, x_profiles.profile_pic_url),
               external_url=COALESCE(EXCLUDED.external_url, x_profiles.external_url),
               location=COALESCE(EXCLUDED.location, x_profiles.location),
               joined_text=COALESCE(EXCLUDED.joined_text, x_profiles.joined_text),
               metadata=x_profiles.metadata || EXCLUDED.metadata,
               updated_at=now()
            """,
            pid, username, p.get("display_name") or p.get("full_name"), p.get("bio"),
            _int(p.get("followers_count")), _int(p.get("following_count")), _int(p.get("posts_count")),
            bool(p.get("is_verified")) if p.get("is_verified") is not None else None,
            bool(p.get("is_private")) if p.get("is_private") is not None else None,
            pic, p.get("external_url"), p.get("location"), p.get("joined_text"),
            json.dumps(p.get("metadata") or {}),
        )
    await _record_users(pool, platform, [{"user_id": pid, "username": username,
                                          "display_name": p.get("display_name") or p.get("full_name"),
                                          "profile_pic_url": pic}], "profile")
    await _record_profile_capability_receipts(pool, platform)
    return True


async def _save_facebook_profile(pool, platform, p, uname, pic) -> bool:
    from . import _record_profile_capability_receipts, _record_users

    async with pool.acquire() as conn:
        pid = str(p.get("platform_user_id") or p.get("user_id") or uname).strip().lstrip("@")
        await conn.execute(
            """
            INSERT INTO facebook_profiles
              (id, platform_user_id, username, display_name, bio, followers_count,
               following_count, friends_count, is_person, profile_pic_url, external_url,
               collected_at, updated_at, metadata)
            VALUES (gen_random_uuid(),$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,now(),now(),$11::jsonb)
            ON CONFLICT (platform_user_id) DO UPDATE SET
               username=COALESCE(NULLIF(EXCLUDED.username, ''), facebook_profiles.username),
               display_name=COALESCE(EXCLUDED.display_name, facebook_profiles.display_name),
               bio=COALESCE(EXCLUDED.bio, facebook_profiles.bio),
               followers_count=COALESCE(EXCLUDED.followers_count, facebook_profiles.followers_count),
               following_count=COALESCE(EXCLUDED.following_count, facebook_profiles.following_count),
               friends_count=COALESCE(EXCLUDED.friends_count, facebook_profiles.friends_count),
               is_person=COALESCE(EXCLUDED.is_person, facebook_profiles.is_person),
               profile_pic_url=COALESCE(EXCLUDED.profile_pic_url, facebook_profiles.profile_pic_url),
               external_url=COALESCE(EXCLUDED.external_url, facebook_profiles.external_url),
               metadata=facebook_profiles.metadata || EXCLUDED.metadata,
               updated_at=now()
            """,
            pid, uname, p.get("display_name") or p.get("full_name"), p.get("bio"),
            _int(p.get("followers_count")), _int(p.get("following_count")), _int(p.get("friends_count")),
            bool(p.get("is_person")) if p.get("is_person") is not None else None,
            pic, p.get("external_url"), json.dumps(p.get("metadata") or {}),
        )
    await _record_users(pool, platform, [{"user_id": pid, "username": uname,
                                          "display_name": p.get("display_name") or p.get("full_name"),
                                          "profile_pic_url": pic}], "profile")
    await _record_profile_capability_receipts(pool, platform)
    return True


async def _save_instagram_profile(pool, platform, p, uname, pic) -> bool:
    from . import _record_users

    # Tier 4 change-history: snapshot the row BEFORE the upsert so we can diff
    # old -> new and log bio/username/follower/etc. changes. Best-effort.
    prev_row = None
    if UserChangeTracker is not None:
        try:
            async with pool.acquire() as conn:
                prev_row = await conn.fetchrow(
                    "SELECT username, full_name, bio, followers_count, following_count, "
                    "posts_count, is_verified, is_private, profile_pic_url, external_url "
                    "FROM instagram_profiles WHERE platform_user_id = $1",
                    str(p.get("user_id") or uname),
                )
        except Exception:
            logger.debug("ig change-tracker prev-row fetch failed for %s", uname, exc_info=True)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO instagram_profiles
                  (id, platform_user_id, username, full_name, bio, followers_count,
                   following_count, posts_count, is_verified, is_private, profile_pic_url,
                   external_url, collected_at, updated_at)
                VALUES (gen_random_uuid(),$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,now(),now())
                ON CONFLICT (platform_user_id) DO UPDATE SET
                   username=EXCLUDED.username, full_name=EXCLUDED.full_name, bio=EXCLUDED.bio,
                   followers_count=EXCLUDED.followers_count, following_count=EXCLUDED.following_count,
                   posts_count=EXCLUDED.posts_count, is_verified=EXCLUDED.is_verified,
                   is_private=EXCLUDED.is_private, profile_pic_url=COALESCE(EXCLUDED.profile_pic_url, instagram_profiles.profile_pic_url),
                   external_url=EXCLUDED.external_url, updated_at=now()
                """,
                str(p.get("user_id") or uname), uname, p.get("full_name"), p.get("bio"),
                _int(p.get("followers_count")), _int(p.get("following_count")), _int(p.get("posts_count")),
                bool(p.get("is_verified")), bool(p.get("is_private")), pic, p.get("external_url"),
            )
    except Exception:
        logger.debug("save profile failed %s", uname, exc_info=True)
    await _track_ig_profile_change(pool, p, uname, prev_row)
    await _record_users(pool, platform, [{"user_id": p.get("user_id"), "username": uname,
                                          "display_name": p.get("full_name"), "profile_pic_url": pic}], "profile")
    return True


async def _save_profile(pool, platform, p) -> bool:
    """Upsert a full profile. Instagram is API-rich; X/Facebook are browser DOM
    best-effort profiles keyed on the visible handle."""
    if platform not in ("instagram", "x", "facebook", "tiktok", "snapchat") or (
        platform in _VERIFIED_FILTER_PLATFORMS and _explicitly_verified_capture(p)
    ):
        return False
    uname = (p.get("username") or "").strip().lstrip("@")
    if not uname:
        return False
    pic = p.get("profile_pic_url")
    if platform == "snapchat":
        return await _save_snapchat_profile(pool, platform, p, uname)
    if platform == "tiktok":
        return await _save_tiktok_profile(pool, platform, p, uname, pic)
    if platform == "x":
        return await _save_x_profile(pool, platform, p, uname, pic)
    if platform == "facebook":
        return await _save_facebook_profile(pool, platform, p, uname, pic)
    return await _save_instagram_profile(pool, platform, p, uname, pic)




async def _record_profile_capability_receipts(pool, platform: str) -> None:
    """Make browser profile fields usable only after their own row readback."""
    if platform not in {"x", "facebook", "tiktok", "snapchat"}:
        return
    try:
        from src.core.capability_receipts import record_profile_field_receipts
        async with pool.acquire() as conn:
            await record_profile_field_receipts(conn, platform)
    except Exception:
        logger.debug("profile capability receipt failed for %s", platform, exc_info=True)


async def _track_ig_profile_change(pool, p, uname, prev_row) -> None:
    """Diff the prior instagram_profiles row against the incoming extension
    payload and log field changes into instagram_user_changes (Tier 4). Maps the
    extension's field names onto INSTAGRAM_TRACKED_FIELDS. Best-effort — never
    breaks ingest."""
    if UserChangeTracker is None or INSTAGRAM_TRACKED_FIELDS is None:
        return
    try:
        pk_val = int(p.get("user_id") or 0)
    except (TypeError, ValueError):
        pk_val = 0
    if not pk_val:
        return
    try:
        current_normalized = None
        if prev_row is not None:
            pr = dict(prev_row)
            current_normalized = {
                "username": pr.get("username"), "full_name": pr.get("full_name"),
                "biography": pr.get("bio"), "is_verified": pr.get("is_verified"),
                "is_private": pr.get("is_private"), "profile_pic_url": pr.get("profile_pic_url"),
                "follower_count": pr.get("followers_count"),
                "following_count": pr.get("following_count"),
                "post_count": pr.get("posts_count"), "external_url": pr.get("external_url"),
            }
        new_snapshot = {
            "username": p.get("username"), "full_name": p.get("full_name"),
            "biography": p.get("bio"), "is_verified": bool(p.get("is_verified")),
            "is_private": bool(p.get("is_private")), "profile_pic_url": p.get("profile_pic_url"),
            "follower_count": _int(p.get("followers_count")),
            "following_count": _int(p.get("following_count")),
            "post_count": _int(p.get("posts_count")), "external_url": p.get("external_url"),
        }
        await UserChangeTracker(pool).detect_and_log(
            table="instagram_user_changes", pk_col="user_id", pk_val=pk_val,
            current_row=current_normalized, new_row=new_snapshot,
            fields=INSTAGRAM_TRACKED_FIELDS,
        )
    except Exception:
        logger.debug("ig change-tracker detect_and_log failed for %s", uname, exc_info=True)


async def _record_ig_access(pool, target_username, owner, profile) -> None:
    """Record that ``owner`` (the extension's logged-in IG account) could see
    ``target_username`` into profile_access_{summary,attempts} — the follow-aware
    selector (Phase 0). Because the extension is the live IG path (the headless
    collector is 429'd), this is where the routing data actually accumulates.

    A successful profile ingest means the owner CAN access the target. A private
    profile that still yielded data implies the owner FOLLOWS it (private data is
    only visible to followers). Best-effort — never breaks ingest.
    """
    if ProfileAccessRepository is None or not pool or not target_username:
        return
    account = None
    if isinstance(owner, dict):
        account = (owner.get("username") or owner.get("id") or "") or None
    elif isinstance(owner, str):
        account = owner.strip() or None
    # Fallback label when the extension didn't send which account it used: still
    # records the public/private signal, but won't match a routable cookie
    # account (so it can't mis-route — routing only trusts real account names).
    if not account:
        account = "ig_extension"
    _priv = profile.get("is_private")
    is_private = bool(_priv) if _priv is not None else None
    _fbv = profile.get("followed_by_viewer")
    is_followed = bool(_fbv) if _fbv is not None else bool(is_private)
    try:
        repo = ProfileAccessRepository(pool)
        await repo.record_attempt(
            source="instagram",
            target_id=str(target_username).lstrip("@"),
            account=str(account),
            can_access=True,
            is_public=(None if is_private is None else (not is_private)),
            is_followed=is_followed,
        )
    except Exception:
        logger.debug("ig access-record failed for %s", target_username, exc_info=True)


# Public alias — routes/generic.py imports this name.
save_profile = _save_profile
