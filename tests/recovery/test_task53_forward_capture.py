from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts.recovery.task53_forward_capture import CapturePreflightError, REQUIRED_RECEIPTS, preflight


def _usage(free: int):
    return shutil._ntuple_diskusage(total=free, used=0, free=free)


def _evidence(tmp_path: Path) -> Path:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700, parents=True)
    for name in REQUIRED_RECEIPTS:
        (root / name).write_text("ok", encoding="utf-8")
    return root


def test_preflight_requires_pgdata_plus_private_reserve(tmp_path):
    evidence = _evidence(tmp_path)
    pgdata = tmp_path / "pgdata"

    report = preflight(
        evidence=evidence,
        reserve_bytes=20,
        run=lambda *args: "10\tpgdata" if args[0] == "sudo" else str(pgdata),
        disk_usage=lambda _path: _usage(30),
    )

    assert report["pgdata_bytes"] == 10
    assert report["minimum_before_capture_bytes"] == 30


def test_preflight_refuses_missing_receipt_or_insufficient_space(tmp_path):
    evidence = _evidence(tmp_path)
    (evidence / REQUIRED_RECEIPTS[0]).unlink()
    with pytest.raises(CapturePreflightError, match="required receipts"):
        preflight(evidence=evidence, run=lambda *_args: str(tmp_path), disk_usage=lambda _path: _usage(999))

    evidence = _evidence(tmp_path / "second")
    with pytest.raises(CapturePreflightError, match="insufficient private headroom"):
        preflight(
            evidence=evidence,
            reserve_bytes=20,
            run=lambda *args: "10\tpgdata" if args[0] == "sudo" else str(tmp_path / "pgdata"),
            disk_usage=lambda _path: _usage(29),
        )
