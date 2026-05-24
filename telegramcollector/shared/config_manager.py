from __future__ import annotations

import argparse
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from shared.config_store import config_store


@dataclass
class EnvLine:
    """Internal representation of a single line in a .env file."""

    raw: str
    key: str | None
    value: str | None


@dataclass
class SettingDefinition:
    """Describes one tunable parameter in the SETTING_GROUPS registry."""

    key: str
    group: str
    python_type: type
    default: Any
    min_val: float | None
    max_val: float | None
    step: float | None
    live: bool
    description: str
    requires_restart: bool
    cli_flag: str
    choices: list[str] | None = None  # if set, renders st.selectbox instead of st.text_input
    sensitive: bool = False
    bootstrap_only: bool = False
    owners: tuple[str, ...] | None = None


# ---------------------------------------------------------------------------
# SETTING_GROUPS registry — all non-secret tunable settings
# ---------------------------------------------------------------------------
# Rules:
#   - requires_restart = not live
#   - cli_flag = "--" + key.lower().replace("_", "-")
#   - Defaults cross-checked against shared/config.py (config.py wins on conflict)
# ---------------------------------------------------------------------------

SETTING_GROUPS: dict[str, list[SettingDefinition]] = {
    "platform": [
        SettingDefinition(
            key="TG_API_ID", group="platform", python_type=int, default=0,
            min_val=1, max_val=None, step=None, live=False,
            description="Telegram API ID from my.telegram.org",
            requires_restart=True, cli_flag="--tg-api-id",
            bootstrap_only=True,
        ),
        SettingDefinition(
            key="TG_API_HASH", group="platform", python_type=str, default="",
            min_val=None, max_val=None, step=None, live=False,
            description="Telegram API hash from my.telegram.org",
            requires_restart=True, cli_flag="--tg-api-hash",
            sensitive=True, bootstrap_only=True,
        ),
        SettingDefinition(
            key="BOT_TOKEN", group="platform", python_type=str, default="",
            min_val=None, max_val=None, step=None, live=False,
            description="Single fallback bot token",
            requires_restart=True, cli_flag="--bot-token",
            sensitive=True, bootstrap_only=True,
        ),
        SettingDefinition(
            key="BOT_TOKENS", group="platform", python_type=str, default="",
            min_val=None, max_val=None, step=None, live=False,
            description="Multi-bot token list (Name:token;Name2:token2)",
            requires_restart=True, cli_flag="--bot-tokens",
            sensitive=True, bootstrap_only=True,
        ),
        SettingDefinition(
            key="FACE_BOT_TOKENS", group="platform", python_type=str, default="",
            min_val=None, max_val=None, step=None, live=False,
            description="Face-recognition publishing bot tokens",
            requires_restart=True, cli_flag="--face-bot-tokens",
            sensitive=True, bootstrap_only=True,
        ),
        SettingDefinition(
            key="BULK_SENDER_BOT_TOKENS", group="platform", python_type=str, default="",
            min_val=None, max_val=None, step=None, live=False,
            description="Bulk sender bot tokens (semicolon-separated)",
            requires_restart=True, cli_flag="--bulk-sender-bot-tokens",
            sensitive=True, bootstrap_only=True,
        ),
        SettingDefinition(
            key="HUB_GROUP_ID", group="platform", python_type=str, default="",
            min_val=None, max_val=None, step=None, live=False,
            description="Hub group numeric ID or @username",
            requires_restart=True, cli_flag="--hub-group-id",
            bootstrap_only=True,
        ),
        SettingDefinition(
            key="DB_HOST", group="platform", python_type=str, default="postgres",
            min_val=None, max_val=None, step=None, live=False,
            description="Postgres host",
            requires_restart=True, cli_flag="--db-host",
            bootstrap_only=True,
        ),
        SettingDefinition(
            key="DB_PORT", group="platform", python_type=int, default=5432,
            min_val=1, max_val=65535, step=None, live=False,
            description="Postgres port",
            requires_restart=True, cli_flag="--db-port",
            bootstrap_only=True,
        ),
        SettingDefinition(
            key="DB_NAME", group="platform", python_type=str, default="telegramcollector",
            min_val=None, max_val=None, step=None, live=False,
            description="Postgres database name",
            requires_restart=True, cli_flag="--db-name",
            bootstrap_only=True,
        ),
        SettingDefinition(
            key="DB_USER", group="platform", python_type=str, default="postgres",
            min_val=None, max_val=None, step=None, live=False,
            description="Postgres username",
            requires_restart=True, cli_flag="--db-user",
            bootstrap_only=True,
        ),
        SettingDefinition(
            key="DB_PASSWORD", group="platform", python_type=str, default="",
            min_val=None, max_val=None, step=None, live=False,
            description="Postgres password",
            requires_restart=True, cli_flag="--db-password",
            sensitive=True, bootstrap_only=True,
        ),
        SettingDefinition(
            key="REDIS_HOST", group="platform", python_type=str, default="redis",
            min_val=None, max_val=None, step=None, live=False,
            description="Redis host",
            requires_restart=True, cli_flag="--redis-host",
            bootstrap_only=True,
        ),
        SettingDefinition(
            key="REDIS_PORT", group="platform", python_type=int, default=6379,
            min_val=1, max_val=65535, step=None, live=False,
            description="Redis port",
            requires_restart=True, cli_flag="--redis-port",
            bootstrap_only=True,
        ),
        SettingDefinition(
            key="REDIS_DB", group="platform", python_type=int, default=0,
            min_val=0, max_val=100, step=None, live=False,
            description="Redis DB index",
            requires_restart=True, cli_flag="--redis-db",
            bootstrap_only=True,
        ),
        SettingDefinition(
            key="REDIS_PASSWORD", group="platform", python_type=str, default="",
            min_val=None, max_val=None, step=None, live=False,
            description="Redis password",
            requires_restart=True, cli_flag="--redis-password",
            sensitive=True, bootstrap_only=True,
        ),
        SettingDefinition(
            key="MEDIA_STORE_PATH", group="platform", python_type=str, default="/mnt/hdd/media",
            min_val=None, max_val=None, step=None, live=False,
            description="Media storage base path",
            requires_restart=True, cli_flag="--media-store-path",
            bootstrap_only=True,
        ),
        SettingDefinition(
            key="SESSIONS_BASE_PATH", group="platform", python_type=str, default="/data/sessions",
            min_val=None, max_val=None, step=None, live=False,
            description="Base path for all service sessions",
            requires_restart=True, cli_flag="--sessions-base-path",
            bootstrap_only=True,
        ),
        SettingDefinition(
            key="SESSIONS_DIR", group="platform", python_type=str, default="sessions",
            min_val=None, max_val=None, step=None, live=False,
            description="Legacy sessions directory root",
            requires_restart=True, cli_flag="--sessions-dir",
            bootstrap_only=True,
        ),
        SettingDefinition(
            key="BULK_SENDER_SESSIONS_PATH", group="platform", python_type=str, default="/data/sessions/bulk_sender",
            min_val=None, max_val=None, step=None, live=False,
            description="Bulk sender session directory path",
            requires_restart=True, cli_flag="--bulk-sender-sessions-path",
            bootstrap_only=True,
        ),
        SettingDefinition(
            key="LOGIN_BOT_ID", group="platform", python_type=str, default="",
            min_val=None, max_val=None, step=None, live=False,
            description="Optional login bot ID",
            requires_restart=True, cli_flag="--login-bot-id",
        ),
    ],
    "collector": [
        SettingDefinition(
            key="RUN_MODE", group="collector", python_type=str, default="both",
            min_val=None, max_val=None, step=None, live=False,
            description="Service run mode: realtime, backfill, or both",
            requires_restart=True, cli_flag="--run-mode",
            choices=["both", "realtime", "backfill"],
        ),
        SettingDefinition(
            key="STORY_SCAN_ENABLED", group="collector", python_type=bool, default=True,
            min_val=None, max_val=None, step=None, live=True,
            description="Enable story scanning",
            requires_restart=False, cli_flag="--story-scan-enabled",
        ),
        SettingDefinition(
            key="STORY_SCAN_INTERVAL", group="collector", python_type=int, default=300,
            min_val=60, max_val=None, step=None, live=False,
            description="Story scan interval in seconds",
            requires_restart=True, cli_flag="--story-scan-interval",
        ),
        SettingDefinition(
            key="STORY_PRIORITY_BOOST", group="collector", python_type=int, default=10,
            min_val=0, max_val=100, step=None, live=False,
            description="Priority boost for story messages",
            requires_restart=True, cli_flag="--story-priority-boost",
        ),
        SettingDefinition(
            key="COLLECTOR_BACKFILL_ENABLED", group="collector", python_type=bool, default=True,
            min_val=None, max_val=None, step=None, live=False,
            description="Enable backfill mode",
            requires_restart=True, cli_flag="--collector-backfill-enabled",
        ),
        SettingDefinition(
            key="COLLECTOR_BACKFILL_MSG_PER_SEC", group="collector", python_type=float, default=20.0,
            min_val=0.1, max_val=200.0, step=0.5, live=False,
            description="Backfill messages per second rate limit",
            requires_restart=True, cli_flag="--collector-backfill-msg-per-sec",
        ),
        SettingDefinition(
            key="COLLECTOR_BACKFILL_CHAT_DELAY", group="collector", python_type=float, default=2.0,
            min_val=0.0, max_val=None, step=0.1, live=False,
            description="Delay between chats in backfill mode (seconds)",
            requires_restart=True, cli_flag="--collector-backfill-chat-delay",
        ),
        SettingDefinition(
            key="COLLECTOR_MEMBER_FETCH_DELAY", group="collector", python_type=float, default=0.5,
            min_val=0.0, max_val=60.0, step=0.1, live=False,
            description="Delay between member fetch pages (seconds)",
            requires_restart=True, cli_flag="--collector-member-fetch-delay",
        ),
        SettingDefinition(
            key="COLLECTOR_MEMBER_FETCH_MAX_PER_HOUR", group="collector", python_type=int, default=200,
            min_val=0, max_val=100000, step=None, live=False,
            description="Max member fetch operations per hour",
            requires_restart=True, cli_flag="--collector-member-fetch-max-per-hour",
        ),
        SettingDefinition(
            key="COLLECTOR_BACKFILL_BATCH_SIZE", group="collector", python_type=int, default=100,
            min_val=1, max_val=1000, step=None, live=False,
            description="Batch size for backfill operations",
            requires_restart=True, cli_flag="--collector-backfill-batch-size",
        ),
        SettingDefinition(
            key="COLLECTOR_BACKFILL_POLL_INTERVAL", group="collector", python_type=int, default=30,
            min_val=5, max_val=None, step=None, live=False,
            description="Backfill poll interval in seconds",
            requires_restart=True, cli_flag="--collector-backfill-poll-interval",
        ),
        SettingDefinition(
            key="COLLECTOR_MEDIA_WORKER_COUNT", group="collector", python_type=int, default=4,
            min_val=1, max_val=32, step=None, live=False,
            description="Number of media download workers",
            requires_restart=True, cli_flag="--collector-media-worker-count",
        ),
        SettingDefinition(
            key="COLLECTOR_MAX_MEDIA_SIZE_MB", group="collector", python_type=int, default=50,
            min_val=1, max_val=2000, step=None, live=False,
            description="Max media file size in MB",
            requires_restart=True, cli_flag="--collector-max-media-size-mb",
        ),
        SettingDefinition(
            key="COLLECTOR_GROUP_MANAGER_POLL_INTERVAL", group="collector", python_type=int, default=60,
            min_val=60, max_val=None, step=None, live=False,
            description="Group manager poll interval in seconds",
            requires_restart=True, cli_flag="--collector-group-manager-poll-interval",
        ),
        SettingDefinition(
            key="COLLECTOR_ADMIN_LOG_POLL_INTERVAL", group="collector", python_type=int, default=300,
            min_val=10, max_val=None, step=None, live=False,
            description="Admin log poll interval in seconds",
            requires_restart=True, cli_flag="--collector-admin-log-poll-interval",
        ),
        SettingDefinition(
            key="COLLECTOR_STORY_SCAN_INTERVAL", group="collector", python_type=int, default=600,
            min_val=60, max_val=None, step=None, live=False,
            description="Collector story scan interval in seconds",
            requires_restart=True, cli_flag="--collector-story-scan-interval",
        ),
        SettingDefinition(
            key="COLLECTOR_STORY_EXPIRY_BUFFER", group="collector", python_type=int, default=60,
            min_val=0, max_val=None, step=None, live=False,
            description="Story expiry buffer in seconds",
            requires_restart=True, cli_flag="--collector-story-expiry-buffer",
        ),
        SettingDefinition(
            key="ACCOUNT_SCHEDULE_ENABLED", group="collector", python_type=bool, default=False,
            min_val=None, max_val=None, step=None, live=False,
            description="Enable account scheduling",
            requires_restart=True, cli_flag="--account-schedule-enabled",
        ),
        SettingDefinition(
            key="ACCOUNT_ACTIVE_START", group="collector", python_type=str, default="00:00",
            min_val=None, max_val=None, step=None, live=False,
            description="Account active start time (HH:MM)",
            requires_restart=True, cli_flag="--account-active-start",
        ),
        SettingDefinition(
            key="ACCOUNT_ACTIVE_END", group="collector", python_type=str, default="24:00",
            min_val=None, max_val=None, step=None, live=False,
            description="Account active end time (HH:MM)",
            requires_restart=True, cli_flag="--account-active-end",
        ),
    ],
    "face_recognition": [
        SettingDefinition(
            key="SIMILARITY_THRESHOLD", group="face_recognition", python_type=float, default=0.55,
            min_val=0.0, max_val=1.0, step=0.01, live=False,
            description="Legacy similarity threshold (alias)",
            requires_restart=True, cli_flag="--similarity-threshold",
        ),
        SettingDefinition(
            key="MIN_QUALITY_THRESHOLD", group="face_recognition", python_type=float, default=0.67,
            min_val=0.0, max_val=1.0, step=0.01, live=False,
            description="Legacy minimum face quality threshold (alias)",
            requires_restart=True, cli_flag="--min-quality-threshold",
        ),
        SettingDefinition(
            key="FACE_PROCESSING_ENABLED", group="face_recognition", python_type=bool, default=False,
            min_val=None, max_val=None, step=None, live=True,
            description="Enable face processing",
            requires_restart=False, cli_flag="--face-processing-enabled",
        ),
        SettingDefinition(
            key="FACE_SIMILARITY_THRESHOLD", group="face_recognition", python_type=float, default=0.55,
            min_val=0.0, max_val=1.0, step=0.01, live=True,
            description="Face similarity threshold",
            requires_restart=False, cli_flag="--face-similarity-threshold",
        ),
        SettingDefinition(
            key="FACE_MIN_QUALITY_THRESHOLD", group="face_recognition", python_type=float, default=0.67,
            min_val=0.0, max_val=1.0, step=0.01, live=True,
            description="Minimum face quality threshold",
            requires_restart=False, cli_flag="--face-min-quality-threshold",
        ),
        SettingDefinition(
            key="FACE_BATCH_SIZE", group="face_recognition", python_type=int, default=10,
            min_val=1, max_val=500, step=None, live=False,
            description="Face processing batch size",
            requires_restart=True, cli_flag="--face-batch-size",
        ),
        SettingDefinition(
            key="FACE_VIDEO_MAX_FRAMES", group="face_recognition", python_type=int, default=10,
            min_val=1, max_val=100, step=None, live=False,
            description="Max frames to extract from video",
            requires_restart=True, cli_flag="--face-video-max-frames",
        ),
        SettingDefinition(
            key="FACE_CIRCLE_VIDEO_FPS", group="face_recognition", python_type=float, default=2.0,
            min_val=1, max_val=30, step=None, live=False,
            description="FPS for circle video processing",
            requires_restart=True, cli_flag="--face-circle-video-fps",
        ),
        SettingDefinition(
            key="FACE_POLL_INTERVAL", group="face_recognition", python_type=int, default=5,
            min_val=1, max_val=None, step=None, live=False,
            description="Face processor poll interval in seconds",
            requires_restart=True, cli_flag="--face-poll-interval",
        ),
    ],
    "user_intelligence": [
        SettingDefinition(
            key="USER_INTEL_PROCESSING_ENABLED", group="user_intelligence", python_type=bool, default=True,
            min_val=None, max_val=None, step=None, live=True,
            description="Enable user intelligence processing",
            requires_restart=False, cli_flag="--user-intel-processing-enabled",
        ),
        SettingDefinition(
            key="USER_INTEL_NETWORK_ENABLED", group="user_intelligence", python_type=bool, default=True,
            min_val=None, max_val=None, step=None, live=True,
            description="Enable network graph building",
            requires_restart=False, cli_flag="--user-intel-network-enabled",
        ),
        SettingDefinition(
            key="USER_INTEL_BATCH_SIZE", group="user_intelligence", python_type=int, default=100,
            min_val=1, max_val=1000, step=None, live=False,
            description="User intelligence batch size",
            requires_restart=True, cli_flag="--user-intel-batch-size",
        ),
        SettingDefinition(
            key="USER_INTEL_POLL_INTERVAL", group="user_intelligence", python_type=int, default=5,
            min_val=1, max_val=None, step=None, live=False,
            description="User intelligence poll interval in seconds",
            requires_restart=True, cli_flag="--user-intel-poll-interval",
        ),
    ],
    "link_discovery": [
        SettingDefinition(
            key="LINK_DISCOVERY_PROCESSING_ENABLED", group="link_discovery", python_type=bool, default=True,
            min_val=None, max_val=None, step=None, live=True,
            description="Enable link discovery processing",
            requires_restart=False, cli_flag="--link-discovery-processing-enabled",
        ),
        SettingDefinition(
            key="LINK_DISCOVERY_BATCH_SIZE", group="link_discovery", python_type=int, default=100,
            min_val=1, max_val=1000, step=None, live=False,
            description="Link discovery batch size",
            requires_restart=True, cli_flag="--link-discovery-batch-size",
        ),
        SettingDefinition(
            key="LINK_DISCOVERY_POLL_INTERVAL", group="link_discovery", python_type=int, default=5,
            min_val=1, max_val=None, step=None, live=False,
            description="Link discovery poll interval in seconds",
            requires_restart=True, cli_flag="--link-discovery-poll-interval",
        ),
        SettingDefinition(
            key="LINK_DISCOVERY_RESOLVE_METADATA", group="link_discovery", python_type=bool, default=False,
            min_val=None, max_val=None, step=None, live=False,
            description="Resolve link metadata",
            requires_restart=True, cli_flag="--link-discovery-resolve-metadata",
        ),
        SettingDefinition(
            key="LINK_DISCOVERY_RESOLVE_RATE_LIMIT", group="link_discovery", python_type=int, default=10,
            min_val=1, max_val=1000, step=None, live=False,
            description="Link resolution rate limit (requests/min)",
            requires_restart=True, cli_flag="--link-discovery-resolve-rate-limit",
        ),
    ],
    "bulk_sender": [
        SettingDefinition(
            key="BULK_SENDER_SEND_DELAY", group="bulk_sender", python_type=float, default=1.5,
            min_val=0.0, max_val=60.0, step=0.1, live=False,
            description="Delay between sends in seconds",
            requires_restart=True, cli_flag="--bulk-sender-send-delay",
        ),
        SettingDefinition(
            key="BULK_SENDER_MAX_RETRIES", group="bulk_sender", python_type=int, default=3,
            min_val=0, max_val=20, step=None, live=False,
            description="Maximum send retries",
            requires_restart=True, cli_flag="--bulk-sender-max-retries",
        ),
    ],
    "shared": [
        SettingDefinition(
            key="WORKER_TASK_TIMEOUT", group="shared", python_type=int, default=300,
            min_val=1, max_val=None, step=None, live=False,
            description="Task timeout for worker processing (seconds)",
            requires_restart=True, cli_flag="--worker-task-timeout",
        ),
        SettingDefinition(
            key="NUM_WORKERS", group="shared", python_type=int, default=6,
            min_val=1, max_val=32, step=None, live=False,
            description="Number of worker processes",
            requires_restart=True, cli_flag="--num-workers",
        ),
        SettingDefinition(
            key="MAX_WORKERS", group="shared", python_type=int, default=10,
            min_val=1, max_val=64, step=None, live=False,
            description="Maximum worker processes",
            requires_restart=True, cli_flag="--max-workers",
        ),
        SettingDefinition(
            key="QUEUE_MAX_SIZE", group="shared", python_type=int, default=4000,
            min_val=10, max_val=None, step=None, live=False,
            description="Maximum queue size",
            requires_restart=True, cli_flag="--queue-max-size",
        ),
        SettingDefinition(
            key="HEALTH_CHECK_INTERVAL", group="shared", python_type=int, default=300,
            min_val=5, max_val=None, step=None, live=False,
            description="Health check interval in seconds",
            requires_restart=True, cli_flag="--health-check-interval",
        ),
        SettingDefinition(
            key="SIGTERM_DRAIN_TIMEOUT", group="shared", python_type=int, default=30,
            min_val=0, max_val=None, step=None, live=False,
            description="SIGTERM drain timeout in seconds",
            requires_restart=True, cli_flag="--sigterm-drain-timeout",
        ),
        SettingDefinition(
            key="REDIS_RECONNECT_INTERVAL", group="shared", python_type=int, default=30,
            min_val=1, max_val=None, step=None, live=False,
            description="Redis reconnect interval in seconds",
            requires_restart=True, cli_flag="--redis-reconnect-interval",
        ),
        SettingDefinition(
            key="REDIS_RECONNECT_MAX_ATTEMPTS", group="shared", python_type=int, default=0,
            min_val=0, max_val=None, step=None, live=False,
            description="Max Redis reconnect attempts",
            requires_restart=True, cli_flag="--redis-reconnect-max-attempts",
        ),
        SettingDefinition(
            key="CIRCUIT_BREAKER_THRESHOLD", group="shared", python_type=int, default=5,
            min_val=1, max_val=None, step=None, live=False,
            description="Failures before opening circuit breaker",
            requires_restart=True, cli_flag="--circuit-breaker-threshold",
        ),
        SettingDefinition(
            key="CIRCUIT_BREAKER_TIMEOUT", group="shared", python_type=int, default=60,
            min_val=1, max_val=None, step=None, live=False,
            description="Circuit breaker reset timeout (seconds)",
            requires_restart=True, cli_flag="--circuit-breaker-timeout",
        ),
        SettingDefinition(
            key="MAX_RETRY_ATTEMPTS", group="shared", python_type=int, default=3,
            min_val=0, max_val=None, step=None, live=False,
            description="Max retry attempts for transient failures",
            requires_restart=True, cli_flag="--max-retry-attempts",
        ),
        SettingDefinition(
            key="RETRY_BASE_DELAY", group="shared", python_type=float, default=1.0,
            min_val=0.0, max_val=None, step=0.1, live=False,
            description="Base retry delay in seconds",
            requires_restart=True, cli_flag="--retry-base-delay",
        ),
        SettingDefinition(
            key="SESSION_ROTATION_ENABLED", group="shared", python_type=bool, default=True,
            min_val=None, max_val=None, step=None, live=False,
            description="Enable session rotation",
            requires_restart=True, cli_flag="--session-rotation-enabled",
        ),
        SettingDefinition(
            key="ENABLE_MTPROTO_RESET", group="shared", python_type=bool, default=False,
            min_val=None, max_val=None, step=None, live=False,
            description="Enable MTProto reset",
            requires_restart=True, cli_flag="--enable-mtproto-reset",
        ),
        SettingDefinition(
            key="ENABLE_SESSION_LOCK", group="shared", python_type=bool, default=True,
            min_val=None, max_val=None, step=None, live=False,
            description="Enable session locking",
            requires_restart=True, cli_flag="--enable-session-lock",
        ),
        SettingDefinition(
            key="MTPROTO_RESET_NEW_SESSIONS_ONLY", group="shared", python_type=bool, default=True,
            min_val=None, max_val=None, step=None, live=False,
            description="Reset MTProto for new sessions only",
            requires_restart=True, cli_flag="--mtproto-reset-new-sessions-only",
        ),
        SettingDefinition(
            key="SCALE_UP_SUSTAINED_SECONDS", group="shared", python_type=int, default=60,
            min_val=10, max_val=None, step=None, live=False,
            description="Seconds of sustained load before scale up",
            requires_restart=True, cli_flag="--scale-up-sustained-seconds",
        ),
        SettingDefinition(
            key="SCALE_DOWN_SUSTAINED_SECONDS", group="shared", python_type=int, default=120,
            min_val=10, max_val=None, step=None, live=False,
            description="Seconds of low load before scale down",
            requires_restart=True, cli_flag="--scale-down-sustained-seconds",
        ),
        SettingDefinition(
            key="AUTOSCALER_POLL_INTERVAL", group="shared", python_type=int, default=15,
            min_val=1, max_val=None, step=None, live=False,
            description="Autoscaler poll interval in seconds",
            requires_restart=True, cli_flag="--autoscaler-poll-interval",
        ),
        SettingDefinition(
            key="STARTUP_PROBE_MAX_ATTEMPTS", group="shared", python_type=int, default=30,
            min_val=1, max_val=None, step=None, live=False,
            description="Max startup probe attempts",
            requires_restart=True, cli_flag="--startup-probe-max-attempts",
        ),
        SettingDefinition(
            key="STARTUP_PROBE_RETRY_INTERVAL", group="shared", python_type=float, default=2.0,
            min_val=1, max_val=None, step=None, live=False,
            description="Startup probe retry interval in seconds",
            requires_restart=True, cli_flag="--startup-probe-retry-interval",
        ),
        SettingDefinition(
            key="DASHBOARD_PORT_SEARCH_RANGE", group="shared", python_type=int, default=20,
            min_val=1, max_val=100, step=None, live=False,
            description="Port search range for dashboards",
            requires_restart=True, cli_flag="--dashboard-port-search-range",
        ),
        SettingDefinition(
            key="MAX_MEDIA_SIZE_MB", group="shared", python_type=int, default=50,
            min_val=1, max_val=2000, step=None, live=False,
            description="Max media size in MB",
            requires_restart=True, cli_flag="--max-media-size-mb",
        ),
        SettingDefinition(
            key="REALTIME_QUEUE_MAX", group="shared", python_type=int, default=1000,
            min_val=1, max_val=None, step=None, live=False,
            description="Max realtime queue size",
            requires_restart=True, cli_flag="--realtime-queue-max",
        ),
        SettingDefinition(
            key="USE_GPU", group="shared", python_type=bool, default=True,
            min_val=None, max_val=None, step=None, live=False,
            description="Use GPU for processing",
            requires_restart=True, cli_flag="--use-gpu",
        ),
        SettingDefinition(
            key="REALTIME_BATCH_SIZE", group="shared", python_type=int, default=10,
            min_val=1, max_val=1000, step=None, live=False,
            description="Realtime processing batch size",
            requires_restart=True, cli_flag="--realtime-batch-size",
        ),
        SettingDefinition(
            key="REALTIME_BATCH_INTERVAL", group="shared", python_type=float, default=1.0,
            min_val=0.01, max_val=60.0, step=0.1, live=False,
            description="Realtime batch interval in seconds",
            requires_restart=True, cli_flag="--realtime-batch-interval",
        ),
        SettingDefinition(
            key="ENABLE_PROMETHEUS", group="shared", python_type=bool, default=True,
            min_val=None, max_val=None, step=None, live=False,
            description="Enable Prometheus metrics",
            requires_restart=True, cli_flag="--enable-prometheus",
        ),
        SettingDefinition(
            key="PROMETHEUS_PORT", group="shared", python_type=int, default=8000,
            min_val=1, max_val=65535, step=None, live=False,
            description="Prometheus metrics port",
            requires_restart=True, cli_flag="--prometheus-port",
        ),
        SettingDefinition(
            key="LOG_FORMAT", group="shared", python_type=str, default="json",
            min_val=None, max_val=None, step=None, live=False,
            description="Log format: json or text",
            requires_restart=True, cli_flag="--log-format",
            choices=["json", "text"],
        ),
        SettingDefinition(
            key="HUB_NOTIFY_BATCH_INTERVAL", group="shared", python_type=int, default=30,
            min_val=1, max_val=3600, step=None, live=False,
            description="Hub notification batch interval in seconds",
            requires_restart=True, cli_flag="--hub-notify-batch-interval",
        ),
        SettingDefinition(
            key="HUB_NOTIFY_RATE_LIMIT", group="shared", python_type=int, default=100,
            min_val=1, max_val=None, step=None, live=False,
            description="Hub notification rate limit per second",
            requires_restart=True, cli_flag="--hub-notify-rate-limit",
        ),
        SettingDefinition(
            key="NOTIFY_ON_NEW_IDENTITY", group="shared", python_type=bool, default=True,
            min_val=None, max_val=None, step=None, live=False,
            description="Notify on new identity creation",
            requires_restart=True, cli_flag="--notify-on-new-identity",
        ),
        SettingDefinition(
            key="NOTIFY_ON_SCAN_MILESTONE", group="shared", python_type=bool, default=True,
            min_val=None, max_val=None, step=None, live=False,
            description="Notify on scan milestone events",
            requires_restart=True, cli_flag="--notify-on-scan-milestone",
        ),
        SettingDefinition(
            key="NOTIFY_MILESTONE_INTERVAL", group="shared", python_type=int, default=500,
            min_val=1, max_val=None, step=None, live=False,
            description="Messages between milestone notifications",
            requires_restart=True, cli_flag="--notify-milestone-interval",
        ),
        SettingDefinition(
            key="CLEANUP_INTERVAL", group="shared", python_type=int, default=3600,
            min_val=1, max_val=None, step=None, live=False,
            description="Cleanup scheduler interval in seconds",
            requires_restart=True, cli_flag="--cleanup-interval",
        ),
        SettingDefinition(
            key="GENERAL_TOPIC_RETENTION_HOURS", group="shared", python_type=int, default=12,
            min_val=1, max_val=None, step=None, live=False,
            description="General topic retention duration (hours)",
            requires_restart=True, cli_flag="--general-topic-retention-hours",
        ),
    ],
}


