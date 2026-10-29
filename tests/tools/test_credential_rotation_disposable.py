from __future__ import annotations

import shutil
import subprocess

import pytest


def test_disposable_rotation_contract() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI unavailable")
    probe = subprocess.run(["docker", "info"], capture_output=True, text=True)
    if probe.returncode:
        pytest.skip("docker daemon unavailable")
    result = subprocess.run(
        ["python", "scripts/verify_credential_rotation_disposable.py"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "contract verified" in result.stdout
