"""Compose hash guard for the reconciled storage-root state.

The pre-recovery dirty default
``${UNIFIEDCOLLECTOR_HOST_ROOT:-/media/sg-ultra/unifiedcollector}`` replaced
``${UNIFIEDCOLLECTOR_HOST_ROOT:-/media/seagate-expansion/unifiedcollector}``
across every volume mount. Task 0 froze that dirty file as authoritative
(``task-0-source-manifest.tsv`` row for ``docker/docker-compose.yml``; the
rendered ``compose_sha256`` identity in ``task-0-artifact-identity.json``
covers the same state), and the recovery plan reconciles it as intended.

The task-30 public-only guard, task-31 module policy, task-34 meaningful
healthcheck, task-35 private evidence mount/resource limits, task-37
fail-closed admission-policy/state mounts, and task-42 fail-closed internal
secret/dashboard bindings are part of the
reviewed Compose state. Any further compose byte change -- commands, ports,
healthchecks, or an unknown storage root -- must fail here loudly instead of
drifting silently.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[2] / "docker" / "docker-compose.yml"

# Reviewed Compose state after task-42 security hardening, Task 41's one-shot
# schema-init gate, Task 43's read-only cutover pause marker for watchdog, and
# the WhatsApp-only durable raw-payload spool on the existing sessions volume.
FROZEN_COMPOSE_SHA256 = "983dff771dbba9e6a60b194009936dad526603c427c92ec28702f98d7ebb1dc3"

# The reconciled intended storage root (task 29).
INTENDED_ROOT = "/media/sg-ultra/unifiedcollector"
_ROOT_DEFAULT = re.compile(r"UNIFIEDCOLLECTOR_HOST_ROOT:-([^}]+)\}")


def _compose_sha256() -> str:
    return hashlib.sha256(COMPOSE.read_bytes()).hexdigest()


def test_compose_file_matches_the_reconciled_frozen_hash() -> None:
    assert _compose_sha256() == FROZEN_COMPOSE_SHA256


def test_compose_default_storage_root_is_the_intended_sg_ultra_root() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "seagate-expansion" not in text
    defaults = _ROOT_DEFAULT.findall(text)
    assert defaults, "UNIFIEDCOLLECTOR_HOST_ROOT default missing from compose"
    assert set(defaults) == {INTENDED_ROOT}


def test_whatsapp_history_sync_is_paused_without_disabling_realtime_sync() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert text.count('SYNC_FULL_HISTORY: "false"') == 2
    assert text.count('WHATSAPP_DEEP_BACKFILL: "false"') == 2
