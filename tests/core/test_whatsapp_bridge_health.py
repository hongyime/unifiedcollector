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


def test_whatsapp_bridge_summary_reports_paired_if_any_slot_ready():
    summary = summarize_whatsapp_bridge_health([
        {"bridge": "1", "ok": True, "status": "ready", "whatsapp_ready": True},
        {"bridge": "2", "ok": True, "status": "awaiting_scan", "qr_available": True},
    ])

    assert summary["status"] == "paired"
    assert summary["ready_count"] == 1
