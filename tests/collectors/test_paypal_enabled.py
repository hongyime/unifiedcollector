"""PayPal follows the shared profile-probe default: on unless opted out."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.collectors.paypal import PayPalCollector

REPO = Path(__file__).resolve().parents[2]


def test_paypal_probe_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAYPAL_PROBE_ENABLED", raising=False)
    assert PayPalCollector().enabled is True


def test_paypal_probe_can_still_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYPAL_PROBE_ENABLED", "0")
    assert PayPalCollector().enabled is False


def test_compose_paypal_probe_default_is_on() -> None:
    text = (REPO / "docker" / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'PAYPAL_PROBE_ENABLED: "${PAYPAL_PROBE_ENABLED:-1}"' in text
    assert 'PAYPAL_PROBE_ENABLED: "${PAYPAL_PROBE_ENABLED:-0}"' not in text
