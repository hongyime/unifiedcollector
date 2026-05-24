from __future__ import annotations

import importlib
import sys
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[2] / "services" / "user_intelligence"
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))


def load_module(name: str):
    return importlib.import_module(f"user_intelligence.{name}")


def test_change_tracker_detects_changes_and_ignores_empty_overwrite():
    tracker_mod = load_module("change_tracker")
    tracker = tracker_mod.ChangeTracker()

    last_known = {
        "push_name": "Alice",
        "display_name": "Alice D",
    }
    payload = {
        "push_name": "",
        "display_name": "Alice Doe",
    }

    changes = tracker.detect_changes(payload, last_known)
    assert ("display_name", "Alice D", "Alice Doe") in changes
    assert all(change[0] != "push_name" for change in changes)


def test_network_pairs_are_sorted_before_upsert(monkeypatch):
    net_mod = load_module("network_builder")

    calls: list[tuple[str, str]] = []

    async def fake_list_other(chat_jid: str, user_jid: str, conn):
        return ["z-user", "a-user"]

    async def fake_upsert(user_a: str, user_b: str, conn):
        calls.append((user_a, user_b))

    monkeypatch.setattr(net_mod.database, "list_other_chat_members", fake_list_other)
    monkeypatch.setattr(net_mod.database, "upsert_connection", fake_upsert)

    import asyncio

    updates = asyncio.run(net_mod.network_builder.update_for_new_membership("m-user", "chat@g.us", conn=None))
    assert updates == 2
    assert calls == [("m-user", "z-user"), ("m-user", "a-user")]
