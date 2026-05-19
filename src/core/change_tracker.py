import logging

logger = logging.getLogger(__name__)

DEFAULT_TRACKED_FIELDS = [
    "push_name",
    "display_name",
    "phone_number",
    "is_business",
    "status_text",
    "profile_pic_url",
]


def _normalize(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


class ChangeTracker:

    def __init__(self, tracked_fields: list[str] | None = None):
        self._fields = tracked_fields or DEFAULT_TRACKED_FIELDS

    def detect_changes(self, new_payload: dict, last_known: dict | None) -> list[tuple[str, str, str]]:
        if last_known is None:
            return []

        changes: list[tuple[str, str, str]] = []
        for field in self._fields:
            old_val = _normalize(last_known.get(field))
            new_val = _normalize(new_payload.get(field))
            if new_val and new_val != old_val:
                changes.append((field, old_val, new_val))

        return changes

    async def track_and_persist(self, pool, jid: str, new_payload: dict):
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT * FROM wa_user_profiles WHERE jid = $1", jid,
            )

            last_known = dict(existing) if existing else None
            changes = self.detect_changes(new_payload, last_known)

            if changes:
                for field, old_val, new_val in changes:
                    await conn.execute(
                        """
                        INSERT INTO wa_user_history (user_jid, field_name, old_value, new_value)
                        VALUES ($1, $2, $3, $4)
                        """,
                        jid, field, old_val, new_val,
                    )
                logger.debug("User %s: %d field changes detected", jid, len(changes))

            update_fields = {
                k: v for k, v in new_payload.items()
                if k in self._fields and _normalize(v)
            }

            if existing:
                if update_fields:
                    sets = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(update_fields))
                    vals = [jid] + list(update_fields.values())
                    await conn.execute(
                        f"UPDATE wa_user_profiles SET {sets}, last_seen = NOW(), "
                        f"message_count = message_count + 1 WHERE jid = $1",
                        *vals,
                    )
            else:
                cols = ["jid"] + list(update_fields.keys())
                placeholders = ", ".join(f"${i+1}" for i in range(len(cols)))
                col_str = ", ".join(cols)
                vals = [jid] + list(update_fields.values())
                await conn.execute(
                    f"INSERT INTO wa_user_profiles ({col_str}) VALUES ({placeholders})",
                    *vals,
                )

            return changes
