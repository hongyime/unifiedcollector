"""One profile probe per process. Unset still runs the original five."""
from __future__ import annotations

import pytest

from src.collectors.profile_only_runner import _build_collectors


def test_profile_only_source_limits_the_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROFILE_ONLY_SOURCE", "paypal")
    assert [c.SOURCE_NAME for c in _build_collectors()] == ["paypal"]


def test_unknown_profile_only_source_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROFILE_ONLY_SOURCE", "not-a-source")
    with pytest.raises(SystemExit):
        list(_build_collectors())


def test_unset_profile_only_source_keeps_all_five(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROFILE_ONLY_SOURCE", raising=False)
    assert [c.SOURCE_NAME for c in _build_collectors()] == [
        "snapchat",
        "paypal",
        "airbnb",
        "bluesky",
        "pinterest",
    ]
