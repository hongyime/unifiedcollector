"""Native public-discovery env precedence.

One precedence for every reader: an explicit source-scoped
``<SOURCE>_PROBE_NATIVE_PUBLIC_DISCOVERY_ENABLED`` wins, then the global
``NATIVE_PUBLIC_DISCOVERY_ENABLED``, otherwise disabled.
"""
from __future__ import annotations

import pytest

from src.collectors import native_platform_scrape
from src.core.profile_only_collector import ProfileOnlyCollector

_GLOBAL = "NATIVE_PUBLIC_DISCOVERY_ENABLED"
_SCOPED = "PINTEREST_PROBE_NATIVE_PUBLIC_DISCOVERY_ENABLED"


class _PinterestProbe(ProfileOnlyCollector):
    SOURCE_NAME = "pinterest"
    PROFILE_TABLE = "pinterest_profiles"

    async def probe_profile(self, username: str):  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def collector():
    return _PinterestProbe()


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv(_GLOBAL, raising=False)
    monkeypatch.delenv(_SCOPED, raising=False)


# --- profile-only collector gate ---


def test_profile_only_global_enable_enables_discovery(clean_env, monkeypatch, collector):
    monkeypatch.setenv(_GLOBAL, "1")
    assert collector._native_public_discovery_enabled() is True


def test_profile_only_global_unset_defaults_disabled(clean_env, collector):
    assert collector._native_public_discovery_enabled() is False


def test_profile_only_global_zero_disables(clean_env, monkeypatch, collector):
    monkeypatch.setenv(_GLOBAL, "0")
    assert collector._native_public_discovery_enabled() is False


def test_profile_only_scoped_zero_overrides_global_enable(
    clean_env, monkeypatch, collector
):
    monkeypatch.setenv(_GLOBAL, "1")
    monkeypatch.setenv(_SCOPED, "0")
    assert collector._native_public_discovery_enabled() is False


def test_profile_only_scoped_one_overrides_global_disable(
    clean_env, monkeypatch, collector
):
    monkeypatch.setenv(_GLOBAL, "0")
    monkeypatch.setenv(_SCOPED, "1")
    assert collector._native_public_discovery_enabled() is True


@pytest.mark.parametrize("raw", ["true", "YES", "On", " 1 "])
def test_profile_only_truthy_variants_accepted(
    clean_env, monkeypatch, collector, raw
):
    monkeypatch.setenv(_GLOBAL, raw)
    assert collector._native_public_discovery_enabled() is True


@pytest.mark.parametrize("raw", ["", " ", "yeah", "2", "0 "])
def test_profile_only_ambiguous_values_are_disabled(
    clean_env, monkeypatch, collector, raw
):
    monkeypatch.setenv(_GLOBAL, raw)
    assert collector._native_public_discovery_enabled() is False


def test_profile_only_ambiguous_scoped_value_falls_back_disabled(
    clean_env, monkeypatch, collector
):
    # An explicit but unrecognized scoped value must not enable discovery.
    monkeypatch.setenv(_GLOBAL, "1")
    monkeypatch.setenv(_SCOPED, "junk")
    assert collector._native_public_discovery_enabled() is False


# --- native_platform_scrape helper (same precedence, platform-keyed) ---


def test_scrape_global_enable_enables_discovery(clean_env, monkeypatch):
    monkeypatch.setenv(_GLOBAL, "1")
    assert native_platform_scrape._public_discovery_enabled() is True


def test_scrape_global_unset_defaults_disabled(clean_env):
    assert native_platform_scrape._public_discovery_enabled() is False


def test_scrape_platform_scoped_zero_overrides_global_enable(
    clean_env, monkeypatch
):
    monkeypatch.setenv(_GLOBAL, "1")
    monkeypatch.setenv(_SCOPED, "0")
    assert native_platform_scrape._public_discovery_enabled("pinterest") is False


def test_scrape_platform_scoped_one_overrides_global_disable(
    clean_env, monkeypatch
):
    monkeypatch.setenv(_GLOBAL, "0")
    monkeypatch.setenv(_SCOPED, "1")
    assert native_platform_scrape._public_discovery_enabled("pinterest") is True


def test_scrape_other_platform_scope_does_not_leak(clean_env, monkeypatch):
    monkeypatch.setenv(_GLOBAL, "1")
    monkeypatch.setenv(_SCOPED, "0")
    assert native_platform_scrape._public_discovery_enabled("snapchat") is True


def test_scrape_global_flag_without_platform_ignores_scoped(
    clean_env, monkeypatch
):
    monkeypatch.setenv(_GLOBAL, "0")
    monkeypatch.setenv(_SCOPED, "1")
    assert native_platform_scrape._public_discovery_enabled() is False