def all_setting_definitions() -> list[SettingDefinition]:
    """Returns all setting definitions across groups in declaration order."""
    return [defn for definitions in SETTING_GROUPS.values() for defn in definitions]


SETTING_BY_KEY: dict[str, SettingDefinition] = {
    defn.key: defn for defn in all_setting_definitions()
}


# ---------------------------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------------------------

class ConfigManager:
    """Centralises config read/write and CLI-to-env bridging logic."""

    _SECRET_PATTERN = re.compile(r"PASSWORD|TOKEN|HASH|SECRET|KEY", re.IGNORECASE)

    def __init__(self, env_path: str = ".env") -> None:
        self.env_path = env_path
        if not os.path.exists(env_path):
            log_fn = logging.warning if env_path == ".env" else logging.debug
            log_fn("ConfigManager: .env file not found at %s", env_path)

    # ------------------------------------------------------------------
    # Task 2.1 — parse / read
    # ------------------------------------------------------------------

    def _parse_env_file(self) -> list[EnvLine]:
        """Parse .env into EnvLine list, preserving comments and blank lines."""
        lines: list[EnvLine] = []
        try:
            with open(self.env_path, encoding="utf-8") as f:
                for raw in f:
                    raw = raw.rstrip("\r\n")
                    stripped = raw.strip()
                    if not stripped or stripped.startswith("#"):
                        lines.append(EnvLine(raw=raw, key=None, value=None))
                    elif "=" in stripped:
                        k, _, v = raw.partition("=")
                        lines.append(EnvLine(raw=raw, key=k.strip(), value=v))
                    else:
                        lines.append(EnvLine(raw=raw, key=None, value=None))
        except FileNotFoundError:
            pass
        return lines

    def _read_env_only(self, key: str) -> str | None:
        """Return key from .env only (without DB lookup)."""
        for line in self._parse_env_file():
            if line.key == key:
                return line.value
        return None

    def read_env(self, key: str) -> str | None:
        """Return raw string value for key from config store, then .env fallback."""
        # Preserve hermetic tests that use temp env files by skipping DB access.
        if self.env_path == ".env":
            db_value = config_store.get_setting(key)
            if db_value is not None:
                return db_value
        return self._read_env_only(key)

    # ------------------------------------------------------------------
    # Task 2.2 + 2.3 — atomic write + optional Redis push
    # ------------------------------------------------------------------

    def write_setting(
        self,
        key: str,
        value: Any,
        live: bool = False,
        changed_by: str | None = None,
        source: str = "dashboard",
    ) -> None:
        """Persist config setting (DB primary, .env fallback) and optionally push to Redis."""
        str_value = str(value)

        definition = SETTING_BY_KEY.get(key)
        if self.env_path == ".env":
            sensitive = definition.sensitive if definition else bool(self._SECRET_PATTERN.search(key))
            persist_result = config_store.persist_setting(
                key=key,
                value=str_value,
                group=definition.group if definition else "unmapped",
                sensitive=sensitive,
                changed_by=changed_by or os.getenv("DASHBOARD_OPERATOR") or "dashboard",
                source=source,
                live_applied=live,
                restart_required=definition.requires_restart if definition else (not live),
                owners=definition.owners if definition else tuple(),
            )
            if not persist_result.persisted:
                logging.warning(
                    "ConfigManager: DB persist failed for %s (fallback to .env): %s",
                    key,
                    persist_result.error,
                )

        lines = self._parse_env_file()
        found = False
        for i, line in enumerate(lines):
            if line.key == key:
                lines[i] = EnvLine(raw=f"{key}={str_value}", key=key, value=str_value)
                found = True
                break
        if not found:
            lines.append(EnvLine(raw=f"{key}={str_value}", key=key, value=str_value))

        tmp_path = self.env_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write("\n".join(line.raw for line in lines))
                if lines:
                    f.write("\n")
            os.replace(tmp_path, self.env_path)
        except OSError as exc:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise IOError(f"Cannot write .env: {exc}") from exc

        if not self._SECRET_PATTERN.search(key):
            logging.debug("ConfigManager: wrote %s=%s", key, str_value)
        else:
            logging.debug("ConfigManager: wrote %s=<redacted>", key)

        # Task 2.3 — Redis push for live settings
        if live:
            try:
                from shared.config import set_dynamic_setting
                set_dynamic_setting(key, str_value)
            except Exception:
                pass  # Redis unavailable — .env write already succeeded

    # ------------------------------------------------------------------
    # Task 2.4 — CLI overrides
    # ------------------------------------------------------------------

    def apply_cli_overrides(
        self,
        args: argparse.Namespace,
        mapping: dict[str, str],
    ) -> None:
        """Bridge parsed CLI args into os.environ for pydantic to pick up."""
        for attr_name, env_key in mapping.items():
            value = getattr(args, attr_name, None)
            if value is not None:
                os.environ[env_key] = str(value)

    # ------------------------------------------------------------------
    # Task 2.5 — list_settings
    # ------------------------------------------------------------------

    def list_settings(self, group: str) -> dict[str, Any]:
        """Return {key: current_value} for all settings in group."""
        if group not in SETTING_GROUPS:
            raise KeyError(f"Unknown setting group: {group!r}")

        definitions = SETTING_GROUPS[group]
        db_snapshot: dict[str, str] = {}
        if self.env_path == ".env":
            db_snapshot = config_store.get_settings_snapshot([d.key for d in definitions])

        result: dict[str, Any] = {}
        for defn in definitions:
            val = db_snapshot.get(defn.key)
            if val is None:
                val = self._read_env_only(defn.key)
            result[defn.key] = val if val is not None else defn.default
        return result


