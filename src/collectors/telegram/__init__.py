"""Unified Telegram collector — Wave 2 Batch E.

Ports `telegramcollector/services/collector/{backfill_worker,realtime_worker,...}.py`
+ cherry-picks `telegramtoolkit/src/{core/scan_targets,managers/download_profile_photos,
managers/processors/user_analyzer_processor}.py` into a single BaseCollector subclass.

Public surface (called by scheduler / cron):
    - run(targets)          BaseCollector lifecycle entry
    - collect(targets)      per-cycle parallel collect across worker pool
    - collect_realtime()    spawn @client.on(NewMessage) handlers + run forever
    - backfill_chat(chat_id, target_depth=N, max_iterations=M)
                            cursor-based historical pagination (newest -> oldest)
    - collect_dialogs()     iter_dialogs across all workers; upsert telegram_chats
    - collect_chat_members(chat_id)
                            iter_participants → telegram_chat_members upsert
                            (called by daily 03:00 SGT cron — see PRD memory)
    - collect_user_profile(user_id)
                            user metadata + profile photos
    - download_message_media(message_id)
                            single-message media download via Telethon → core.media_download

Anything outbound (send/reply/edit/delete/forward, bot commands, web UI,
bulk_sender) is DROPPED per Wave 0 spec.

Package layout (PERF-004 sub-plan 4C):
    helpers.py       module-level pure functions + regex constants
    session.py       SessionState, TelegramWorker, EntityUnresolvable/Deferred
    mixins/          one concern per file (backfill/realtime/dialogs/members/
                     profile/media) composed onto TelegramCollector
    collector.py     concrete TelegramCollector class + private helpers
    parse.py         Telethon message parsing (independent, unchanged)

This ``__init__.py`` is a thin re-export shell so the pre-split import
surface (``from src.collectors.telegram import TelegramCollector``) and the
extensive private-name imports used by
``tests/collectors/test_telegram*.py`` keep working unchanged.
"""
from __future__ import annotations

# ── Re-exports ────────────────────────────────────────────────────────────
#
# These live here (not in collector.py) so tests can monkeypatch symbols
# on this module — most notably ``VAULT_ROOT`` and ``UserChangeTracker`` —
# and have the changes picked up by the mixins and the collector class.

from src.core.user_change_tracker import (  # noqa: F401 — re-export
    TELEGRAM_TRACKED_FIELDS,
    UserChangeTracker,
)
from src.core.vault import (  # noqa: F401 — re-export (monkeypatch targets)
    VAULT_ROOT,
    write_atomic_artifact,
    write_raw_payload,
)

from src.collectors.telegram.helpers import (  # noqa: F401 — re-export
    _MIME_EXT_MAP,
    _TELEGRAM_LINK_USERNAME_RE,
    _TELEGRAM_RESERVED_PATHS,
    _TELEGRAM_USERNAME_RE,
    _as_int,
    _ext_from_mime,
    _format_exception,
    _is_file_reference_expired,
    _is_flood_wait,
    _is_transient,
    _is_transient_realtime_write_error,
    _message_text_for_mentions,
    _normalize_telegram_username,
    _obj_get,
    _parse_int_set_env,
    _parse_optional_int_env,
    _telegram_message_content_id,
    _telethon_payload,
    _tg_json,
    _tg_jsonb,
    _tier1_raw_archives_enabled,
)
from src.collectors.telegram.session import (  # noqa: F401 — re-export
    EntityResolveDeferred,
    EntityUnresolvable,
    SessionState,
    TelegramWorker,
)

# ``asyncio`` is re-exported so ``monkeypatch.setattr(tg_mod.asyncio,
# "sleep", ...)`` still works from tests that patch through the parent
# module instead of the leaf submodule.
import asyncio  # noqa: E402,F401 — re-export (monkeypatch target)
import logging as _logging

# ``logger`` re-exposed for tests that reference ``tg_mod.logger`` to
# rewire logging in a spec (e.g. ``monkeypatch.setattr(tg_mod, "logger",
# tg_mod.logger)``). Points at the shared parent-module logger used by
# every submodule so log records stay identifiable as
# ``src.collectors.telegram``.
logger = _logging.getLogger("src.collectors.telegram")

# Import the concrete class LAST so the re-exports above are already
# populated on this module by the time the collector's method bodies run
# (they lazy-import ``UserChangeTracker`` / ``VAULT_ROOT`` back through
# this module so tests can redirect behaviour with ``monkeypatch``).
from src.collectors.telegram.collector import TelegramCollector  # noqa: E402,F401 — re-export

__all__ = [
    "EntityResolveDeferred",
    "EntityUnresolvable",
    "SessionState",
    "TELEGRAM_TRACKED_FIELDS",
    "TelegramCollector",
    "TelegramWorker",
    "UserChangeTracker",
    "VAULT_ROOT",
]
