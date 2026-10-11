"""PayPal runs unconditionally without an opt-out gate."""
from __future__ import annotations

from pathlib import Path


from src.collectors.paypal import PayPalCollector

REPO = Path(__file__).resolve().parents[2]


def test_paypal_probe_defaults_on() -> None:
    assert PayPalCollector().enabled is True


def test_paypal_probe_has_no_disable_gate() -> None:
    assert PayPalCollector().enabled is True


def test_compose_paypal_probe_has_no_gate() -> None:
    text = (REPO / "docker" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "PAYPAL_PROBE_ENABLED" not in text
