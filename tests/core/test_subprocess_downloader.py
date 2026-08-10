from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.core import subprocess_downloader
from src.core.subprocess_downloader import DownloadResult


def _result(**kwargs) -> DownloadResult:
    values = {
        "returncode": 1,
        "stdout": "",
        "stderr": "",
        "files": [],
        "tempdir": Path("."),
        "elapsed": 12.3,
        "timed_out": False,
        "cancelled": False,
    }
    values.update(kwargs)
    return DownloadResult(**values)


def test_output_summary_prefers_stderr_and_keeps_stdout_context():
    result = _result(stdout="stdout detail", stderr="stderr detail")

    assert result.output_summary(80) == "stderr: stderr detail\nstdout: stdout detail"
    assert result.err_summary(80) == "stderr: stderr detail\nstdout: stdout detail"


def test_output_summary_falls_back_to_stdout():
    result = _result(stdout="download failed in stdout")

    assert result.output_summary(80) == "download failed in stdout"


def test_output_summary_classifies_blank_failure():
    result = _result(returncode=7)

    assert result.output_summary(80) == "process exited with rc=7; no stderr/stdout captured"


def test_output_summary_classifies_timeout_and_cancel():
    assert _result(timed_out=True).output_summary(80) == "process timed out after 12.3s"
    assert _result(cancelled=True).output_summary(80) == "process cancelled"


@pytest.mark.asyncio
async def test_yt_dlp_download_disables_update_check(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess_downloader, "check_tool", lambda name: True)
    runner = AsyncMock(return_value=_result(returncode=0, tempdir=tmp_path))
    monkeypatch.setattr(subprocess_downloader, "_run_and_collect", runner)

    await subprocess_downloader.yt_dlp_download("https://example.test/video", tempdir=str(tmp_path))

    argv = runner.await_args.args[0]
    assert "--no-update" in argv


@pytest.mark.asyncio
async def test_gallery_dl_runs_from_tempdir_to_capture_relative_outputs(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess_downloader, "check_tool", lambda name: True)
    runner = AsyncMock(return_value=_result(returncode=0, tempdir=tmp_path))
    monkeypatch.setattr(subprocess_downloader, "_run_and_collect", runner)

    await subprocess_downloader.gallery_dl_download("https://example.test/@user", tempdir=str(tmp_path))

    assert runner.await_args.kwargs["cwd"] == str(tmp_path)


@pytest.mark.asyncio
async def test_yt_dlp_runs_from_tempdir_to_capture_relative_outputs(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess_downloader, "check_tool", lambda name: True)
    runner = AsyncMock(return_value=_result(returncode=0, tempdir=tmp_path))
    monkeypatch.setattr(subprocess_downloader, "_run_and_collect", runner)

    await subprocess_downloader.yt_dlp_download("https://example.test/video", tempdir=str(tmp_path))

    assert runner.await_args.kwargs["cwd"] == str(tmp_path)
