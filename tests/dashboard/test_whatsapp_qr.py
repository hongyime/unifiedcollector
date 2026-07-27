from __future__ import annotations

import os

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-only-for-pytest-do-not-use")
os.environ.setdefault("DASHBOARD_ADMIN_PASSWORD", "x")

from src.dashboard.api import _should_request_fresh_wa_qr


def test_should_request_fresh_wa_qr_when_unregistered_and_no_qr():
    assert _should_request_fresh_wa_qr(
        {"whatsapp_ready": False, "registered": False, "status": "disconnected"},
        {"qr": None},
    )


def test_should_not_request_fresh_wa_qr_for_registered_session():
    assert not _should_request_fresh_wa_qr(
        {"whatsapp_ready": False, "registered": True, "status": "disconnected"},
        {"qr": None},
    )


def test_should_not_request_fresh_wa_qr_when_qr_exists():
    assert not _should_request_fresh_wa_qr(
        {"whatsapp_ready": False, "registered": False, "status": "awaiting_scan"},
        {"qr": "raw-code"},
    )
