"""Tests for src.core.env typed env helpers (P2-1)."""
from __future__ import annotations

import pytest

from src.core.env import env_bool, env_int, env_float, env_str


def test_env_bool_truthy_spellings(monkeypatch):
    for v in ("1", "true", "TRUE", "Yes", "on", "t", " y "):
        monkeypatch.setenv("X", v)
        assert env_bool("X") is True, v


def test_env_bool_falsy_spellings(monkeypatch):
    for v in ("0", "false", "NO", "off", "f", ""):
        monkeypatch.setenv("X", v)
        assert env_bool("X") is False, v


def test_env_bool_unset_uses_default(monkeypatch):
    monkeypatch.delenv("X", raising=False)
    assert env_bool("X", default=True) is True
    assert env_bool("X", default=False) is False


def test_env_bool_typo_raises(monkeypatch):
    monkeypatch.setenv("X", "ture")
    with pytest.raises(ValueError):
        env_bool("X")


def test_env_int_bounds(monkeypatch):
    monkeypatch.setenv("N", "5")
    assert env_int("N", 1) == 5
    monkeypatch.setenv("N", "0")
    with pytest.raises(ValueError):
        env_int("N", 1, min_value=1)


def test_env_int_unset_default(monkeypatch):
    monkeypatch.delenv("N", raising=False)
    assert env_int("N", 42) == 42


def test_env_float_parse(monkeypatch):
    monkeypatch.setenv("F", "1.5")
    assert env_float("F", 0.0) == 1.5


def test_env_str_required(monkeypatch):
    monkeypatch.delenv("S", raising=False)
    with pytest.raises(ValueError):
        env_str("S", required=True)
    assert env_str("S", default="d") == "d"