# ---------------------------------------------------------------------------
# Task 2.6 — module-level helpers + singleton
# ---------------------------------------------------------------------------

def _bool_flag(value: str) -> bool:
    """Convert CLI string to bool."""
    return value.lower() in ("true", "1", "yes", "on")


def _build_verbose_help(defn: "SettingDefinition") -> str:
    """Build a rich help string for the ? tooltip from a SettingDefinition."""
    parts = [defn.description]

    if defn.min_val is not None and defn.max_val is not None:
        parts.append(f"Range: {defn.min_val} – {defn.max_val}")
    elif defn.min_val is not None:
        parts.append(f"Minimum: {defn.min_val}")
    elif defn.max_val is not None:
        parts.append(f"Maximum: {defn.max_val}")

    if defn.step is not None:
        parts.append(f"Step: {defn.step}")

    if defn.choices:
        parts.append(f"Allowed values: {', '.join(defn.choices)}")

    if defn.live:
        parts.append("✅ Live — takes effect immediately without restarting the service.")
    else:
        parts.append("🔄 Restart required — save the value then restart the service container for it to take effect.")

    if defn.bootstrap_only:
        parts.append("⚙️ Bootstrap setting — only needs to be set once during initial setup. Changing it later requires a full service restart.")

    if defn.sensitive:
        parts.append("🔒 Sensitive — this value is stored encrypted and masked in the UI.")

    parts.append(f"Env var: {defn.key}")
    parts.append(f"CLI flag: {defn.cli_flag}")

    return "\n\n".join(parts)


