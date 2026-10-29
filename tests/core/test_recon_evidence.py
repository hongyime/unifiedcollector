from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from src.core import recon_evidence


def test_evidence_root_is_owner_only_and_unreadable_by_unprivileged_identity(
    monkeypatch,
):
    parent = Path(tempfile.mkdtemp(prefix="recon-evidence-test-", dir="/tmp"))
    try:
        parent.chmod(0o755)
        root = parent / "recon-evidence"
        monkeypatch.setenv("RECON_EVIDENCE_ROOT", str(root))
        ensured = recon_evidence.evidence_root()
        (ensured / "raw.json").write_text("sensitive")
        (ensured / "raw.json").chmod(0o600)

        assert root.stat().st_mode & 0o777 == 0o700
        sudo = shutil.which("sudo")
        assert sudo, "sudo is required for the unprivileged permission probe"
        result = subprocess.run(
            [sudo, "-n", "-u", "nobody", sys.executable, "-c",
             "from pathlib import Path; Path(__import__('sys').argv[1]).read_text()",
             str(root / "raw.json")],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def test_evidence_capacity_fails_closed_for_low_headroom(monkeypatch, tmp_path):
    monkeypatch.setattr(
        recon_evidence.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 99})(),
    )
    with pytest.raises(recon_evidence.EvidenceCapacityError, match="headroom"):
        recon_evidence.ensure_evidence_capacity(
            tmp_path / "evidence", min_free_bytes=100, max_evidence_bytes=1000
        )


def test_evidence_capacity_fails_closed_when_retained_bytes_would_exceed_cap(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        recon_evidence.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 10_000})(),
    )
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    (root / "existing.json").write_bytes(b"x" * 9)
    with pytest.raises(recon_evidence.EvidenceCapacityError, match="byte budget"):
        recon_evidence.ensure_evidence_capacity(
            root, additional_bytes=2, min_free_bytes=1, max_evidence_bytes=10
        )


def test_environment_cannot_disable_the_hard_disk_floor_or_raise_evidence_cap(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("RECON_MIN_DISK_FREE_BYTES", "1")
    monkeypatch.setenv("RECON_MAX_EVIDENCE_BYTES", str(100 * 1024**3))
    monkeypatch.setattr(
        recon_evidence.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": recon_evidence.DEFAULT_MIN_FREE_BYTES - 1})(),
    )
    with pytest.raises(recon_evidence.EvidenceCapacityError, match="headroom"):
        recon_evidence.ensure_evidence_capacity(tmp_path / "evidence")

    monkeypatch.setattr(
        recon_evidence.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 100 * 1024**3})(),
    )
    with pytest.raises(recon_evidence.EvidenceCapacityError, match="byte budget"):
        recon_evidence.ensure_evidence_capacity(
            tmp_path / "other-evidence",
            additional_bytes=recon_evidence.DEFAULT_MAX_EVIDENCE_BYTES + 1,
        )
