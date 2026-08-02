from pathlib import Path

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
