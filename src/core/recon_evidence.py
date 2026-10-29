"""Owner-only storage and hard capacity checks for raw recon evidence."""
from __future__ import annotations

import os
import secrets
import shutil
import stat
import tempfile
from pathlib import Path

DEFAULT_EVIDENCE_ROOT = Path("/var/lib/unifiedcollector/recon-evidence")
DEFAULT_MIN_FREE_BYTES = 2 * 1024**3
DEFAULT_MAX_EVIDENCE_BYTES = 10 * 1024**3


class EvidenceCapacityError(RuntimeError):
    """Evidence storage cannot safely accept more raw output."""


def evidence_root() -> Path:
    """Create and verify the configured private evidence root."""
    root = Path(os.getenv("RECON_EVIDENCE_ROOT", str(DEFAULT_EVIDENCE_ROOT)))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise EvidenceCapacityError("recon evidence root must be a real directory")
    root.chmod(0o700)
    if stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise EvidenceCapacityError("recon evidence root must have mode 700")
    return root


def _tree_bytes(root: Path) -> int:
    total = 0
    for item in root.rglob("*"):
        if item.is_symlink():
            continue
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def ensure_evidence_capacity(
    root: Path | None = None,
    *,
    additional_bytes: int = 0,
    min_free_bytes: int | None = None,
    max_evidence_bytes: int | None = None,
) -> Path:
    """Fail closed on low filesystem headroom or a cumulative evidence cap."""
    if additional_bytes < 0:
        raise ValueError("additional_bytes cannot be negative")
    selected = root or evidence_root()
    selected.mkdir(mode=0o700, parents=True, exist_ok=True)
    if selected.is_symlink() or not selected.is_dir():
        raise EvidenceCapacityError("recon evidence root must be a real directory")
    selected.chmod(0o700)
    if stat.S_IMODE(selected.stat().st_mode) != 0o700:
        raise EvidenceCapacityError("recon evidence root must have mode 700")
    free_floor = min_free_bytes
    if free_floor is None:
        configured_floor = int(
            os.getenv("RECON_MIN_DISK_FREE_BYTES", str(DEFAULT_MIN_FREE_BYTES))
        )
        free_floor = max(DEFAULT_MIN_FREE_BYTES, configured_floor)
    evidence_cap = max_evidence_bytes
    if evidence_cap is None:
        configured_cap = int(
            os.getenv("RECON_MAX_EVIDENCE_BYTES", str(DEFAULT_MAX_EVIDENCE_BYTES))
        )
        evidence_cap = min(DEFAULT_MAX_EVIDENCE_BYTES, configured_cap)
    if free_floor <= 0 or evidence_cap <= 0:
        raise ValueError("disk headroom and evidence caps must be positive")
    if shutil.disk_usage(selected).free < free_floor:
        raise EvidenceCapacityError("recon evidence disk headroom is below configured minimum")
    if _tree_bytes(selected) + additional_bytes > evidence_cap:
        raise EvidenceCapacityError("recon evidence byte budget would be exceeded")
    return selected


def store_private_evidence(subdirectory: str, suffix: str, data: bytes) -> Path:
    """Retain raw output in a private subdirectory with mode-600 file bits."""
    if not subdirectory or Path(subdirectory).is_absolute() or ".." in Path(subdirectory).parts:
        raise ValueError("evidence subdirectory must be relative to the root")
    root = ensure_evidence_capacity(additional_bytes=len(data))
    target_dir = root / subdirectory
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target_dir.is_symlink() or not target_dir.resolve().is_relative_to(root.resolve()):
        raise EvidenceCapacityError("evidence subdirectory must remain under the private root")
    target_dir.chmod(0o700)
    fd, raw_path = tempfile.mkstemp(
        prefix=f"{secrets.token_hex(8)}-", suffix=suffix, dir=target_dir,
    )
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    ensure_evidence_capacity()
    return Path(raw_path)
