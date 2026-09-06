"""ProfileMixin — user profile metadata + profile photo tracker.

Extracted from :mod:`src.collectors.telegram` (PERF-004 sub-plan 4C step 8).
Mixed into :class:`TelegramCollector` via multiple inheritance; every
``self.*`` reference resolves at MRO-time to the composed class.

Cherry-picked from ``telegramtoolkit/src/managers/download_profile_photos.py``.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.collectors.telegram.session import EntityUnresolvable
from src.core.user_change_tracker import TELEGRAM_TRACKED_FIELDS

if TYPE_CHECKING:  # pragma: no cover — forward-ref hints only
    from src.collectors.telegram.session import TelegramWorker


logger = logging.getLogger("src.collectors.telegram")


class ProfileMixin:
    """User profile fetch + profile photo download (fresh + historical)."""

    async def collect_user_profile(
        self, user_id, worker: "TelegramWorker | None" = None,
    ) -> dict | None:
        """Fetch user metadata + profile photos.

        Returns a dict of the persisted fields, or None if the user can't
        be resolved by any worker.
        """
        if worker is None:
            if not self._workers:
                self._workers = await self._spawn_workers()
            if not self._workers:
                logger.error("collect_user_profile: no workers — bailing")
                return None
            worker = self._workers[0]

        # Resolve via whichever account actually owns this user (cross-account).
        # A single-worker get_entity raised "Cannot find any entity" for users
        # owned by the other N-1 accounts (the profile-path equivalent of the
        # stories bug) — that was the ~75/20min WARNING spam AND lost profiles.
        # Route the rest of the collection through the owning account.
        try:
            owner_w, user = await self._resolve_entity_any_worker(worker, str(user_id))
            worker = owner_w
            client = owner_w.client
        except EntityUnresolvable:
            logger.debug("collect_user_profile: no connected account owns user %s", user_id)
            return None
        except Exception as exc:
            logger.debug("collect_user_profile resolve (transient) for %s: %s", user_id, exc)
            return None

        # ── User-intelligence diff: snapshot the row BEFORE upserting so the
        # change tracker can compare old → new and emit one row per changed
        # field into telegram_user_changes. Wrapped in try/except so any
        # failure (DB, schema drift, etc.) is non-fatal to ingestion.
        prev_row = None
        try:
            async with self.pool.acquire() as conn:
                prev_row = await conn.fetchrow(
                    "SELECT username, first_name, last_name "
                    "FROM telegram_users WHERE platform_user_id = $1",
                    str(getattr(user, "id", user_id)),
                )
        except Exception as exc:
            logger.debug("user_change_tracker: prev-row fetch failed: %s", exc)

        await self._upsert_user_full(user)

        try:
            # Resolve UserChangeTracker via the parent module at call time so
            # tests can monkeypatch ``src.collectors.telegram.UserChangeTracker``
            # to inject a fake — the mixin lives in a submodule and would
            # otherwise pin its own module-local binding.
            from src.collectors.telegram import UserChangeTracker

            tracker = UserChangeTracker(self.pool)
            new_snapshot = {
                "username":   getattr(user, "username", None),
                "first_name": getattr(user, "first_name", None),
                "last_name":  getattr(user, "last_name", None),
                "bio":        getattr(user, "about", None) or getattr(user, "bio", None),
                "premium":    getattr(user, "premium", None),
                "verified":   getattr(user, "verified", None),
                "phone":      getattr(user, "phone", None),
            }
            photo = getattr(user, "photo", None)
            if photo is not None:
                new_snapshot["profile_photo_id"] = getattr(photo, "photo_id", None)
            await tracker.detect_and_log(
                table="telegram_user_changes",
                pk_col="user_id",
                pk_val=int(getattr(user, "id", 0) or 0),
                current_row=dict(prev_row) if prev_row else None,
                new_row=new_snapshot,
                fields=TELEGRAM_TRACKED_FIELDS,
            )
        except Exception as exc:
            logger.debug("user_change_tracker: detect_and_log failed: %s", exc)

        uid = str(getattr(user, "id", user_id))
        uname = (getattr(user, "username", None)
                 or getattr(user, "first_name", None) or uid)

        # Profile photo (first/largest) — download the file AND record its path in
        # telegram_users.photo_url so the user registry/dashboard can show it.
        try:
            cid = f"profile_user_{uid}"
            if not self.is_known(cid):
                photo_bytes = await client.download_profile_photo(user, bytes)
                if photo_bytes:
                    await self.download_media({
                        "entity_id": uid,
                        "entity_name": uname,
                        "content_type": "user_profile_photo",
                        "content_id": cid,
                        "data": photo_bytes,
                        "extension": "jpg",
                        # Deep-link URL — public users have https://t.me/<username>
                        "chat_username": getattr(user, "username", None),
                    }, worker=worker)
            # set photo_url to the stored file (whether just downloaded or already known)
            async with self.pool.acquire() as conn:
                fp = await conn.fetchval(
                    "SELECT file_path FROM media_items WHERE source='telegram' AND content_id=$1", cid
                )
                await conn.execute(
                    "UPDATE telegram_users SET photo_url=COALESCE($1, photo_url), updated_at=NOW() WHERE platform_user_id=$2",
                    fp, uid,
                )
        except Exception as exc:
            logger.debug("user profile photo failed for %s: %s", uid, exc)

        # Older photos via get_profile_photos (cherry-pick from toolkit).
        try:
            photos = await client.get_profile_photos(user)
            for idx, photo in enumerate(photos or []):
                pid = getattr(photo, "id", None)
                if pid is None:
                    continue
                cid_p = f"profile_user_{uid}_{pid}"
                if self.is_known(cid_p):
                    continue
                try:
                    photo_bytes = await client.download_media(photo, bytes)
                    if photo_bytes:
                        await self.download_media({
                            "entity_id": uid,
                            "entity_name": uname,
                            "content_type": "user_profile_photo",
                            "content_id": cid_p,
                            "data": photo_bytes,
                            "extension": "jpg",
                            "chat_username": getattr(user, "username", None),
                        }, worker=worker)
                except Exception as exc:
                    logger.debug("photo %s for %s failed: %s", pid, uid, exc)
        except Exception as exc:
            logger.debug("get_profile_photos failed for %s: %s", uid, exc)

        return {
            "platform_user_id": uid,
            "username": getattr(user, "username", None),
            "first_name": getattr(user, "first_name", None),
            "last_name": getattr(user, "last_name", None),
            "phone": getattr(user, "phone", None),
            "is_bot": bool(getattr(user, "bot", False)),
            "is_verified": bool(getattr(user, "verified", False)),
            "is_premium": bool(getattr(user, "premium", False)),
        }

    async def _collect_user_photos_pass(self, worker: "TelegramWorker", batch: int = 15) -> int:
        """Per-cycle bounded sweep: download profile photos for telegram_users that
        don't have one yet + set photo_url. Best-effort (cross-account users that
        worker[0] can't resolve are retried on later cycles)."""
        if not self.pool:
            return 0
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT platform_user_id FROM telegram_users "
                "WHERE photo_url IS NULL AND (is_deleted IS NOT TRUE) "
                "ORDER BY updated_at ASC NULLS FIRST LIMIT $1",
                batch,
            )
        n = 0
        for r in rows:
            if self._stop.is_set():
                break
            try:
                await self.collect_user_profile(r["platform_user_id"], worker=worker)
                n += 1
            except Exception as exc:
                logger.debug("user photo collect failed %s: %s", r["platform_user_id"], exc)
        return n
