import pytest

from src.core.spider_discovery_store import (
    canonical_discovery_node,
    persist_discovery_target,
)


class ExecuteRecorder:
    def __init__(self):
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return "INSERT 0 1"


def test_discovery_nodes_are_namespaced():
    assert canonical_discovery_node("chat", "123") == "chat:123"
    assert canonical_discovery_node("member", "123") == "member:123"
    with pytest.raises(ValueError, match="target_id"):
        canonical_discovery_node("chat", "")


@pytest.mark.asyncio
async def test_discovery_evidence_and_queue_write_are_gated_and_capped(monkeypatch):
    conn = ExecuteRecorder()
    monkeypatch.setenv("TELEGRAM_SHARED_SPIDER_ENABLED", "1")

    written = await persist_discovery_target(
        conn,
        "telegram",
        "user-7",
        target_type="member",
        discovery_type="telegram_chat_member",
        source_table="telegram_chat_members",
        source_record_id="chat-2:user-7",
        confidence=0.95,
        priority=7,
        source_metadata={"chat_id": "chat-2"},
        max_pending=42,
    )

    assert written is True
    assert len(conn.calls) == 2
    evidence_sql, evidence_args = conn.calls[0]
    queue_sql, queue_args = conn.calls[1]
    assert "spider_discovery_evidence" in evidence_sql
    assert evidence_args[3] == "member:user-7"
    assert evidence_args[6] == "chat-2:user-7"
    assert "spider_queue" in queue_sql
    assert "status IN ('pending', 'in_progress')" in queue_sql
    assert queue_args[1] == "member:user-7"
    assert queue_args[2] == 7
    assert queue_args[-1] == 42


@pytest.mark.asyncio
async def test_discovery_writes_stay_off_without_platform_gate(monkeypatch):
    conn = ExecuteRecorder()
    monkeypatch.delenv("FACEBOOK_SHARED_SPIDER_ENABLED", raising=False)
    written = await persist_discovery_target(
        conn,
        "facebook",
        "profile-9",
        target_type="account",
        discovery_type="browser_feed_author",
        source_table="social_users",
        source_record_id="facebook:profile-9",
        confidence=0.75,
    )
    assert written is False
    assert conn.calls == []
