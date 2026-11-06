"""Read-only receipts for the explicit public-social verified exclusion policy."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


_TRUE = "('true', '1', 'yes')"
_LABELS = "('business', 'creator', 'organization', 'official')"

# Each predicate only accepts an explicit provider field or label. Unknown data
# remains eligible, as required by the collection policy.
_PROFILE_SPECS = (
    ("instagram", "instagram_profiles", "is_verified IS TRUE"),
    ("tiktok", "tiktok_profiles", "is_verified IS TRUE"),
    ("x", "x_profiles", "is_verified IS TRUE"),
    ("pinterest", "pinterest_profiles", f"""
        COALESCE(LOWER(BTRIM(raw_payload->>'profile_is_verified')), '') IN {_TRUE}
        OR COALESCE(LOWER(BTRIM(raw_payload->>'is_verified')), '') IN {_TRUE}
        OR COALESCE(LOWER(BTRIM(raw_payload->>'verified')), '') IN {_TRUE}
        OR COALESCE(LOWER(BTRIM(raw_payload->>'account_type')), '') IN {_LABELS}
        OR COALESCE(LOWER(BTRIM(raw_payload->>'profile_type')), '') IN {_LABELS}
    """),
    ("snapchat", "snapchat_profiles", f"""
        COALESCE(LOWER(BTRIM(raw_payload->>'is_verified')), '') IN {_TRUE}
        OR COALESCE(LOWER(BTRIM(raw_payload->>'verified')), '') IN {_TRUE}
        OR COALESCE(LOWER(BTRIM(raw_payload->>'account_type')), '') IN {_LABELS}
        OR COALESCE(LOWER(BTRIM(raw_payload->>'profile_type')), '') IN {_LABELS}
    """),
    ("youtube", "youtube_channels", "last_skip_reason = 'verified_account'"),
)

_DEPENDENCY_QUERIES = {
    "instagram": (
        ("posts", "instagram_posts", """SELECT COUNT(*)::int FROM instagram_posts post
            JOIN instagram_profiles profile ON profile.id=post.profile_id WHERE ({predicate})"""),
        ("comments", "instagram_comments", """SELECT COUNT(*)::int FROM instagram_comments comment
            JOIN instagram_posts post ON post.id=comment.post_id
            JOIN instagram_profiles profile ON profile.id=post.profile_id WHERE ({predicate})"""),
        ("queue", "instagram_spider_queue", """SELECT COUNT(*)::int FROM instagram_spider_queue queue
            JOIN instagram_profiles profile ON queue.platform_user_id=profile.platform_user_id
                OR LOWER(queue.username)=LOWER(profile.username) WHERE ({predicate})"""),
    ),
    "tiktok": (
        ("posts", "tiktok_posts", """SELECT COUNT(*)::int FROM tiktok_posts post
            JOIN tiktok_profiles profile ON profile.id=post.profile_id WHERE ({predicate})"""),
        ("comments", "tiktok_comments", """SELECT COUNT(*)::int FROM tiktok_comments comment
            JOIN tiktok_posts post ON post.id=comment.post_id
            JOIN tiktok_profiles profile ON profile.id=post.profile_id WHERE ({predicate})"""),
        ("queue", "tiktok_spider_queue", """SELECT COUNT(*)::int FROM tiktok_spider_queue queue
            JOIN tiktok_profiles profile ON queue.platform_user_id=profile.platform_user_id
                OR LOWER(queue.username)=LOWER(profile.username) WHERE ({predicate})"""),
    ),
    "x": (
        ("posts", "x_posts", """SELECT COUNT(*)::int FROM x_posts post
            JOIN x_profiles profile ON LOWER(post.author_username)=LOWER(profile.username) WHERE ({predicate})"""),
        ("edges", "x_edges", """SELECT COUNT(*)::int FROM x_edges edge
            JOIN x_profiles profile ON LOWER(edge.source_username)=LOWER(profile.username)
                OR LOWER(edge.target_username)=LOWER(profile.username) WHERE ({predicate})"""),
        ("queue", "x_profile_targets", """SELECT COUNT(*)::int FROM x_profile_targets queue
            JOIN x_profiles profile ON LOWER(queue.username)=LOWER(profile.username) WHERE ({predicate})"""),
    ),
    "pinterest": (
        ("pins", "pinterest_pins", """SELECT COUNT(*)::int FROM pinterest_pins pin
            JOIN pinterest_profiles profile ON LOWER(pin.username)=LOWER(profile.username) WHERE ({predicate})"""),
        ("boards", "pinterest_boards", """SELECT COUNT(*)::int FROM pinterest_boards board
            JOIN pinterest_profiles profile ON LOWER(board.username)=LOWER(profile.username) WHERE ({predicate})"""),
    ),
    "snapchat": (
        ("stories", "snapchat_stories", """SELECT COUNT(*)::int FROM snapchat_stories story
            JOIN snapchat_profiles profile ON LOWER(story.username)=LOWER(profile.username) WHERE ({predicate})"""),
        ("spotlights", "snapchat_spotlights", """SELECT COUNT(*)::int FROM snapchat_spotlights spotlight
            JOIN snapchat_profiles profile ON LOWER(spotlight.username)=LOWER(profile.username) WHERE ({predicate})"""),
        ("highlights", "snapchat_highlights", """SELECT COUNT(*)::int FROM snapchat_highlights highlight
            JOIN snapchat_profiles profile ON LOWER(highlight.username)=LOWER(profile.username) WHERE ({predicate})"""),
    ),
}

_IDENTITY_COLUMNS = {
    "instagram": "id::text AS id, platform_user_id, username",
    "tiktok": "id::text AS id, platform_user_id, username",
    "x": "id::text AS id, platform_user_id, username",
    "pinterest": "username",
    "snapchat": "username",
    "youtube": "id::text AS id, platform_channel_id, title",
}

_PURGEABLE_SOURCES = frozenset({"instagram", "tiktok", "x"})


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _candidate_values(source: str, candidates: list[Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    """Return frozen platform IDs and normalized handles from a manifest."""
    if source not in _PURGEABLE_SOURCES:
        raise ValueError(f"verified-exclusion purge is not implemented for {source}")
    platform_ids = sorted({str(row["platform_user_id"]).strip() for row in candidates
                           if row.get("platform_user_id") not in (None, "")})
    usernames = sorted({str(row["username"]).strip().lower() for row in candidates
                        if row.get("username") not in (None, "")})
    if not platform_ids and not usernames:
        raise ValueError(f"verified-exclusion manifest has no usable identities for {source}")
    return platform_ids, usernames


async def _delete_rows(
    conn, receipt_id: str, source: str, entity: str, query: str, *args: Any,
) -> int:
    """Archive removed rows inside the same transaction without loading them into memory."""
    position = len(args) + 1
    status = await conn.execute(f"""
        WITH removed AS ({query})
        INSERT INTO verified_exclusion_purge_rows (receipt_id, source, entity, payload)
        SELECT ${position}::uuid, ${position + 1}, ${position + 2}, row FROM removed
    """, *args, receipt_id, source, entity)
    return int(status.rsplit(" ", 1)[-1])


async def _purge_source(
    conn, receipt_id: str, source: str, platform_ids: list[str], usernames: list[str],
) -> dict[str, int]:
    """Delete one source using only identities frozen into the approved manifest."""
    values = (source, platform_ids, usernames)
    deleted: dict[str, int] = {}
    deleted["media"] = await _delete_rows(conn, receipt_id, source, "media", """
        DELETE FROM media_items AS media
        WHERE media.source=$1
          AND (media.entity_id=ANY($2::text[]) OR LOWER(media.entity_name)=ANY($3::text[]))
        RETURNING to_jsonb(media) AS row
    """, *values)
    deleted["targets"] = await _delete_rows(conn, receipt_id, source, "targets", """
        DELETE FROM collection_targets AS target
        WHERE target.source=$1
          AND (target.target_id=ANY($2::text[]) OR LOWER(COALESCE(target.target_name, ''))=ANY($3::text[]))
        RETURNING to_jsonb(target) AS row
    """, *values)
    deleted["follow_edges"] = await _delete_rows(conn, receipt_id, source, "follow_edges", """
        DELETE FROM follow_edges AS edge
        WHERE edge.platform=$1
          AND (edge.target_uid=ANY($2::text[]) OR LOWER(COALESCE(edge.target_username, ''))=ANY($3::text[]))
        RETURNING to_jsonb(edge) AS row
    """, *values)
    deleted["dead_letters"] = await _delete_rows(conn, receipt_id, source, "dead_letters", """
        DELETE FROM dead_letter_queue AS letter
        WHERE letter.source=$1
          AND (letter.entity_id=ANY($2::text[]) OR LOWER(COALESCE(letter.entity_id, ''))=ANY($3::text[]))
        RETURNING to_jsonb(letter) AS row
    """, *values)

    if source in {"instagram", "tiktok"}:
        prefix = source
        deleted["queue"] = await _delete_rows(conn, receipt_id, source, "queue", f"""
            DELETE FROM {prefix}_spider_queue AS queue
            WHERE queue.platform_user_id=ANY($1::text[])
               OR LOWER(COALESCE(queue.username, ''))=ANY($2::text[])
            RETURNING to_jsonb(queue) AS row
        """, platform_ids, usernames)
        deleted["comments"] = await _delete_rows(conn, receipt_id, source, "comments", f"""
            DELETE FROM {prefix}_comments AS comment
            USING {prefix}_posts AS post, {prefix}_profiles AS profile
            WHERE comment.post_id=post.id AND post.profile_id=profile.id
              AND (profile.platform_user_id=ANY($1::text[])
                   OR LOWER(COALESCE(profile.username, ''))=ANY($2::text[]))
            RETURNING to_jsonb(comment) AS row
        """, platform_ids, usernames)
        deleted["posts"] = await _delete_rows(conn, receipt_id, source, "posts", f"""
            DELETE FROM {prefix}_posts AS post
            USING {prefix}_profiles AS profile
            WHERE post.profile_id=profile.id
              AND (profile.platform_user_id=ANY($1::text[])
                   OR LOWER(COALESCE(profile.username, ''))=ANY($2::text[]))
            RETURNING to_jsonb(post) AS row
        """, platform_ids, usernames)
        deleted["profiles"] = await _delete_rows(conn, receipt_id, source, "profiles", f"""
            DELETE FROM {prefix}_profiles AS profile
            WHERE profile.platform_user_id=ANY($1::text[])
            RETURNING to_jsonb(profile) AS row
        """, platform_ids)
        return deleted

    deleted["queue"] = await _delete_rows(conn, receipt_id, source, "queue", """
        DELETE FROM x_profile_targets AS target
        WHERE LOWER(target.username)=ANY($1::text[])
        RETURNING to_jsonb(target) AS row
    """, usernames)
    deleted["edges"] = await _delete_rows(conn, receipt_id, source, "edges", """
        DELETE FROM x_edges AS edge
        WHERE LOWER(COALESCE(edge.source_username, ''))=ANY($1::text[])
           OR LOWER(edge.target_username)=ANY($1::text[])
        RETURNING to_jsonb(edge) AS row
    """, usernames)
    deleted["posts"] = await _delete_rows(conn, receipt_id, source, "posts", """
        DELETE FROM x_posts AS post
        WHERE LOWER(post.author_username)=ANY($1::text[])
        RETURNING to_jsonb(post) AS row
    """, usernames)
    deleted["profiles"] = await _delete_rows(conn, receipt_id, source, "profiles", """
        DELETE FROM x_profiles AS profile
        WHERE profile.platform_user_id=ANY($1::text[])
        RETURNING to_jsonb(profile) AS row
    """, platform_ids)
    return deleted


async def _record_media_cleanup(conn, receipt_id: str) -> int:
    """Queue only archive paths not referenced by a surviving media row."""
    count = int(await conn.fetchval("""
        SELECT COUNT(DISTINCT payload->>'file_path')::int
        FROM verified_exclusion_purge_rows
        WHERE receipt_id=$1::uuid AND entity='media' AND payload ? 'file_path'
    """, receipt_id) or 0)
    if not count:
        return 0
    await conn.execute("""
        WITH candidates AS (
            SELECT DISTINCT payload->>'file_path' AS file_path,
                NULLIF(payload->>'sha256', '') AS sha256
            FROM verified_exclusion_purge_rows
            WHERE receipt_id=$1::uuid AND entity='media' AND payload ? 'file_path'
        )
        INSERT INTO verified_exclusion_media_cleanup (receipt_id, file_path, sha256, status, completed_at)
        SELECT $1::uuid, candidate.file_path, candidate.sha256, 'pending', NULL
        FROM candidates AS candidate
        ON CONFLICT (receipt_id, file_path) DO NOTHING
    """, receipt_id)
    return count


async def purge_verified_exclusion_manifest(conn, manifest_id: str) -> dict[str, Any]:
    """Purge one frozen manifest and retain all removed rows as a restore receipt."""
    async with conn.transaction():
        manifest_row = await conn.fetchrow(
            "SELECT payload FROM verified_exclusion_manifests WHERE id=$1::uuid FOR UPDATE", manifest_id,
        )
        if manifest_row is None:
            raise ValueError("verified-exclusion manifest does not exist")
        manifest = _json_value(manifest_row["payload"])
        if manifest.get("destructive") is not False:
            raise ValueError("verified-exclusion manifest is not a read-only before-image")
        already_committed = await conn.fetchval("""
            SELECT 1 FROM verified_exclusion_purge_receipts
            WHERE manifest_id=$1::uuid AND status='committed' LIMIT 1
        """, manifest_id)
        if already_committed:
            raise ValueError("verified-exclusion manifest was already purged")

        source_entries = manifest.get("sources")
        if not isinstance(source_entries, list):
            raise ValueError("verified-exclusion manifest has no source entries")
        active_entries = [entry for entry in source_entries if entry.get("candidate_identities")]
        unsupported = {entry.get("source") for entry in active_entries} - _PURGEABLE_SOURCES
        if unsupported:
            raise ValueError(f"verified-exclusion purge needs a source adapter: {sorted(unsupported)}")

        receipt_payload: dict[str, Any] = {
            "manifest_id": str(manifest_id),
            "policy": manifest.get("policy"),
            "sources": [],
        }
        receipt_id = str(await conn.fetchval("""
            INSERT INTO verified_exclusion_purge_receipts (manifest_id, status, payload)
            VALUES ($1::uuid, 'prepared', $2::jsonb) RETURNING id::text
        """, manifest_id, json.dumps(receipt_payload)))
        for entry in active_entries:
            source = entry["source"]
            identities = entry["candidate_identities"]
            platform_ids, usernames = _candidate_values(source, identities)
            deleted = await _purge_source(conn, receipt_id, source, platform_ids, usernames)
            receipt_payload["sources"].append({
                "source": source,
                "candidate_identities": identities,
                "restore_rows_table": "verified_exclusion_purge_rows",
                "deleted_counts": deleted,
            })
        receipt_payload["media_cleanup_candidates"] = await _record_media_cleanup(conn, receipt_id)
        await conn.execute("""
            UPDATE verified_exclusion_purge_receipts
            SET status='committed', payload=$2::jsonb, completed_at=now() WHERE id=$1::uuid
        """, receipt_id, json.dumps(receipt_payload, default=str))
    return {"receipt_id": receipt_id, "sources": [
        {"source": item["source"], "deleted_counts": item["deleted_counts"]}
        for item in receipt_payload["sources"]
    ]}


async def cleanup_verified_exclusion_media(conn, receipt_id: str, *, root: Path | None = None) -> dict[str, int]:
    """Remove only receipt files still unreferenced at delete time."""
    from src.core.media_reconciliation import canonical_media_path
    from src.core.vault import VAULT_ROOT

    vault_root = root or VAULT_ROOT
    rows = await conn.fetch("""
        SELECT id::text, file_path, sha256 FROM verified_exclusion_media_cleanup
        WHERE receipt_id=$1::uuid AND status='pending' ORDER BY created_at, id
    """, receipt_id)
    result = {"deleted": 0, "shared": 0, "missing": 0, "failed": 0}
    for row in rows:
        shared = await conn.fetchval("""
            SELECT EXISTS(SELECT 1 FROM media_items WHERE file_path=$2)
                OR EXISTS(
                    SELECT 1 FROM media_items
                    WHERE COALESCE($1, '') <> '' AND sha256=$1
                )
        """, row["sha256"], row["file_path"])
        status = "shared" if shared else "deleted"
        error = None
        if shared:
            result[status] += 1
        else:
            try:
                path = canonical_media_path(row["file_path"], vault_root)
                if path.exists():
                    path.unlink()
                    result[status] += 1
                else:
                    status = "missing"
                    result[status] += 1
            except OSError as exc:
                status = "failed"
                error = str(exc)
                result[status] += 1
            except ValueError as exc:
                status = "failed"
                error = str(exc)
                result[status] += 1
        await conn.execute("""
            UPDATE verified_exclusion_media_cleanup
            SET status=$2, error=$3, completed_at=now() WHERE id=$1::uuid
        """, row["id"], status, error)
    return result


async def _table_exists(conn, table: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1)", f"public.{table}"))


async def _dependency_counts(conn, source: str, table: str, predicate: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, dependency_table, query in _DEPENDENCY_QUERIES.get(source, ()):
        if await _table_exists(conn, dependency_table):
            counts[name] = int(await conn.fetchval(query.format(predicate=predicate)) or 0)
    if not await _table_exists(conn, "media_items"):
        return counts
    if source in {"instagram", "tiktok"}:
        identities = f"SELECT platform_user_id::text AS value FROM {table} WHERE ({predicate}) UNION SELECT LOWER(username) FROM {table} WHERE ({predicate})"
    elif source == "youtube":
        identities = f"SELECT platform_channel_id::text AS value FROM {table} WHERE ({predicate}) UNION SELECT LOWER(title) FROM {table} WHERE ({predicate})"
    else:
        identities = f"SELECT LOWER(username) AS value FROM {table} WHERE ({predicate})"
    media_query = f"""
        WITH identities AS ({identities})
        SELECT COUNT(*)::int FROM media_items media
        WHERE media.source=$1 AND (media.entity_id IN (SELECT value FROM identities)
             OR LOWER(media.entity_name) IN (SELECT value FROM identities))
    """
    counts["media_rows"] = int(await conn.fetchval(media_query, source) or 0)
    shared_query = f"""
        WITH identities AS ({identities}), candidate_media AS (
            SELECT DISTINCT media.sha256 FROM media_items media
            WHERE media.source=$1 AND COALESCE(media.sha256, '') <> ''
              AND (media.entity_id IN (SELECT value FROM identities)
                   OR LOWER(media.entity_name) IN (SELECT value FROM identities))
        )
        SELECT COUNT(DISTINCT other.sha256)::int FROM media_items other
        JOIN candidate_media candidate ON candidate.sha256=other.sha256
        WHERE other.source <> $1
    """
    counts["shared_media_blobs"] = int(await conn.fetchval(shared_query, source) or 0)
    return counts


async def _candidate_identities(conn, source: str, table: str, predicate: str) -> list[dict]:
    columns = _IDENTITY_COLUMNS[source]
    rows = await conn.fetch(f"SELECT {columns} FROM {table} WHERE ({predicate}) ORDER BY 1")
    return [dict(row) for row in rows]


async def build_verified_exclusion_manifest(conn) -> dict:
    """Return candidate counts without touching profiles, posts, edges, or media."""
    sources: list[dict] = []
    for source, table, predicate in _PROFILE_SPECS:
        if not await _table_exists(conn, table):
            continue
        count = int(await conn.fetchval(f"SELECT COUNT(*)::int FROM {table} WHERE {predicate}") or 0)
        sources.append({
            "source": source,
            "profile_table": table,
            "profile_candidates": count,
            "candidate_identities": await _candidate_identities(conn, source, table, predicate),
            "dependencies": await _dependency_counts(conn, source, table, predicate),
        })
    return {
        "policy": "explicit_verified_or_provider_label_only",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources,
        "profile_candidates": sum(item["profile_candidates"] for item in sources),
        "destructive": False,
    }


async def persist_verified_exclusion_manifest(conn) -> dict:
    manifest = await build_verified_exclusion_manifest(conn)
    manifest_id = await conn.fetchval(
        "INSERT INTO verified_exclusion_manifests(payload) VALUES($1::jsonb) RETURNING id::text",
        json.dumps(manifest),
    )
    manifest["id"] = str(manifest_id)
    return manifest
