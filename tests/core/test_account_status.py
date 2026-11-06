import pytest

from src.core import account_status


class _Acquire:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def fetch(self, sql):
        if "telegram_user_accounts" in sql:
            return [{"name": "account", "phone": "1", "status": "ok", "last_connected_at": None, "last_error": None}]
        if "source_health" in sql:
            return [{"source": "telegram", "status": "live", "last_success_at": None, "last_error": None}]
        if "beeper_shadow_accounts" in sql:
            return [{"account_id": "discord", "network": "Discord", "login_id": "1", "user_id": "u",
                     "user_full_name": "Owner", "user_username": "owner", "user_phone": "+6591234567",
                     "status": "connected", "last_seen_at": None}]
        return [{"platform": "x", "account": "owner", "status": "live", "reason": None}]


class _Pool:
    def acquire(self):
        return _Acquire()


@pytest.mark.asyncio
async def test_runtime_account_status_merges_db_and_bridge_state(monkeypatch):
    async def bridges():
        return [{
            "bridge": "2", "whatsapp_ready": False, "qr_available": True,
            "invite_accept_available": True, "invite_accept_state": "available_not_proven", "auth_state": {},
            "history": {"chats": 2, "complete": 0, "pending": 1, "eligible": 1},
        }]

    monkeypatch.setattr(account_status, "fetch_whatsapp_bridge_health", bridges)
    payload = await account_status.get_runtime_account_status(_Pool())

    assert payload["telegram"][0]["name"] == "account"
    assert payload["whatsapp"] == [{
        "session": "2", "ready": False, "status": "awaiting_scan", "needs_scan": True,
        "invite_accept_available": True, "invite_accept_state": "available_not_proven", "auth_state": {},
        "history": {"chats": 2, "complete": 0, "pending": 1, "eligible": 1},
    }]
    assert payload["cookie_status"] == {("x", "owner"): ("live", None)}
    assert payload["beeper"][0]["network"] == "Discord"
