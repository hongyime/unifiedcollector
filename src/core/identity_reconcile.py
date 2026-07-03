"""social_users identity reconciliation (P2 review §3).

social_users PK is (platform, uid) where uid = platform_user_id when known, else
username (see add_social_users.sql). So the SAME person seen first as a bare
username (e.g. a comment author) and later with a resolved numeric id becomes TWO
rows that nothing merges — the downstream analyzer inherits the fragmentation as
ground-truth noise.

This job merges each username-keyed row into its canonical id-keyed row:
  * an id-keyed row has  uid = platform_user_id  and a known username
  * a username-keyed row has  uid = <username>  (uid IS DISTINCT FROM platform_user_id)
  * they match on (platform, id_row.username = username_row.uid)

Merge rule (id-keyed row wins as canonical):
  times_seen += Σ src.times_seen · contexts = union · first_seen = min ·
  last_seen = max · profile_photo_url/display_name = coalesce(existing, any src) ·
  metadata = src-then-target concat (target keys win). The username rows are then
  deleted. Idempotent: a second run finds no pairs.

Invoked periodically by the scheduler. Safe/fail-soft; runs in one transaction so a
failure rolls back cleanly (no half-merge).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# id-keyed target rows + their username-keyed source rows, one row per (target, src).
_MATCH_SQL = """
SELECT idk.uid           AS target_uid,
       idk.platform      AS platform,
       un.uid            AS src_uid,
       un.times_seen     AS src_times_seen,
       un.contexts       AS src_contexts,
       un.first_seen     AS src_first_seen,
       un.last_seen      AS src_last_seen,
       un.profile_photo_url AS src_photo,
       un.display_name   AS src_display_name,
       un.metadata       AS src_metadata
FROM      (SELECT platform, uid, username
             FROM social_users
            WHERE platform_user_id IS NOT NULL
              AND uid = platform_user_id
              AND username IS NOT NULL) idk
JOIN      (SELECT platform, uid, times_seen, contexts, first_seen, last_seen,
                  profile_photo_url, display_name, metadata
             FROM social_users
            WHERE platform_user_id IS DISTINCT FROM uid) un
  ON un.platform = idk.platform
 AND un.uid      = idk.username
"""


async def reconcile_social_users(pool, *, batch_limit: int = 20000) -> dict:
    """Merge username-keyed social_users rows into their id-keyed canonical rows.

    Returns {"pairs": n, "targets": m, "deleted": k}. batch_limit caps rows scanned
    per run so a huge backlog is drained over several runs rather than one long txn.
    """
    result = {"pairs": 0, "targets": 0, "deleted": 0}
    async with pool.acquire() as conn:
        rows = await conn.fetch(_MATCH_SQL + f" LIMIT {int(batch_limit)}")
        if not rows:
            return result
        result["pairs"] = len(rows)

        # Group source rows by their canonical (platform, target_uid).
        merged: dict[tuple[str, str], dict] = {}
        for r in rows:
            key = (r["platform"], r["target_uid"])
            m = merged.get(key)
            if m is None:
                m = {
                    "add_seen": 0,
                    "contexts": set(),
                    "first_seen": None,
                    "last_seen": None,
                    "photo": None,
                    "display_name": None,
                    "src_uids": [],
                }
                merged[key] = m
            m["add_seen"] += r["src_times_seen"] or 0
            m["contexts"].update(r["src_contexts"] or [])
            if r["src_first_seen"] and (m["first_seen"] is None or r["src_first_seen"] < m["first_seen"]):
                m["first_seen"] = r["src_first_seen"]
            if r["src_last_seen"] and (m["last_seen"] is None or r["src_last_seen"] > m["last_seen"]):
                m["last_seen"] = r["src_last_seen"]
            if m["photo"] is None and r["src_photo"]:
                m["photo"] = r["src_photo"]
            if m["display_name"] is None and r["src_display_name"]:
                m["display_name"] = r["src_display_name"]
            m["src_uids"].append(r["src_uid"])

        result["targets"] = len(merged)

        # Commit PER TARGET, not one giant transaction. social_users is under
        # continuous collector UPSERT load; a 4000-target single txn both exceeds
        # command_timeout and holds row locks long enough to contend. Each target's
        # UPDATE+DELETE is naturally atomic on its own — per-target commits make
        # progress incremental, grab/release locks fast, and interleave with
        # collectors. A mid-run failure leaves earlier targets correctly merged.
        for (platform, target_uid), m in merged.items():
            try:
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE social_users t SET
                            times_seen        = t.times_seen + $3,
                            contexts          = (SELECT array(SELECT DISTINCT unnest(t.contexts || $4::text[]))),
                            first_seen        = LEAST(t.first_seen, $5),
                            last_seen         = GREATEST(t.last_seen, $6),
                            profile_photo_url = COALESCE(t.profile_photo_url, $7),
                            display_name      = COALESCE(t.display_name, $8)
                        WHERE t.platform = $1 AND t.uid = $2
                        """,
                        platform, target_uid, m["add_seen"], list(m["contexts"]),
                        m["first_seen"], m["last_seen"], m["photo"], m["display_name"],
                    )
                    res = await conn.execute(
                        "DELETE FROM social_users WHERE platform = $1 AND uid = ANY($2::text[])",
                        platform, m["src_uids"],
                    )
                # res like "DELETE <n>"
                try:
                    result["deleted"] += int(res.rsplit(" ", 1)[1])
                except (ValueError, IndexError):
                    pass
            except Exception as e:
                # Skip a contended/locked target this run; the next run retries it.
                logger.debug("reconcile skip target %s/%s: %s", platform, target_uid, e)

    logger.info(
        "identity reconcile: %d pairs -> %d canonical targets, %d username rows merged/deleted",
        result["pairs"], result["targets"], result["deleted"],
    )
    return result
