import asyncio

import pytest

from src.core.recon import normalize_recon_target, queue_recon_target
from src.core.recon_spiderfoot import allowed_modules, normalize_observation, target_in_scope


def test_normalize_recon_target_validates_domain_and_email():
    assert normalize_recon_target("domain", "Example.COM") == ("domain", "example.com")
    assert normalize_recon_target("email", "A@Example.COM") == ("email", "a@example.com")
    with pytest.raises(ValueError):
        normalize_recon_target("domain", "not-a-domain")
    with pytest.raises(ValueError):
        normalize_recon_target("unknown", "example.com")


def test_queue_recon_target_returns_row():
    class Conn:
        async def fetchrow(self, sql, *args):
            assert "INSERT INTO recon_targets" in sql
            assert args[0] == "domain"
            assert args[1] == "example.com"
            return {
                "id": "00000000-0000-0000-0000-000000000001",
                "target_type": args[0],
                "target_value": args[1],
                "source": args[2],
                "priority": args[3],
                "status": "pending",
            }

    row = asyncio.run(queue_recon_target(
        Conn(),
        target_type="domain",
        target_value="Example.COM",
        source="test",
        priority=2,
    ))

    assert row["target_type"] == "domain"
    assert row["target_value"] == "example.com"


def test_spiderfoot_scope_modules_and_observation_normalization(monkeypatch):
    monkeypatch.delenv("SPIDERFOOT_MODULES", raising=False)

    assert allowed_modules() == ["sfp_dnsresolve", "sfp_whois", "sfp_names"]
    assert target_in_scope("sub.example.com", "example.com")
    assert not target_in_scope("other.net", "example.com")

    obs = normalize_observation("00000000-0000-0000-0000-000000000001", {
        "module": "sfp_dnsresolve",
        "type": "DOMAIN_NAME",
        "data": "example.com",
        "confidence": "0.5",
    })

    assert obs["module"] == "sfp_dnsresolve"
    assert obs["observation_type"] == "DOMAIN_NAME"
    assert obs["confidence"] == 0.5
