from src.recon_spiderfoot_service import format_report


def test_format_report_omits_target_value_from_default_logs():
    line = format_report({
        "status": "completed",
        "target": {
            "target_type": "email",
            "target_value": "person@example.com",
        },
        "modules": ["sfp_dnsresolve", "sfp_whois"],
        "observations": 3,
        "dry_run": False,
    })

    assert "status=completed" in line
    assert "target_type=email" in line
    assert "observations=3" in line
    assert "sfp_dnsresolve,sfp_whois" in line
    assert "person@example.com" not in line


def test_format_report_includes_bounded_error():
    line = format_report({
        "status": "failed",
        "target": {"target_type": "domain", "target_value": "example.com"},
        "observations": 0,
        "error": "x" * 300,
    })

    assert "target_type=domain" in line
    assert "error=" in line
    assert len(line) < 260
    assert "example.com" not in line
