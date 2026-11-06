"""Durable, field-level proof for capability-gated source collection."""
from __future__ import annotations

import json
from datetime import timedelta


_FIELD_READBACKS = {
    ("lemon8", "profile.avatar_url"): (
        "lemon8_profiles", "avatar_url", "updated_at", "collector_native_profile",
    ),
    ("lemon8", "profile.bio"): (
        "lemon8_profiles", "bio", "updated_at", "collector_native_profile",
    ),
    ("lemon8", "profile.followers_count"): (
        "lemon8_profiles", "followers_count", "updated_at", "collector_native_profile",
    ),
    ("x", "profile.followers_count"): (
        "x_profiles", "followers_count", "updated_at", "browser_x_profile",
    ),
    ("facebook", "profile.bio"): (
        "facebook_profiles", "bio", "updated_at", "browser_facebook_profile",
    ),
    ("facebook", "profile.avatar_url"): (
        "facebook_profiles", "profile_pic_url", "updated_at", "browser_facebook_profile",
    ),
    ("facebook", "profile.followers_count"): (
        "facebook_profiles", "followers_count", "updated_at", "browser_facebook_profile",
    ),
    ("tiktok", "profile.followers_count"): (
        "tiktok_profiles", "followers_count", "updated_at", "collector_profile",
    ),
    ("whatsapp", "profile.avatar_url"): (
        "whatsapp_users", "photo_url", "updated_at", "baileys_profile_picture_canary",
    ),
    ("tiktok", "profile.avatar_url"): (
        "tiktok_profiles", "avatar_url", "updated_at", "collector_profile",
    ),
    ("tiktok", "profile.bio"): (
        "tiktok_profiles", "bio", "updated_at", "collector_profile",
    ),
    ("threads", "post.author_username"): (
        "threads_posts", "author_username", "collected_at", "browser_threads_post",
    ),
}

_PREDICATE_READBACKS = {
    ("pinterest", "profile.avatar_url"): (
        "pinterest_profiles", "username", "probed_at",
        r"avatar_url IS NOT NULL AND btrim(avatar_url) <> '' AND avatar_url !~* '/images/user/default(_[0-9]+)?\.png([?].*)?$'",
        "collector_profile_avatar",
    ),
    ("pinterest", "pin.creator_provenance"): (
        "pinterest_pins", "pin_id", "collected_at",
        "username IS NOT NULL AND metadata->>'owner_verified'='true'",
        "collector_verified_owner_pin",
    ),
    ("threads", "profile.avatar_url"): (
        "social_users", "COALESCE(platform_user_id, uid)", "last_seen",
        "platform='threads' AND profile_photo_url IS NOT NULL AND btrim(profile_photo_url) <> ''",
        "threads_profile_observation",
    ),
    ("snapchat", "profile.bitmoji_url"): (
        "snapchat_profiles", "username", "probed_at",
        "bitmoji_url IS NOT NULL AND btrim(bitmoji_url) <> ''",
        "browser_snapchat_public_profile_dom",
    ),
}

_PROFILE_RECEIPTS = {
    "x": ("profile.followers_count",),
    "facebook": ("profile.avatar_url", "profile.bio", "profile.followers_count"),
    "tiktok": ("profile.avatar_url", "profile.bio", "profile.followers_count"),
    "snapchat": ("profile.bitmoji_url",),
}


async def record_field_readback_receipt(conn, source: str, capability: str, *, max_age: timedelta) -> dict:
    """Persist a receipt only when a recent row proves the exact supported field."""
    if (source, capability) in _FIELD_READBACKS:
        table, field, timestamp_field, path = _FIELD_READBACKS[(source, capability)]
        row_id = "username" if table == "pinterest_profiles" else "id::text"
        row = await conn.fetchrow(
            f"SELECT {row_id} AS id, {timestamp_field} AS updated_at FROM {table} "
            f"WHERE {field} IS NOT NULL AND {timestamp_field} >= now() - $1::interval "
            f"ORDER BY {timestamp_field} DESC LIMIT 1",
            max_age,
        )
    elif (source, capability) in _PREDICATE_READBACKS:
        table, id_field, timestamp_field, predicate, path = _PREDICATE_READBACKS[(source, capability)]
        field = capability.rsplit(".", 1)[-1]
        row = await conn.fetchrow(
            f"SELECT {id_field}::text AS id, {timestamp_field} AS updated_at FROM {table} "
            f"WHERE {predicate} AND {timestamp_field} >= now() - $1::interval "
            f"ORDER BY {timestamp_field} DESC LIMIT 1",
            max_age,
        )
    else:
        raise ValueError(f"no bounded readback canary for {source}/{capability}")
    state = "proven" if row else "failed"
    proof = {
        "path": path,
        "field": field,
        "fresh_within_seconds": int(max_age.total_seconds()),
        "durable_row_id": str(row["id"]) if row else None,
        "durable_updated_at": row["updated_at"].isoformat() if row else None,
    }
    receipt_id = await conn.fetchval("""
        INSERT INTO source_capability_receipts (source, capability, state, proof, rollback)
        VALUES($1, $2, $3, $4::jsonb, $5::jsonb) RETURNING id::text
    """, source, capability, state, json.dumps(proof), json.dumps({
        "action": "do_not_enable_without_a_new_proven_receipt",
    }))
    return {"id": str(receipt_id), "source": source, "capability": capability, "state": state, "proof": proof}


async def record_profile_field_receipts(conn, source: str, *, max_age: timedelta = timedelta(hours=24)) -> list[dict]:
    """Persist one fresh durable receipt per eligible profile field."""
    return [
        await record_field_readback_receipt(conn, source, capability, max_age=max_age)
        for capability in _PROFILE_RECEIPTS.get(source, ())
    ]


async def latest_capability_receipts(conn) -> list[dict]:
    """Return the latest state per field without leaking durable-row identity."""
    rows = await conn.fetch("""
        SELECT DISTINCT ON (source, capability)
               source, capability, state, proof->>'path' AS path, created_at
        FROM source_capability_receipts
        ORDER BY source, capability, created_at DESC
    """)
    return [
        {
            "source": str(row["source"]), "capability": str(row["capability"]),
            "state": str(row["state"]), "path": row["path"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
