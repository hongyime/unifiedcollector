"""
ChangeTracker: detects and records field-level profile changes from user sightings.
"""
from __future__ import annotations

import json

import asyncpg

# Tracked fields and their payload extraction paths
TRACKED_FIELDS: dict[str, str] = {
    "username":         "username",
    "first_name":       "first_name",
    "last_name":        "last_name",
    "bio":              "bio",
    "profile_photo_id": "photo.photo_id",  # nested path
}


def _extract_field_values(payload: dict) -> dict[str, str | None]:
    """
    Extracts the five tracked field values from a Telethon user snapshot payload.

    Empty string ("") is treated as None (absent).
    """
    photo = payload.get("photo")
    profile_photo_id: str | None = None
    if photo and photo.get("photo_id"):
        profile_photo_id = str(photo["photo_id"])

    return {
        "username":         payload.get("username") or None,
        "first_name":       payload.get("first_name") or None,
        "last_name":        payload.get("last_name") or None,
        "bio":              payload.get("bio") or None,
        "profile_photo_id": profile_photo_id,
    }


class ChangeTracker:
    def __init__(self, db_pool: asyncpg.Pool) -> None:
        """
        db_pool: asyncpg pool (user_intel_user credentials).
        No in-memory state is maintained between sightings — all state is read from DB.
        """
        self._pool = db_pool

    async def process_sighting(self, sighting: dict) -> None:
        """
        Main entry point for one user_sightings row.

        sighting keys used: user_id, payload (JSONB dict), seen_at

        Decision tree per field:
          - incoming is None  → skip (partial payload)
          - last_known is None → skip (first non-empty observation, establish baseline)
          - incoming != last_known → write change record
          - else → skip (no change)
        """
        user_id: int = sighting["user_id"]
        payload = sighting["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)

        last_state = await self._fetch_last_known_state(user_id)
        incoming = _extract_field_values(payload)

        for field in TRACKED_FIELDS:
            old_val = last_state.get(field)   # None if no prior history
            new_val = incoming.get(field)     # None if absent/empty in payload

            if new_val is None:
                # Absent or empty in payload — do not overwrite known state
                continue
            elif old_val is None:
                # First non-empty observation — establish baseline, no change record
                continue
            elif new_val != old_val:
                # Genuine change
                await self._write_change(user_id, field, old_val, new_val)
            # else: same value — no change, skip

    async def _fetch_last_known_state(self, user_id: int) -> dict[str, str | None]:
        """
        Returns the most recent non-empty value for each tracked field for this user.

        Uses DISTINCT ON to get the latest row per field in a single query.
        Fields with no history row are mapped to None.
        """
        rows = await self._pool.fetch(
            """
            SELECT DISTINCT ON (field_name)
                   field_name, new_value
              FROM user_intelligence.user_history
             WHERE user_id = $1
             ORDER BY field_name, changed_at DESC;
            """,
            user_id,
        )

        # Start with all tracked fields set to None
        state: dict[str, str | None] = {field: None for field in TRACKED_FIELDS}
        for row in rows:
            state[row["field_name"]] = row["new_value"]
        return state

    async def _write_change(
        self,
        user_id: int,
        field_name: str,
        old_value: str,
        new_value: str,
    ) -> None:
        """
        Inserts one change record into user_intelligence.user_history.
        """
        await self._pool.execute(
            """
            INSERT INTO user_intelligence.user_history
              (user_id, field_name, old_value, new_value, changed_at)
            VALUES ($1, $2, $3, $4, NOW());
            """,
            user_id,
            field_name,
            old_value,
            new_value,
        )
