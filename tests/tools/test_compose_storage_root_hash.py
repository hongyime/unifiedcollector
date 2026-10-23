"""Compose hash guard for the reconciled storage-root state.

The pre-recovery dirty default
``${UNIFIEDCOLLECTOR_HOST_ROOT:-/media/sg-ultra/unifiedcollector}`` replaced
``${UNIFIEDCOLLECTOR_HOST_ROOT:-/media/seagate-expansion/unifiedcollector}``
across every volume mount. Task 0 froze that dirty file as authoritative
(``task-0-source-manifest.tsv`` row for ``docker/docker-compose.yml``; the
rendered ``compose_sha256`` identity in ``task-0-artifact-identity.json``
covers the same state), and the recovery plan reconciles it as intended.

Any further compose byte change -- commands, ports, healthchecks, an unknown
storage root -- must fail here loudly instead of drifting silently.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[2] / "docker" / "docker-compose.yml"

# task-0-source-manifest.tsv: sha256 of docker/docker-compose.yml at freeze
# time (HEAD fc5c25b8 plus the seven staged pre-recovery files, which include
# the intended seagate-expansion -> sg-ultra storage-root default).
FROZEN_COMPOSE_SHA256 = "13df5a3d172c089ffbc4a2dea4d4bfeb977108039bf13a3f6ed73e0bacf4ca0e"

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
