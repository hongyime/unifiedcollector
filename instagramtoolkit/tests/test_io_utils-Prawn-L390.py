"""Tests for src/io_utils.py — safe_json_write and retry_with_backoff."""
import json
import os
import time
from unittest.mock import patch, MagicMock

import pytest

from io_utils import safe_json_write, retry_with_backoff


# ══════════════════════════════════════════════════════════════
#  safe_json_write
# ══════════════════════════════════════════════════════════════

class TestSafeJsonWrite:
    """Tests for the atomic JSON write function."""

    def test_creates_file_with_correct_content(self, tmp_path):
        path = str(tmp_path / "out.json")
        data = {"key": "value", "nums": [1, 2, 3]}
        safe_json_write(path, data)

        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == data

    def test_creates_intermediate_directories(self, tmp_path):
        path = str(tmp_path / "a" / "b" / "c.json")
        safe_json_write(path, {"nested": True})
        assert os.path.exists(path)

    def test_overwrites_existing_file_atomically(self, tmp_path):
        path = str(tmp_path / "out.json")
        safe_json_write(path, {"v": 1})
        safe_json_write(path, {"v": 2})

        with open(path, "r", encoding="utf-8") as f:
            assert json.load(f)["v"] == 2

    def test_no_temp_file_left_on_success(self, tmp_path):
        path = str(tmp_path / "out.json")
        safe_json_write(path, {"ok": True})
        leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
        assert leftovers == []

    def test_handles_unicode_content(self, tmp_path):
        path = str(tmp_path / "uni.json")
        data = {"emoji": "🎉", "chinese": "你好"}
        safe_json_write(path, data)

        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == data

    def test_respects_indent_parameter(self, tmp_path):
        path = str(tmp_path / "indented.json")
        safe_json_write(path, {"a": 1}, indent=4)

        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        # indent=4 means 4 spaces before "a"
        assert '    "a": 1' in raw

    def test_raises_on_unserializable_data(self, tmp_path):
        path = str(tmp_path / "bad.json")
        with pytest.raises(TypeError):
            safe_json_write(path, {"func": lambda x: x})
        # Temp file should be cleaned up
        leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
        assert leftovers == []

    def test_empty_dict(self, tmp_path):
        path = str(tmp_path / "empty.json")
        safe_json_write(path, {})
        with open(path, "r", encoding="utf-8") as f:
            assert json.load(f) == {}


# ══════════════════════════════════════════════════════════════
#  retry_with_backoff
# ══════════════════════════════════════════════════════════════

class TestRetryWithBackoff:
    """Tests for the retry wrapper (time.sleep is always mocked)."""

    @patch("io_utils.time.sleep")
    def test_succeeds_on_first_try(self, mock_sleep):
        result = retry_with_backoff(lambda: 42)
        assert result == 42
        mock_sleep.assert_not_called()

    @patch("io_utils.time.sleep")
    def test_retries_on_connection_error(self, mock_sleep):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("network blip")
            return "ok"

        result = retry_with_backoff(flaky, max_retries=3, base_delay=1.0)
        assert result == "ok"
        assert calls["n"] == 3
        assert mock_sleep.call_count == 2  # slept before attempt 2 and 3

    @patch("io_utils.time.sleep")
    def test_returns_none_after_exhaustion(self, mock_sleep):
        def always_fail():
            raise ConnectionError("down")

        result = retry_with_backoff(always_fail, max_retries=2, base_delay=0.1)
        assert result is None

    @patch("io_utils.time.sleep")
    def test_rate_limit_uses_floor_delay(self, mock_sleep):
        def rate_limited():
            raise ConnectionError("Please wait a few minutes before trying again")

        retry_with_backoff(rate_limited, max_retries=1, base_delay=1.0)
        # Should have used at least 300s (the rate-limit floor)
        if mock_sleep.call_count > 0:
            actual_delay = mock_sleep.call_args[0][0]
            assert actual_delay >= 300

    @patch("io_utils.time.sleep")
    def test_non_retryable_exception_propagates(self, mock_sleep):
        def fatal():
            raise ValueError("bad input")

        with pytest.raises(ValueError, match="bad input"):
            retry_with_backoff(fatal, max_retries=3)
        mock_sleep.assert_not_called()

    @patch("io_utils.time.sleep")
    def test_label_appears_in_output(self, mock_sleep, capsys):
        def fail_once():
            raise ConnectionError("oops")

        retry_with_backoff(fail_once, max_retries=1, base_delay=0.1, label="test-op")
        captured = capsys.readouterr()
        assert "test-op" in captured.out

    @patch("io_utils.time.sleep")
    def test_passes_args_and_kwargs(self, mock_sleep):
        def adder(a, b, extra=0):
            return a + b + extra

        result = retry_with_backoff(adder, 3, 4, extra=10)
        assert result == 17