def render_config_panel(group: str, live_keys: set[str]) -> None:
    """Render a 2-column Streamlit config panel for all settings in group."""
    import streamlit as st  # local import so non-dashboard code doesn't require streamlit

    definitions = SETTING_GROUPS.get(group, [])
    for i in range(0, len(definitions), 2):
        pair = definitions[i : i + 2]
        cols = st.columns(2)
        for col, defn in zip(cols, pair):
            with col:
                current_raw = config_manager.read_env(defn.key)
                is_live = defn.key in live_keys
                caption_parts = ["🟢 live" if is_live else "🔄 restart required"]
                if defn.bootstrap_only:
                    caption_parts.append("⚙️ bootstrap")
                if defn.sensitive:
                    caption_parts.append("🔒 secret")
                caption = " · ".join(caption_parts)

                verbose_help = _build_verbose_help(defn)

                try:
                    if defn.python_type is bool:
                        current_val = (
                            current_raw.lower() in ("true", "1", "yes", "on")
                            if current_raw is not None
                            else bool(defn.default)
                        )
                        new_val = st.toggle(defn.key, value=current_val, help=verbose_help)
                    elif defn.python_type is int:
                        raw_int = int(current_raw) if current_raw is not None else int(defn.default)
                        # Clamp to min_val so Streamlit doesn't raise a validation error
                        # (can happen for bootstrap fields whose default is 0 but min_val is 1)
                        min_int = int(defn.min_val) if defn.min_val is not None else None
                        max_int = int(defn.max_val) if defn.max_val is not None else None
                        if min_int is not None and raw_int < min_int:
                            raw_int = min_int
                        if max_int is not None and raw_int > max_int:
                            raw_int = max_int
                        kwargs: dict[str, Any] = {"value": raw_int, "step": 1, "help": verbose_help}
                        if min_int is not None:
                            kwargs["min_value"] = min_int
                        if max_int is not None:
                            kwargs["max_value"] = max_int
                        new_val = st.number_input(defn.key, **kwargs)
                    elif defn.python_type is float:
                        raw_float = float(current_raw) if current_raw is not None else float(defn.default)
                        min_float = float(defn.min_val) if defn.min_val is not None else None
                        max_float = float(defn.max_val) if defn.max_val is not None else None
                        if min_float is not None and raw_float < min_float:
                            raw_float = min_float
                        if max_float is not None and raw_float > max_float:
                            raw_float = max_float
                        kwargs = {"value": raw_float, "step": defn.step or 0.1, "format": "%.3f", "help": verbose_help}
                        if min_float is not None:
                            kwargs["min_value"] = min_float
                        if max_float is not None:
                            kwargs["max_value"] = max_float
                        new_val = st.number_input(defn.key, **kwargs)
                    else:
                        current_val = current_raw if current_raw is not None else str(defn.default)
                        if defn.choices is not None:
                            try:
                                idx = defn.choices.index(current_val)
                            except ValueError:
                                idx = 0
                            new_val = st.selectbox(
                                defn.key,
                                options=defn.choices,
                                index=idx,
                                help=verbose_help,
                            )
                        else:
                            kwargs: dict[str, Any] = {"value": current_val, "help": verbose_help}
                            if defn.sensitive:
                                kwargs["type"] = "password"
                            new_val = st.text_input(defn.key, **kwargs)

                    if st.button("Save", key=f"save_{defn.key}"):
                        try:
                            config_manager.write_setting(defn.key, new_val, live=is_live)
                            if is_live:
                                st.success("✅ Applied immediately.")
                            else:
                                st.info("✅ Saved. Restart service to apply.")
                        except IOError as exc:
                            st.error(f"Failed to save: {exc}. Check file permissions.")

                    st.caption(caption)
                except Exception as exc:
                    st.error(f"Error rendering {defn.key}: {exc}")


# Module-level singleton
config_manager = ConfigManager()
