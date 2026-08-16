from src.core.whatsapp_bridge_health import summarize_whatsapp_bridge_health


def test_whatsapp_bridge_summary_reports_unpaired_qr_loop():
    summary = summarize_whatsapp_bridge_health([
        {
            "bridge": "1",
            "ok": True,
            "status": "awaiting_scan",
            "whatsapp_ready": False,
            "qr_available": True,
        },
        {
            "bridge": "2",
            "ok": True,
            "status": "awaiting_scan",
            "whatsapp_ready": False,
            "qr_available": True,
        },
    ])

    assert summary["status"] == "unpaired"
    assert summary["ready_count"] == 0
    assert summary["reachable_count"] == 2
    assert "QR pairing" in summary["detail"]


def test_whatsapp_bridge_summary_treats_qr_refresh_states_as_unpaired():
    summary = summarize_whatsapp_bridge_health([
        {
            "bridge": "1",
            "ok": True,
            "status": "refreshing_qr",
            "whatsapp_ready": False,
            "qr_available": False,
            "last_disconnect_reason": "QR refs attempts ended",
        },
        {
            "bridge": "2",
            "ok": True,
            "status": "connecting_unpaired",
            "whatsapp_ready": False,
            "qr_available": False,
        },
    ])

    assert summary["status"] == "unpaired"


def test_whatsapp_bridge_summary_reports_partial_if_only_some_slots_ready():
    summary = summarize_whatsapp_bridge_health([
        {
            "bridge": "1",
            "ok": True,
            "status": "ready",
            "whatsapp_ready": True,
            "phone_number": "6584731565",
            "push_name": "Prawn Productions",
        },
        {
            "bridge": "2",
            "ok": True,
            "status": "awaiting_scan",
            "qr_available": True,
            "auth_state": {"note": "creds_json_empty_scan_required"},
        },
    ])

    assert summary["status"] == "partial"
    assert summary["ready_count"] == 1
    assert summary["waiting_count"] == 1
    assert "6584731565" in summary["detail"]
    assert "Prawn Productions" in summary["detail"]
    assert "empty slot" in summary["detail"]
    assert "scan the waiting slot only if you expect another WhatsApp account/device" in summary["detail"]


def test_whatsapp_bridge_summary_counts_empty_fresh_qr_slot_as_partial():
    summary = summarize_whatsapp_bridge_health([
        {
            "bridge": "1",
            "ok": True,
            "status": "waiting_for_fresh_qr",
            "whatsapp_ready": False,
            "needs_scan": True,
            "auth_state": {"note": "creds_json_empty_scan_required"},
        },
        {
            "bridge": "2",
            "ok": True,
            "status": "ready",
            "whatsapp_ready": True,
            "connected": True,
            "session_name": "session_2",
        },
    ])

    assert summary["status"] == "partial"
    assert summary["ready_count"] == 1
    assert summary["waiting_count"] == 1
    assert "empty slot" in summary["detail"]


def test_whatsapp_bridge_summary_reports_paired_when_all_slots_ready():
    summary = summarize_whatsapp_bridge_health([
        {"bridge": "1", "ok": True, "status": "ready", "whatsapp_ready": True, "phone_number": "111"},
        {"bridge": "2", "ok": True, "status": "ready", "whatsapp_ready": True, "phone_number": "222"},
    ])

    assert summary["status"] == "paired"
    assert summary["ready_count"] == 2
    assert summary["waiting_count"] == 0
    assert "111" in summary["detail"]
    assert "222" in summary["detail"]


def test_whatsapp_bridge_summary_does_not_call_livez_fallback_unreachable():
    summary = summarize_whatsapp_bridge_health([
        {"bridge": "1", "ok": True, "status": "health_timeout_alive"},
        {"bridge": "2", "ok": False, "status": "unreachable", "error": "timed out"},
    ])

    assert summary["status"] == "degraded"
    assert summary["reachable_count"] == 1
