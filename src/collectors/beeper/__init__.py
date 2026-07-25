"""Beeper Desktop Local API collector — polymorphic shadow ingest.

Replaces the old `matrix.py` (Wave 1 read-only Matrix client). The new Beeper
Desktop architecture exposes a single REST/WS API on `127.0.0.1:23373` that
spans every connected network — Telegram, WhatsApp, Discord, Signal,
LinkedIn, Facebook, Google Chat, Instagram, Slack, iMessage, native Matrix.

This collector ingests chats + messages + participants from ALL bridges into
the polymorphic `beeper_shadow_*` tables, providing redundancy for our
first-party collectors (parallel-tables model — no dedupe layer).

Read-only by design. The endpoints to send / react / edit / delete are NOT
called from this module. Outbound features are intentionally unimplemented to
match the project's no-outbound rule.

Auth: Bearer token from BEEPER_DESKTOP_API_TOKEN env (created by the user via
Beeper Desktop Settings → Developer → Access Tokens). Token does NOT expire
on its own; the user can revoke it from the same screen.

Network: defaults to `http://host.docker.internal:23373`. The Docker collector
service needs `extra_hosts: ["host.docker.internal:host-gateway"]` in compose.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote

import httpx

from src.core.base_collector import BaseCollector
from src.core.rate_limit_events import record_rate_limit_event
from src.core.raw_archive import report_raw_archive_result
from src.core.user_change_tracker import (
    UserChangeTracker,
    BEEPER_TRACKED_FIELDS,
)
from src.core.vault import VAULT_ROOT, write_atomic_artifact, write_raw_payload

logger = logging.getLogger(__name__)

# Page size for Beeper chat/message pagination. Was hardcoded 50; raising it
# fetches more per round-trip, speeding up sync + backfill. The Beeper Desktop
# local API comfortably handles 100-200. Override via BEEPER_PAGE_SIZE.
_BEEPER_PAGE_SIZE = int(os.getenv("BEEPER_PAGE_SIZE", "100"))

# Transient-network retry tuning. A flaky resolver (e.g. a restarting pihole
# container) makes `host.docker.internal` momentarily unresolvable; that is a
# blip, not a failure. Retry a few times with short backoff before giving up.
_BEEPER_TRANSIENT_RETRIES = int(os.getenv("BEEPER_TRANSIENT_RETRIES", "3"))
_BEEPER_TRANSIENT_BACKOFF = float(os.getenv("BEEPER_TRANSIENT_BACKOFF", "1.5"))

# Permanent-failure tombstoning (2026-06-21). Some assets can NEVER be served:
# Beeper evicted them from its local cache (ENOENT) or the asset's access token is
# invalid. Without giving up, get_backfill_items re-selects them every cycle
# forever (no media_items row is ever written), starving new collection and
# flat-lining progress_count (-> watchdog false-kills -> "beeper disconnected").
# After this many failed download attempts, tombstone the content_id so it is
# never retried. Recoverable blips fail < this and recover normally.
_BEEPER_MEDIA_TOMBSTONE_AFTER = int(os.getenv("BEEPER_MEDIA_TOMBSTONE_AFTER", "3"))
_BEEPER_429_COOLDOWN_SECONDS = int(os.getenv("BEEPER_429_COOLDOWN_SECONDS", "900"))
_BEEPER_HTTP_STATUS_RE = re.compile(r"->\s*(\d{3})")


def _tier1_raw_archives_enabled() -> bool:
    raw = os.getenv("COLLECTOR_TIER1_RAW_PAYLOADS_ENABLED", "1")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


# ── feature gate ──────────────────────────────────────────────────────────


def is_enabled() -> bool:
    """True iff BEEPER_COLLECTOR_ENABLED is truthy AND a token is set."""
    from src.core.env import env_bool
    if not env_bool("BEEPER_COLLECTOR_ENABLED", default=False):
        return False
    return bool(os.environ.get("BEEPER_DESKTOP_API_TOKEN", "").strip())


# ── HTTP client ───────────────────────────────────────────────────────────


class BeeperAPIError(RuntimeError):
    """Any non-2xx response or transport error from the Beeper Desktop API."""


class BeeperTransientError(BeeperAPIError):
    """A transient transport condition — DNS/name-resolution blip or connect
    timeout to the local Beeper Desktop API. Retryable; callers should treat
    it as 'try again next cycle', NOT as a hard error to alarm on."""


# Substrings that mark a transient name-resolution / DNS failure across
# platforms (Linux glibc, musl, macOS, Windows getaddrinfo).
_TRANSIENT_DNS_MARKERS = (
    "getaddrinfo",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname",
    "name resolution",
    "no address associated with hostname",
    "name does not resolve",
    "try again",
)


def _is_transient_network_error(exc: BaseException | None) -> bool:
    """True if `exc` looks like a transient DNS / connect blip worth retrying."""
    if exc is None:
        return False
    # Connect-level failures and read timeouts are inherently transient: the
    # local API is briefly unreachable, not permanently broken.
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)):
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_DNS_MARKERS)


class BeeperClient:
    """Thin async HTTP wrapper for /v1/* and /v0/mcp.

    Holds one httpx.AsyncClient with bearer auth + sane timeouts. Built for
    long-lived reuse — `await client.close()` at shutdown.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        *,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = (
            base_url
            or os.environ.get("BEEPER_DESKTOP_API_URL", "http://127.0.0.1:23373")
        ).rstrip("/")
        self.token = token or os.environ.get("BEEPER_DESKTOP_API_TOKEN", "").strip()
        if not self.token:
            raise BeeperAPIError("BEEPER_DESKTOP_API_TOKEN is not set")

        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Issue an httpx request with transient-network retry.

        Transient DNS/connect blips (a restarting resolver, a momentarily
        unreachable local API) are retried with short backoff. If still failing
        after the retry budget, raise BeeperTransientError (so the caller can
        treat it as 'retry next cycle' rather than alarm). Genuine transport
        errors raise BeeperAPIError.
        """
        attempts = max(1, _BEEPER_TRANSIENT_RETRIES) + 1
        last_exc: httpx.HTTPError | None = None
        for i in range(attempts):
            try:
                return await self._http.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                last_exc = exc
                if _is_transient_network_error(exc) and i < attempts - 1:
                    delay = _BEEPER_TRANSIENT_BACKOFF * (2 ** i)
                    logger.debug(
                        "Beeper transient blip on %s %s (%s); retry %d/%d in %.1fs",
                        method, path, exc, i + 1, attempts - 1, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                break
        if _is_transient_network_error(last_exc):
            raise BeeperTransientError(
                f"{method} {path} transient transport error after "
                f"{attempts} attempts: {last_exc}"
            ) from last_exc
        raise BeeperAPIError(f"{method} {path} transport error: {last_exc}") from last_exc

    async def _get(self, path: str, params: Optional[dict] = None) -> Any:
        resp = await self._request("GET", path, params=params)
        if resp.status_code >= 400:
            raise BeeperAPIError(
                f"GET {path} -> {resp.status_code}: {resp.text[:300]}"
            )
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise BeeperAPIError(f"GET {path} non-JSON body: {exc}") from exc

    # ── public surface ────────────────────────────────────────────────

    async def info(self) -> dict:
        return await self._get("/v1/info")

    async def accounts(self) -> list[dict]:
        return await self._get("/v1/accounts")

    async def chats(
        self,
        *,
        cursor: Optional[str] = None,
        limit: int = 50,
        account_id: Optional[str] = None,
    ) -> dict:
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if account_id:
            params["accountID"] = account_id
        return await self._get("/v1/chats", params=params)

    async def messages(
        self,
        chat_id: str,
        *,
        cursor: Optional[str] = None,
        direction: str = "before",
        limit: int = 50,
    ) -> dict:
        encoded = quote(chat_id, safe="")
        params: dict[str, Any] = {"limit": limit, "direction": direction}
        if cursor:
            params["cursor"] = cursor
        return await self._get(f"/v1/chats/{encoded}/messages", params=params)

    async def serve_asset(self, src_url: str, *, timeout: float = 120.0) -> bytes:
        """Fetch decrypted media bytes via /v1/assets/serve?url=<srcURL>."""
        resp = await self._request(
            "GET",
            "/v1/assets/serve",
            params={"url": src_url},
            timeout=httpx.Timeout(timeout, connect=10.0),
        )
        if resp.status_code >= 400:
            raise BeeperAPIError(
                f"asset serve -> {resp.status_code}: {resp.text[:300]}"
            )
        return resp.content

    async def iter_chats(
        self, *, account_id: Optional[str] = None, page_size: int = _BEEPER_PAGE_SIZE
    ) -> AsyncIterator[dict]:
        cursor: Optional[str] = None
        while True:
            page = await self.chats(
                cursor=cursor, limit=page_size, account_id=account_id
            )
            items = page.get("items", []) if isinstance(page, dict) else page
            for item in items:
                yield item
            cursor = page.get("nextCursor") if isinstance(page, dict) else None
            if not cursor or not items:
                return

    async def iter_messages(
        self,
        chat_id: str,
        *,
        start_cursor: Optional[str] = None,
        direction: str = "before",
        page_size: int = _BEEPER_PAGE_SIZE,
    ) -> AsyncIterator[tuple[dict, dict]]:
        """Yield (message, page_meta) pairs.

        page_meta carries `oldestCursor` / `newestCursor` / `hasMore` so the
        caller can persist sync state per chat.
        """
        cursor = start_cursor
        while True:
            page = await self.messages(
                chat_id, cursor=cursor, direction=direction, limit=page_size
            )
            items = page.get("items", []) if isinstance(page, dict) else page
            meta = {
                "oldestCursor": page.get("oldestCursor") if isinstance(page, dict) else None,
                "newestCursor": page.get("newestCursor") if isinstance(page, dict) else None,
                "hasMore": page.get("hasMore", False) if isinstance(page, dict) else False,
            }
            for msg in items:
                yield msg, meta
            if not meta["hasMore"]:
                return
            cursor = meta["oldestCursor"] if direction == "before" else meta["newestCursor"]
            if not cursor or not items:
                return


# ── helpers ───────────────────────────────────────────────────────────────


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse Beeper ISO 8601 timestamps; return None on failure."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


_MIME_EXT: dict[str, str] = {
    "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
    "image/webp": "webp", "image/apng": "apng",
    "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov",
    "audio/mpeg": "mp3", "audio/ogg": "ogg",
    "application/pdf": "pdf", "application/zip": "zip",
    "application/x-7z-compressed": "7z",
    "text/plain": "txt", "text/html": "html", "text/css": "css",
    "text/markdown": "md", "text/vcard": "vcf",
    "application/octet-stream": "bin",
}


def _ext_from_mime(mime: str | None, filename: str | None) -> str:
    if filename and "." in filename and not filename.startswith("http"):
        return filename.rsplit(".", 1)[-1].lower()[:10]
    if mime:
        clean = mime.split(";")[0].strip().lower()
        if clean in _MIME_EXT:
            return _MIME_EXT[clean]
        guess = mimetypes.guess_extension(clean, strict=False)
        if guess:
            return guess.lstrip(".")
    return "bin"


def _opt(d: dict, *keys: str) -> Any:
    """Walk `d[key1][key2]...`, returning None on any miss."""
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
        if cur is None:
            return None
    return cur


# ── DB writers ────────────────────────────────────────────────────────────


class BeeperWriter:
    """asyncpg writers for the beeper_shadow_* tables."""

    def __init__(self, pool: Any, log: Optional[logging.Logger] = None) -> None:
        self.pool = pool
        self.log = log or logger

    def _archive_raw_payload(
        self,
        *,
        artifact_id: str,
        payload: dict | list,
        target_tables: list[str],
        metadata: dict | None = None,
    ) -> None:
        if not _tier1_raw_archives_enabled():
            return
        try:
            result = write_raw_payload(
                source="beeper",
                artifact_id=artifact_id,
                payload=payload,
                metadata=metadata or {},
                target_tables=target_tables,
                root=VAULT_ROOT,
            )
            report_raw_archive_result(
                self.pool,
                source="beeper",
                artifact_id=artifact_id,
                result=result,
                metadata=metadata,
                log=self.log,
            )
        except Exception as exc:
            self.log.debug("beeper raw archive failed for %s: %s", artifact_id, exc)
            report_raw_archive_result(
                self.pool,
                source="beeper",
                artifact_id=artifact_id,
                result=None,
                metadata=metadata,
                log=self.log,
                error=str(exc),
            )

    async def upsert_account(self, acc: dict) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO beeper_shadow_accounts (
                    account_id, network, login_id, bridge_type, bridge_provider,
                    user_id, user_full_name, user_username, user_email,
                    user_phone, img_url, status, raw,
                    first_seen_at, last_seen_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                    now(), now()
                )
                ON CONFLICT (account_id) DO UPDATE SET
                    network = EXCLUDED.network,
                    login_id = EXCLUDED.login_id,
                    bridge_type = EXCLUDED.bridge_type,
                    bridge_provider = EXCLUDED.bridge_provider,
                    user_id = EXCLUDED.user_id,
                    user_full_name = EXCLUDED.user_full_name,
                    user_username = EXCLUDED.user_username,
                    user_email = EXCLUDED.user_email,
                    user_phone = EXCLUDED.user_phone,
                    img_url = EXCLUDED.img_url,
                    status = EXCLUDED.status,
                    raw = EXCLUDED.raw,
                    last_seen_at = now()
                """,
                acc.get("accountID"),
                acc.get("network") or "unknown",
                str(acc.get("loginID")) if acc.get("loginID") is not None else None,
                _opt(acc, "bridge", "type"),
                _opt(acc, "bridge", "provider"),
                _opt(acc, "user", "id"),
                _opt(acc, "user", "fullName"),
                _opt(acc, "user", "username"),
                _opt(acc, "user", "email"),
                _opt(acc, "user", "phoneNumber"),
                _opt(acc, "user", "imgURL"),
                acc.get("status"),
                json.dumps(acc, default=str),
            )
        account_id = acc.get("accountID") or "unknown"
        self._archive_raw_payload(
            artifact_id=f"accounts/{account_id}",
            payload=acc,
            target_tables=["beeper_shadow_accounts"],
            metadata={
                "account_id": account_id,
                "network": acc.get("network"),
                "ingest_path": "messaging",
                "raw_payload_kind": "account",
            },
        )

    async def upsert_chat(self, chat: dict) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO beeper_shadow_chats (
                    chat_id, local_chat_id, account_id, network,
                    title, description, img_url, chat_type,
                    is_read_only, is_unread, is_archived, is_muted,
                    is_low_priority, last_message_ts, raw,
                    first_seen_at, last_seen_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                    $13, $14, $15, now(), now()
                )
                ON CONFLICT (chat_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    description = EXCLUDED.description,
                    img_url = EXCLUDED.img_url,
                    chat_type = EXCLUDED.chat_type,
                    is_read_only = EXCLUDED.is_read_only,
                    is_unread = EXCLUDED.is_unread,
                    is_archived = EXCLUDED.is_archived,
                    is_muted = EXCLUDED.is_muted,
                    is_low_priority = EXCLUDED.is_low_priority,
                    last_message_ts = COALESCE(
                        EXCLUDED.last_message_ts, beeper_shadow_chats.last_message_ts
                    ),
                    raw = EXCLUDED.raw,
                    last_seen_at = now()
                """,
                chat.get("id"),
                str(chat.get("localChatID")) if chat.get("localChatID") is not None else None,
                chat.get("accountID"),
                chat.get("network") or "unknown",
                chat.get("title"),
                chat.get("description"),
                chat.get("imgURL"),
                chat.get("type"),
                bool(chat.get("isReadOnly", False)),
                chat.get("isUnread"),
                chat.get("isArchived"),
                chat.get("isMuted"),
                chat.get("isLowPriority"),
                _parse_ts(_opt(chat, "lastMessage", "timestamp")) or _parse_ts(chat.get("lastMessageTimestamp")),
                json.dumps(chat, default=str),
            )

        # Participants (best-effort; some networks omit the list)
        participants = _opt(chat, "participants", "items") or []
        if isinstance(participants, list) and participants:
            await self._upsert_participants(chat["id"], chat.get("network", ""), participants)
        chat_id = chat.get("id") or "unknown"
        self._archive_raw_payload(
            artifact_id=f"chats/{chat_id}",
            payload=chat,
            target_tables=["beeper_shadow_chats", "beeper_shadow_participants"],
            metadata={
                "chat_id": chat_id,
                "account_id": chat.get("accountID"),
                "network": chat.get("network"),
                "ingest_path": "messaging",
                "raw_payload_kind": "chat",
            },
        )

    async def _upsert_participants(
        self, chat_id: str, network: str, participants: list[dict]
    ) -> None:
        # ── User-intelligence diff (Tier 4): snapshot this chat's existing
        # participant rows BEFORE upserting so UserChangeTracker can compare
        # old → new and emit one row per changed field into beeper_user_changes
        # (keyed by participant_id). Wrapped in try/except so any failure is
        # non-fatal to ingestion.
        prev_by_pid: dict[str, dict] = {}
        try:
            async with self.pool.acquire() as conn:
                prev_rows = await conn.fetch(
                    "SELECT participant_id, username, full_name, img_url "
                    "FROM beeper_shadow_participants WHERE chat_id = $1",
                    chat_id,
                )
            prev_by_pid = {r["participant_id"]: dict(r) for r in prev_rows}
        except Exception as exc:
            logger.debug("user_change_tracker[beeper]: prev-rows fetch failed: %s", exc)

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for p in participants:
                    pid = p.get("id")
                    if not pid:
                        continue
                    await conn.execute(
                        """
                        INSERT INTO beeper_shadow_participants (
                            chat_id, participant_id, network, username, full_name,
                            img_url, is_self, is_admin, is_pending, is_network_bot,
                            cannot_message, raw, first_seen_at, last_seen_at
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                            now(), now()
                        )
                        ON CONFLICT (chat_id, participant_id) DO UPDATE SET
                            username = EXCLUDED.username,
                            full_name = EXCLUDED.full_name,
                            img_url = EXCLUDED.img_url,
                            is_self = EXCLUDED.is_self,
                            is_admin = EXCLUDED.is_admin,
                            is_pending = EXCLUDED.is_pending,
                            is_network_bot = EXCLUDED.is_network_bot,
                            cannot_message = EXCLUDED.cannot_message,
                            raw = EXCLUDED.raw,
                            last_seen_at = now()
                        """,
                        chat_id,
                        pid,
                        network,
                        p.get("username"),
                        p.get("fullName"),
                        p.get("imgURL"),
                        bool(p.get("isSelf", False)),
                        bool(p.get("isAdmin", False)),
                        bool(p.get("isPending", False)),
                        bool(p.get("isNetworkBot", False)),
                        bool(p.get("cannotMessage", False)),
                        json.dumps(p, default=str),
                    )
        self._archive_raw_payload(
            artifact_id=f"chats/{chat_id}/participants",
            payload=participants,
            target_tables=["beeper_shadow_participants"],
            metadata={
                "chat_id": chat_id,
                "network": network,
                "ingest_path": "messaging",
                "raw_payload_kind": "participants",
            },
        )

        # ── Change-log write (non-fatal, after the upsert transaction closes
        # so we never hold two pool connections at once). Diffs are computed
        # against the per-(chat, participant) snapshot but logged under the
        # participant_id alone.
        try:
            tracker = UserChangeTracker(self.pool)
            for p in participants:
                pid = p.get("id")
                if not pid:
                    continue
                new_snapshot = {
                    "username":  p.get("username"),
                    "full_name": p.get("fullName"),
                    "img_url":   p.get("imgURL"),
                }
                await tracker.detect_and_log(
                    table="beeper_user_changes",
                    pk_col="user_id",
                    pk_val=str(pid),
                    current_row=prev_by_pid.get(pid),
                    new_row=new_snapshot,
                    fields=BEEPER_TRACKED_FIELDS,
                )
        except Exception as exc:
            logger.debug("user_change_tracker[beeper]: detect_and_log failed: %s", exc)

    async def upsert_message(self, msg: dict) -> bool:
        """Upsert a message. Returns True if the row is new (insert), False if it
        was an update — caller uses this for paging-stop heuristics."""
        ts = _parse_ts(msg.get("timestamp"))
        # Some networks (esp. Discord attachments/screenshots) deliver a bogus
        # epoch-0 timestamp ("1970-01-01T00:00:00.000Z"). These are REAL messages
        # with content, so don't store 1970 (it poisons "oldest" queries) and don't
        # drop them — fall back to ingest time as the best available proxy.
        if ts is not None and ts.year < 2000:
            ts = None
        if not ts:
            if msg.get("text") or msg.get("attachments"):
                ts = datetime.now(timezone.utc)  # keep real content w/ observation time
            else:
                self.log.warning(
                    "skipping message with no timestamp: chat=%s id=%s",
                    msg.get("chatID"), msg.get("id"),
                )
                return False

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO beeper_shadow_messages (
                    message_id, chat_id, account_id, network,
                    sender_id, sender_name, is_sender,
                    timestamp, sort_key, msg_type, text,
                    is_deleted, is_unread, mentions, seen,
                    reply_to_id, edited_at, attachments, reactions, raw, deleted_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                    $14, $15, $16, $17, $18, $19, $20, $21
                )
                ON CONFLICT (chat_id, message_id) DO UPDATE SET
                    sender_id = EXCLUDED.sender_id,
                    sender_name = EXCLUDED.sender_name,
                    is_sender = EXCLUDED.is_sender,
                    timestamp = EXCLUDED.timestamp,
                    sort_key = EXCLUDED.sort_key,
                    msg_type = EXCLUDED.msg_type,
                    text = EXCLUDED.text,
                    is_deleted = EXCLUDED.is_deleted,
                    is_unread = EXCLUDED.is_unread,
                    mentions = EXCLUDED.mentions,
                    seen = EXCLUDED.seen,
                    reply_to_id = EXCLUDED.reply_to_id,
                    edited_at = EXCLUDED.edited_at,
                    attachments = EXCLUDED.attachments,
                    reactions = EXCLUDED.reactions,
                    raw = EXCLUDED.raw,
                    -- stamp deleted_at the first time a message flips to deleted
                    deleted_at = CASE
                        WHEN EXCLUDED.is_deleted AND beeper_shadow_messages.deleted_at IS NULL
                        THEN now() ELSE beeper_shadow_messages.deleted_at END
                RETURNING (xmax = 0) AS inserted
                """,
                msg.get("id"),
                msg.get("chatID"),
                msg.get("accountID"),
                msg.get("network") or "unknown",
                msg.get("senderID"),
                msg.get("senderName"),
                bool(msg.get("isSender", False)),
                ts,
                str(msg.get("sortKey")) if msg.get("sortKey") is not None else None,
                msg.get("type"),
                msg.get("text"),
                bool(msg.get("isDeleted", False)),
                msg.get("isUnread"),
                json.dumps(msg.get("mentions") or [], default=str),
                json.dumps(msg.get("seen") or {}, default=str),
                _opt(msg, "replyTo", "id") or msg.get("replyToID"),
                _parse_ts(msg.get("editedTimestamp")),
                json.dumps(msg.get("attachments") or [], default=str),
                json.dumps(msg.get("reactions") or [], default=str),
                json.dumps(msg, default=str),
                datetime.now(timezone.utc) if msg.get("isDeleted") else None,
            )
        self._archive_raw_payload(
            artifact_id=f"messages/{msg.get('chatID') or 'unknown'}/{msg.get('id') or 'unknown'}",
            payload=msg,
            target_tables=["beeper_shadow_messages"],
            metadata={
                "message_id": msg.get("id"),
                "chat_id": msg.get("chatID"),
                "account_id": msg.get("accountID"),
                "network": msg.get("network"),
                "ingest_path": "messaging",
                "raw_payload_kind": "message",
            },
        )
        return bool(row and row["inserted"])

    async def update_sync_state(
        self,
        chat_id: str,
        *,
        oldest_cursor: Optional[str] = None,
        newest_cursor: Optional[str] = None,
        backfill_complete: Optional[bool] = None,
        last_message_ts: Optional[datetime] = None,
        error: Optional[str] = None,
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO beeper_shadow_sync_state (
                    chat_id, oldest_cursor, newest_cursor, backfill_complete,
                    last_synced_at, last_message_ts, error_count, last_error
                ) VALUES (
                    $1, $2, $3, COALESCE($4, FALSE), now(), $5,
                    CASE WHEN $6::text IS NOT NULL THEN 1 ELSE 0 END, $6
                )
                ON CONFLICT (chat_id) DO UPDATE SET
                    oldest_cursor = COALESCE(EXCLUDED.oldest_cursor, beeper_shadow_sync_state.oldest_cursor),
                    newest_cursor = COALESCE(EXCLUDED.newest_cursor, beeper_shadow_sync_state.newest_cursor),
                    backfill_complete = COALESCE($4, beeper_shadow_sync_state.backfill_complete),
                    last_synced_at = now(),
                    last_message_ts = COALESCE(EXCLUDED.last_message_ts, beeper_shadow_sync_state.last_message_ts),
                    error_count = CASE
                        WHEN $6::text IS NOT NULL THEN beeper_shadow_sync_state.error_count + 1
                        ELSE 0
                    END,
                    last_error = $6
                """,
                chat_id,
                oldest_cursor,
                newest_cursor,
                backfill_complete,
                last_message_ts,
                error,
            )

    async def get_sync_state(self, chat_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM beeper_shadow_sync_state WHERE chat_id = $1",
                chat_id,
            )
        return dict(row) if row else None


# ── collector ────────────────────────────────────────────────────────────


class BeeperCollector(BaseCollector):
    """Polymorphic shadow collector across every network bridged by Beeper.

    Lifecycle:
      1. warmup     → /v1/info + /v1/accounts to confirm token + populate accounts
      2. enumerate  → walk /v1/chats to upsert every chat + participants
      3. backfill   → for each chat with backfill_complete=False, walk
                      /v1/chats/{id}/messages?direction=before until oldestCursor
                      stops advancing
      4. tail       → for every chat, fetch newest messages (direction=after,
                      cursor=newest_cursor) and persist them

    The full backfill of 2055 rooms is not done in one cycle — each `collect()`
    pulls a bounded slice (BEEPER_BACKFILL_PAGES_PER_CYCLE per chat by default).
    """

    SOURCE_NAME = "beeper"
    INGEST_PATH = "messaging"  # realtime messaging path (P2 review §3 provenance)
    USE_HUMAN_RATE_LIMITER = False
    USE_ACCOUNT_POOL = False

    def __init__(
        self,
        client: Optional[BeeperClient] = None,
        writer: Optional[BeeperWriter] = None,
    ) -> None:
        super().__init__()
        self._client_owned = client is None
        self.client = client or BeeperClient()
        self._writer_override = writer

    @property
    def writer(self) -> Optional[BeeperWriter]:
        if self._writer_override:
            return self._writer_override
        if self.pool is None:
            return None
        return BeeperWriter(self.pool, log=logger)

    def should_notify_run_error(self, error: Exception) -> bool:
        """Avoid Telegram noise for Beeper Desktop local transport blips."""
        if isinstance(error, (BeeperTransientError, httpx.TransportError, TimeoutError, OSError)):
            return False
        return True

    @staticmethod
    def _api_error_status(error: BaseException) -> int | None:
        match = _BEEPER_HTTP_STATUS_RE.search(str(error))
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    async def _record_api_http_event(
        self,
        error: BaseException,
        *,
        scope: str,
        account: str | None = None,
        metadata: dict | None = None,
    ) -> bool:
        status = self._api_error_status(error)
        if status not in {401, 403, 429}:
            return False
        await record_rate_limit_event(
            self.pool,
            source=self.SOURCE_NAME,
            account=account or "desktop_api",
            scope=scope,
            status_code=status,
            cooldown_seconds=_BEEPER_429_COOLDOWN_SECONDS if status == 429 else None,
            reason=f"Beeper {scope} HTTP {status}",
            metadata={
                **(metadata or {}),
                "error": str(error)[:500],
            },
        )
        return True

    # ── BaseCollector hooks ───────────────────────────────────────────

    async def collect(self, targets: list[str]) -> dict:
        """One sync cycle. `targets` is unused (Beeper enumerates server-side).

        Returns a stat summary so the dashboard can surface per-cycle counts.
        """
        if self.pool is None:
            raise RuntimeError("BeeperCollector requires a DB pool — call set_pool() first")

        stats = {"accounts": 0, "chats": 0, "messages_inserted": 0,
                 "errors": 0, "transient": 0}
        try:
            stats["accounts"] = await self._sync_accounts()
            stats["chats"] = await self._sync_chats()
            stats["messages_inserted"] = await self._sync_messages()
        except BeeperTransientError as exc:
            # Transient DNS/connect blip (e.g. resolver restart). The next cycle
            # resumes from the persisted cursors, so this is not a real failure
            # — log quietly at INFO and do NOT inflate the hard error count.
            logger.info("Beeper transient network blip (retry next cycle): %s", exc)
            stats["transient"] += 1
        except BeeperAPIError as exc:
            logger.error("Beeper sync failed: %s", exc)
            await self._record_api_http_event(exc, scope="desktop_api")
            stats["errors"] += 1

        logger.info(
            "Beeper cycle done: accounts=%d chats=%d messages=%d errors=%d transient=%d",
            stats["accounts"], stats["chats"], stats["messages_inserted"],
            stats["errors"], stats["transient"],
        )
        return stats

    async def download_media(self, item: dict) -> None:
        """Download a Beeper attachment via /v1/assets/serve and persist to drive."""
        cid = item.get("content_id")
        src_url = item.get("src_url")
        if not cid or not src_url:
            logger.debug("Beeper download_media skipped malformed item: %s", item)
            return
        if self.is_known(cid):
            return
        ext = item.get("extension", "bin")
        network = item.get("network", "unknown")
        chat_id = item.get("chat_id", "unknown")
        content_type = item.get("content_type", "attachment")

        entity_id = f"{network}_{chat_id}"
        entity_name = network
        filename = self.build_filename(
            entity_id, entity_name, content_type, cid, extension=ext,
        )
        # Defensive: a derived extension/name occasionally carries a path
        # separator (seen: "...011839.gg/depress"), which made os.replace target
        # a non-existent subdir -> ENOENT. Strip separators from the basename.
        filename = filename.replace("/", "_").replace("\\", "_")
        try:
            data = await self.client.serve_asset(src_url)
            metadata = {
                "network": network,
                "chat_id": chat_id,
                "message_id": item.get("message_id"),
                "original_filename": item.get("original_filename"),
                "mime_type": item.get("mime_type"),
                "raw": item,
                "rebuild_target_tables": ["media_items", "beeper_shadow_messages"],
            }
            artifact = write_atomic_artifact(
                source=self.SOURCE_NAME,
                artifact_id=cid,
                artifact_kind="media_blob",
                data=data,
                extension=ext,
                metadata={
                    **metadata,
                    "entity_id": entity_id,
                    "entity_name": entity_name,
                    "content_type": content_type,
                    "content_id": cid,
                    "filename": filename,
                    "source_url": src_url.split("?")[0],
                },
                root=VAULT_ROOT,
            )
            if not artifact.path:
                raise RuntimeError(f"vault artifact write failed: {artifact.error}")
            metadata["vault_artifact"] = {
                "ok": artifact.ok,
                "partial": artifact.partial,
                "path": artifact.relative_path,
                "blob_path": artifact.blob_relative_path,
                "sidecar_path": artifact.sidecar.relative_path if artifact.sidecar else None,
                "duplicate_blob": artifact.duplicate_blob,
                "error": artifact.error,
            }
            await self.insert_media_item(
                entity_id=entity_id,
                entity_name=entity_name,
                content_type=content_type,
                content_id=cid,
                filename=filename,
                file_path=str(artifact.path),
                file_size=artifact.file_size,
                width=item.get("width"),
                height=item.get("height"),
                sha256=artifact.sha256,
                source_url=src_url.split("?")[0],
                metadata=metadata,
            )
            if artifact.partial:
                await self.send_to_dlq(
                    entity_id,
                    cid,
                    f"vault artifact partial: {artifact.error}",
                )
            self._known_ids.add(cid)
            logger.debug("Beeper media saved: %s (%d bytes)", cid, len(data))
        except Exception as e:
            logger.warning("Beeper media download failed %s: %s", cid, e)
            if isinstance(e, BeeperAPIError):
                await self._record_api_http_event(
                    e,
                    scope="asset_serve",
                    account=f"{network}:{chat_id}",
                    metadata={
                        "content_id": cid,
                        "network": network,
                        "chat_id": chat_id,
                        "message_id": item.get("message_id"),
                    },
                )
            # Count the failure; permanently-dead assets get tombstoned so the
            # backfill stops re-selecting them forever (see _record_media_failure).
            await self._record_media_failure(cid)
            try:
                await self.send_to_dlq(entity_id, cid, str(e)[:500])
            except Exception:
                pass

    async def _load_media_tombstones(self) -> set[str]:
        """content_ids we've permanently given up on (Beeper-evicted / bad-token
        assets). Used to filter get_backfill_items so we don't re-attempt them."""
        if self.pool is None:
            return set()
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT content_id FROM media_recover_state "
                    "WHERE source='beeper' AND tombstoned_at IS NOT NULL"
                )
            return {r["content_id"] for r in rows}
        except Exception:
            logger.debug("beeper: load tombstones failed", exc_info=True)
            return set()

    async def _record_media_failure(self, content_id: str) -> None:
        """Persist a failed asset download; tombstone after N attempts so it is
        never retried. Reuses the reconciler's media_recover_state table."""
        if self.pool is None or not content_id:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO media_recover_state
                        (source, content_id, attempts, last_attempt_at)
                    VALUES ('beeper', $1, 1, now())
                    ON CONFLICT (source, content_id) DO UPDATE SET
                        attempts = media_recover_state.attempts + 1,
                        last_attempt_at = now(),
                        tombstoned_at = CASE
                            WHEN media_recover_state.attempts + 1 >= $2
                            THEN now() ELSE media_recover_state.tombstoned_at END
                    """,
                    content_id, _BEEPER_MEDIA_TOMBSTONE_AFTER,
                )
        except Exception:
            logger.debug("beeper: record_media_failure failed %s", content_id,
                         exc_info=True)

    async def aclose(self) -> None:
        if self._client_owned:
            await self.client.close()

    # ── pipeline stages ───────────────────────────────────────────────

    async def _sync_accounts(self) -> int:
        accounts = await self.client.accounts()
        if not isinstance(accounts, list):
            return 0
        w = self.writer
        if w is None:
            return 0
        for acc in accounts:
            await w.upsert_account(acc)
        return len(accounts)

    async def _sync_chats(self) -> int:
        w = self.writer
        if w is None:
            return 0
        count = 0
        async for chat in self.client.iter_chats(page_size=_BEEPER_PAGE_SIZE):
            await w.upsert_chat(chat)
            count += 1
            if self._stop.is_set():
                break
        return count

    async def _sync_messages(self) -> int:
        w = self.writer
        if w is None:
            return 0

        max_pages = int(os.environ.get("BEEPER_BACKFILL_PAGES_PER_CYCLE", "3"))
        max_chats = int(os.environ.get("BEEPER_MAX_CHATS_PER_CYCLE", "50"))

        async with self.pool.acquire() as conn:
            chat_rows = await conn.fetch(
                """
                SELECT c.chat_id,
                       c.network,
                       s.oldest_cursor,
                       s.newest_cursor,
                       COALESCE(s.backfill_complete, FALSE) AS backfill_complete,
                       c.last_message_ts
                FROM beeper_shadow_chats c
                LEFT JOIN beeper_shadow_sync_state s USING (chat_id)
                -- Tombstone definitively-dead chats: Beeper returns 404 "Chat not
                -- found" (NOT_FOUND) for chats deleted/unbridged on its side. They
                -- were retried every cycle forever, spamming warnings. Once a chat
                -- has 404'd as NOT_FOUND >=3 times, stop syncing it. (Transient
                -- errors keep retrying — they don't carry NOT_FOUND.)
                WHERE NOT (
                    COALESCE(s.error_count, 0) >= 3
                    AND COALESCE(s.last_error, '') LIKE '%NOT_FOUND%'
                )
                ORDER BY
                    CASE WHEN s.newest_cursor IS NOT NULL THEN 1 ELSE 0 END DESC,
                    s.last_synced_at ASC NULLS FIRST,
                    c.last_message_ts DESC NULLS LAST,
                    COALESCE(s.backfill_complete, FALSE) ASC
                LIMIT $1
                """,
                max_chats,
            )

        inserted_total = 0
        for row in chat_rows:
            if self._stop.is_set():
                break
            inserted = await self._sync_one_chat(
                chat_id=row["chat_id"],
                network=row["network"] or "unknown",
                oldest_cursor=row["oldest_cursor"],
                newest_cursor=row["newest_cursor"],
                backfill_complete=row["backfill_complete"],
                max_pages=max_pages,
                w=w,
            )
            inserted_total += inserted
        return inserted_total

    async def get_backfill_items(self, batch_size: int) -> list[dict]:
        """Return flat attachment items from messages missing media_items entries."""
        if self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT m.message_id, m.chat_id,
                       COALESCE(c.network, m.network, 'unknown') AS network,
                       m.attachments
                FROM beeper_shadow_messages m
                LEFT JOIN beeper_shadow_chats c ON c.chat_id = m.chat_id
                WHERE m.attachments IS NOT NULL
                  AND m.attachments != '[]'
                  AND NOT EXISTS (
                      SELECT 1 FROM media_items mi
                      WHERE mi.source = 'beeper'
                        AND mi.content_id LIKE m.message_id || '_%'
                  )
                ORDER BY m.timestamp DESC
                LIMIT $1
                """,
                batch_size * 3,
            )
        # Skip assets we've permanently given up on (Beeper-evicted / bad-token).
        # Without this they'd be re-selected every cycle forever (the storm).
        tombstoned = await self._load_media_tombstones()
        items: list[dict] = []
        for row in rows:
            try:
                atts = json.loads(row["attachments"]) if isinstance(row["attachments"], str) else row["attachments"]
            except (json.JSONDecodeError, TypeError):
                continue
            network = row["network"]
            chat_id = row["chat_id"]
            message_id = row["message_id"]
            for att in (atts or []):
                if len(items) >= batch_size:
                    break
                src_url = att.get("srcURL")
                if not src_url:
                    continue
                # file:// URLs are local cache paths; use the att id (mxc://) for serve
                if src_url.startswith("file:"):
                    att_id_mxc = att.get("id", "")
                    if att_id_mxc.startswith("mxc://"):
                        src_url = att_id_mxc
                    else:
                        continue
                elif not src_url.startswith("mxc://"):
                    continue
                att_id = att.get("id", "")
                content_id = f"{message_id}_{att_id.split('/')[-1][:40]}" if att_id else message_id
                if content_id in tombstoned:
                    continue
                if self.is_known(content_id):
                    continue
                mime = att.get("mimeType")
                att_type = att.get("type", "unknown")
                content_type = {"img": "image", "video": "video", "audio": "audio"}.get(
                    att_type, "file"
                )
                ext = _ext_from_mime(mime, att.get("fileName"))
                size = att.get("size") or {}
                items.append({
                    "content_id": content_id,
                    "entity_id": f"{network}_{chat_id}",
                    "src_url": src_url,
                    "extension": ext,
                    "network": network,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "content_type": content_type,
                    "original_filename": att.get("fileName"),
                    "mime_type": mime,
                    "width": size.get("width"),
                    "height": size.get("height"),
                })
            if len(items) >= batch_size:
                break
        return items

    async def _download_attachments(self, msg: dict) -> None:
        """Extract attachments from a message and download each one."""
        attachments = msg.get("attachments") or []
        if not attachments:
            return
        network = msg.get("network") or "unknown"
        chat_id = msg.get("chatID") or "unknown"
        message_id = msg.get("id") or "unknown"

        for att in attachments:
            src_url = att.get("srcURL")
            if not src_url:
                continue
            if src_url.startswith("file:"):
                att_id_mxc = att.get("id", "")
                if att_id_mxc.startswith("mxc://"):
                    src_url = att_id_mxc
                else:
                    continue
            elif not src_url.startswith("mxc://"):
                continue
            att_id = att.get("id", "")
            content_id = f"{message_id}_{att_id.split('/')[-1][:40]}" if att_id else message_id
            if self.is_known(content_id):
                continue

            mime = att.get("mimeType")
            att_type = att.get("type", "unknown")
            content_type = {"img": "image", "video": "video", "audio": "audio"}.get(
                att_type, "file"
            )
            ext = _ext_from_mime(mime, att.get("fileName"))
            size = att.get("size") or {}

            await self.download_media({
                "content_id": content_id,
                "src_url": src_url,
                "extension": ext,
                "network": network,
                "chat_id": chat_id,
                "message_id": message_id,
                "content_type": content_type,
                "original_filename": att.get("fileName"),
                "mime_type": mime,
                "width": size.get("width"),
                "height": size.get("height"),
            })

    async def _sync_one_chat(
        self,
        *,
        chat_id: str,
        network: str = "unknown",
        oldest_cursor: Optional[str],
        newest_cursor: Optional[str],
        backfill_complete: bool,
        max_pages: int,
        w: BeeperWriter,
    ) -> int:
        """Tail fresh messages first, then perform bounded historical backfill."""
        inserted = 0
        try:
            # Phase A: freshness. If we have a newest cursor, tail it before
            # historical pages so live DM/group traffic is not stuck behind old
            # backfill.
            tail_inserted = 0
            new_newest = newest_cursor
            if newest_cursor:
                async for msg, meta in self.client.iter_messages(
                    chat_id,
                    start_cursor=newest_cursor,
                    direction="after",
                    page_size=_BEEPER_PAGE_SIZE,
                ):
                    is_new = await w.upsert_message(msg)
                    if is_new:
                        tail_inserted += 1
                        msg.setdefault("network", network)
                        await self._download_attachments(msg)
                    new_newest = meta.get("newestCursor") or new_newest
                    if tail_inserted >= max_pages * 50:
                        break

                # Always bump last_synced_at for chats with a freshness cursor so
                # the round-robin continues rotating through active chats.
                await w.update_sync_state(chat_id, newest_cursor=new_newest)
                inserted += tail_inserted

            # Phase B: bounded historical backfill.
            if not backfill_complete:
                pages = 0
                cursor = oldest_cursor
                latest_oldest = oldest_cursor
                final_oldest = oldest_cursor
                latest_newest = new_newest
                async for msg, meta in self.client.iter_messages(
                    chat_id, start_cursor=cursor, direction="before", page_size=_BEEPER_PAGE_SIZE
                ):
                    is_new = await w.upsert_message(msg)
                    if is_new:
                        inserted += 1
                        msg.setdefault("network", network)
                        await self._download_attachments(msg)
                    final_oldest = meta.get("oldestCursor") or final_oldest
                    if meta.get("newestCursor") and not latest_newest:
                        latest_newest = meta["newestCursor"]
                    pages += 1
                    if pages >= max_pages:
                        break

                done = final_oldest == latest_oldest and latest_oldest is not None
                await w.update_sync_state(
                    chat_id,
                    oldest_cursor=final_oldest,
                    newest_cursor=latest_newest,
                    backfill_complete=done,
                )
            elif not newest_cursor:
                # First-ever sync has no freshness boundary. Establish one once
                # the bounded backfill pass returns a newest cursor.
                await w.update_sync_state(chat_id, newest_cursor=new_newest)

        except BeeperTransientError as exc:
            # Transient DNS/connect blip mid-chat — leave sync_state untouched
            # (no error_count bump) so the chat is retried cleanly next cycle.
            logger.debug("chat %s transient blip (retry next cycle): %s", chat_id, exc)
        except BeeperAPIError as exc:
            logger.warning("chat %s sync error: %s", chat_id, exc)
            await self._record_api_http_event(
                exc,
                scope="desktop_api",
                account=f"{network}:{chat_id}",
                metadata={"network": network, "chat_id": chat_id},
            )
            await w.update_sync_state(chat_id, error=str(exc)[:500])
        return inserted
