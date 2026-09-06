"""Mixins for :class:`src.collectors.telegram.TelegramCollector`.

Each submodule owns one concern of the collector (backfill, realtime,
dialogs, members, profile, media). ``TelegramCollector`` composes them
via multiple inheritance so the public class surface is preserved.

MRO-preservation snapshot — captured at the start of PERF-004 sub-plan 4C,
before any method extraction, via::

    from src.collectors.telegram import TelegramCollector
    sorted(m for m in dir(TelegramCollector) if not m.startswith("_"))

Result (34 names)::

    [
        "INGEST_PATH",
        "SOURCE_NAME",
        "USE_ACCOUNT_POOL",
        "USE_HUMAN_RATE_LIMITER",
        "account_media_dir",
        "backfill_chat",
        "build_filename",
        "cleanup",
        "collect",
        "collect_chat_members",
        "collect_dialogs",
        "collect_realtime",
        "collect_user_profile",
        "download_media",
        "download_message_media",
        "ensure_internet",
        "get_backfill_items",
        "heartbeat_source_health",
        "insert_media_item",
        "intentional_idle_reason",
        "is_known",
        "mark_target_collected",
        "media_dir",
        "progress_count",
        "run",
        "run_backfill",
        "save_file",
        "save_json",
        "send_to_dlq",
        "set_pool",
        "sha256_bytes",
        "should_notify_run_error",
        "stop",
        "wait_rate_limit",
    ]

Every mixin extraction must preserve this set (equal-or-superset) so
``TelegramCollector`` remains contract-compatible for the scheduler,
the CLI, and the tests under ``tests/collectors/test_telegram*.py``.
"""
