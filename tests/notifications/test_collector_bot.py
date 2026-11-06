from __future__ import annotations

import pytest

from src import control_executor
from src.notifications import collector_bot


class _Acquire:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *_args):
        return False


class _UpdateDB:
    def __init__(self):
        self.updates = set()

    async def execute(self, _sql, update_id, _payload):
        if update_id in self.updates:
            return "INSERT 0 0"
        self.updates.add(update_id)
        return "INSERT 0 1"


class _Pool:
    def __init__(self, db):
        self.db = db

    def acquire(self):
        return _Acquire(self.db)


@pytest.mark.asyncio
async def test_duplicate_update_is_durably_rejected():
    pool = _Pool(_UpdateDB())

    assert await collector_bot._accept_update(pool, {"update_id": 19}) is True
    assert await collector_bot._accept_update(pool, {"update_id": 19}) is False


@pytest.mark.asyncio
async def test_callback_from_another_chat_never_checks_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    answers = []

    def answer(*args, **kwargs):
        answers.append((args, kwargs))

    monkeypatch.setattr(collector_bot.telegram, "answer_callback_query", answer)

    async def should_not_run(*_args):
        raise AssertionError("authorization must stop before membership lookup")

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", should_not_run)
    await collector_bot._handle_callback(None, {
        "id": "callback", "data": "wd:restart:token", "from": {"id": 9},
        "message": {"chat": {"id": -1008}, "message_id": 1},
    })

    assert answers[0][0][1] == "This control is only valid in the control room."


@pytest.mark.asyncio
async def test_revoked_admin_cannot_authorize_mutation(monkeypatch):
    monkeypatch.setattr(
        collector_bot.telegram, "get_chat_member",
        lambda _chat_id, _user_id: {"ok": True, "result": {"status": "member"}},
    )

    assert await collector_bot._is_current_chat_admin(-1007, 9) is False


def test_executor_rejects_arbitrary_service(monkeypatch):
    monkeypatch.setenv("CONTROL_EXECUTOR_SECRET", "test-secret")
    operation = {"idempotency_key": "card:restart", "action": "restart", "target": "unifiedcollector_postgres"}
    operation["signature"] = control_executor._signature(
        operation["idempotency_key"], operation["action"], operation["target"],
    )

    assert control_executor._valid_operation(operation)
    assert operation["target"] not in control_executor.ALLOWED_SERVICES


def test_lanes_command_accepts_bot_mentions_and_rejects_other_text():
    assert collector_bot._command_name("/lanes@control_bot please") == "lanes"
    assert collector_bot._command_name("/lane-details@control_bot whatsapp") == "lane-details"
    assert collector_bot._command_name("/link-status@control_bot telegram") == "link-status"
    assert collector_bot._command_name("/feed-status@control_bot") == "feed-status"
    assert collector_bot._command_name("/feed-deliveries@control_bot whatsapp") == "feed-deliveries"
    assert collector_bot._command_name("/seen-targets@control_bot instagram") == "seen-targets"
    assert collector_bot._command_name("/health@control_bot") == "health"
    assert collector_bot._command_name("/coverage@control_bot") == "coverage"
    assert collector_bot._command_name("/capabilities@control_bot") == "capabilities"
    assert collector_bot._command_name("/targets@control_bot") == "targets"
    assert collector_bot._command_name("/schedules@control_bot") == "schedules"
    assert collector_bot._command_name("/schedule@control_bot telegram 24 off") == "schedule"
    assert collector_bot._command_name("/disable-telegram@control_bot account") == "disable-telegram"
    assert collector_bot._command_name("/add-target@control_bot telegram target 5") == "add-target"
    assert collector_bot._command_name("/source-controls@control_bot") == "source-controls"
    assert collector_bot._command_name("/rates@control_bot") == "rates"
    assert collector_bot._command_name("/accounts@control_bot") == "accounts"
    assert collector_bot._command_name("/actions@control_bot open") == "actions"
    assert collector_bot._command_name("/graph@control_bot instagram") == "graph"
    assert collector_bot._command_name("/chats@control_bot whatsapp") == "chats"
    assert collector_bot._command_name("/chats@control_bot beeper") == "chats"
    assert collector_bot._command_name("/interactions@control_bot whatsapp") == "interactions"
    assert collector_bot._command_name("/media@control_bot instagram") == "media"
    assert collector_bot._command_name("/media-get@control_bot abc") == "media-get"
    assert collector_bot._command_name("/profiles@control_bot lemon8") == "profiles"
    assert collector_bot._command_name("/posts@control_bot lemon8 creator") == "posts"
    assert collector_bot._command_name("/posts@control_bot threads creator") == "posts"
    assert collector_bot._command_name("/dlq@control_bot telegram") == "dlq"
    assert collector_bot._command_name("/stories@control_bot") == "stories"
    assert collector_bot._command_name("/members@control_bot whatsapp group@g.us") == "members"
    assert collector_bot._command_name("/members@control_bot beeper !room:beeper.local") == "members"
    assert collector_bot._command_name("/messages@control_bot whatsapp group@g.us") == "messages"
    assert collector_bot._command_name("/wa-users@control_bot person") == "wa-users"
    assert collector_bot._command_name("/wa-user@control_bot person@s.whatsapp.net") == "wa-user"
    assert collector_bot._command_name("/messaging-coverage@control_bot") == "messaging-coverage"
    assert collector_bot._command_name("lanes") is None


def test_dlq_format_escapes_provider_errors():
    text = collector_bot._format_dlq_status("telegram", [{
        "source": "telegram", "status": "pending", "retry_count": 1,
        "entity_id": "<message>", "error_message": "<timeout>",
    }])

    assert "&lt;timeout&gt;" in text
    assert "&lt;message&gt;" in text


def test_stories_format_is_counts_only():
    text = collector_bot._format_stories_status({
        "stats": {"stories": 2, "highlights": 1, "accounts": 1, "sources": 1},
        "accounts": [{"source": "instagram", "entity_name": "creator", "story_count": 2, "highlight_count": 1}],
    })

    assert "2 stories, 1 highlights" in text


def test_member_format_escapes_roster_text():
    text = collector_bot._format_member_status("whatsapp", "group@g.us", [{"pushname": "<member>", "role": "admin"}])

    assert "&lt;member&gt;" in text


def test_message_format_escapes_and_omits_media_links():
    text = collector_bot._format_message_status("whatsapp", "group@g.us", {
        "messages": [{"sender_pushname": "<member>", "text": "<hello>", "media_url": "https://private.example/file"}],
    })

    assert "&lt;member&gt;" in text
    assert "&lt;hello&gt;" in text
    assert "private.example" not in text


def test_lane_detail_format_escapes_cursor_and_shows_fence():
    text = collector_bot._format_lane_details([{
        "source": "whatsapp", "account_identity": "wid", "target_identity": "<chat>",
        "status": "leased", "lease_owner": "worker", "fence_token": 2,
        "retry_count": 1, "item_cost": 3, "byte_cost": 4, "cursor": {"id": "<next>"},
    }], "whatsapp")

    assert "fence 2" in text
    assert "&lt;chat&gt;" in text
    assert "&lt;next&gt;" in text


def test_lane_formatter_reports_telegram_historical_lag_and_account_throughput():
    text = collector_bot._format_lane_status({
        "totals": {"queue_depth": 4, "completed": 1, "total": 5},
        "platforms": [{
            "platform": "telegram", "queue_depth": 4, "completed": 1, "total": 5,
            "active_history_workers": 2,
            "common_lane": {"historical_lag_seconds": 90},
            "account_throughput": [{
                "account_identity": "account-1", "active_claims": 1, "claim_budget": 2,
                "write_rate": 1.5, "flood_waits": 3,
            }],
        }],
    })

    assert "historical lag 90s" in text
    assert "active history workers 2" in text
    assert "account-1" in text


def test_link_approval_status_escapes_url_and_error():
    text = collector_bot._format_link_approval_status([{
        "source": "telegram", "account_name": "account", "state": "failed",
        "target_url": "https://example.invalid/<invite>", "last_error": "<denied>",
    }], "telegram")

    assert "&lt;invite&gt;" in text
    assert "&lt;denied&gt;" in text


def test_realtime_feed_policy_format_reports_whatsapp_exclusion():
    text = collector_bot._format_realtime_feed_policy({
        "enabled": True, "excluded_sources": ["whatsapp", "beeper_whatsapp"],
        "whatsapp_group_media_excluded": True,
    })

    assert "excluded" in text
    assert "whatsapp" in text


def test_realtime_delivery_format_escapes_reason_without_media_url():
    text = collector_bot._format_realtime_deliveries({"available": True, "items": [{
        "source": "whatsapp", "status": "skipped", "content_type": "image",
        "reason": "<source_excluded>", "media_url": "https://private.example/file",
    }]}, "whatsapp")

    assert "&lt;source_excluded&gt;" in text
    assert "private.example" not in text


def test_seen_targets_format_escapes_target_text():
    text = collector_bot._format_seen_targets({
        "summary": {"instagram": {"total": 2}},
        "targets": [{"source": "instagram", "target_display": "<target>", "status": "pending", "origin": "registry", "priority": 1}],
    }, "instagram")

    assert "Registry total: <b>2</b>" in text
    assert "&lt;target&gt;" in text


def test_whatsapp_user_history_omits_media_links():
    text = collector_bot._format_whatsapp_user_history("person@s.whatsapp.net", [{
        "text": "<hello>", "media_url": "https://private.example/file",
    }])

    assert "&lt;hello&gt;" in text
    assert "private.example" not in text


def test_messaging_coverage_format_marks_mirrors_non_additive():
    text = collector_bot._format_messaging_coverage([{"network": "WhatsApp", "native_messages": 2, "beeper_messages": 3}])
    assert "not additive" in text


def test_link_approval_callback_parser_accepts_only_known_actions():
    assert collector_bot._parse_callback_data("ln:approve:token") == ("ln", "approve", "token")
    assert collector_bot._parse_callback_data("ln:restart:token") is None


def test_link_cards_allow_only_executor_backed_messaging_approvals():
    import inspect

    source = inspect.getsource(collector_bot._prepare_link_card)
    assert "'telegram', 'whatsapp'" in source
    assert "source IN" in source


def test_verified_manifest_format_is_explicitly_non_destructive():
    text = collector_bot._format_verified_manifest({
        "id": "receipt", "profile_candidates": 2,
        "sources": [{"source": "x", "profile_candidates": 2}],
    })

    assert "receipt" in text
    assert "No data or media was deleted." in text


def test_account_status_shows_unproven_whatsapp_invite_capability():
    text = collector_bot._format_account_status({
        "telegram": [], "cookie_status": {},
        "whatsapp": [{"session": "1", "status": "connected", "invite_accept_available": True,
                      "invite_accept_state": "available_not_proven"}],
    })

    assert "invite join available_not_proven" in text


def test_account_status_shows_whatsapp_history_progress():
    text = collector_bot._format_account_status({
        "telegram": [], "cookie_status": {}, "beeper": [],
        "whatsapp": [{"session": "1", "status": "connected", "history": {
            "chats": 20, "complete": 3, "pending": 2, "eligible": 15,
        }}],
    })

    assert "history 3/20 complete; 2 in flight; 15 eligible" in text


def test_account_status_masks_beeper_phone_number():
    text = collector_bot._format_account_status({
        "telegram": [], "whatsapp": [], "cookie_status": {},
        "beeper": [{"network": "Discord", "user_username": "owner", "user_phone": "+6591234567", "status": "connected"}],
    })

    assert "••••4567" in text
    assert "91234567" not in text


def test_source_controls_format_escapes_stop_reason():
    text = collector_bot._format_source_controls_status([{
        "source_name": "whatsapp", "state": "rate_limited", "enabled": True,
        "consecutive_failures": 1, "stop_reason": "<retry>",
    }])

    assert "&lt;retry&gt;" in text


def test_collection_actions_format_escapes_reason():
    text = collector_bot._format_collection_actions_status({
        "filter": "open", "actions": [{"source": "x", "action_type": "review", "priority": 1, "reason": "<check>"}],
    })

    assert "&lt;check&gt;" in text


@pytest.mark.asyncio
async def test_lanes_command_from_another_chat_never_checks_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")

    async def should_not_run(*_args):
        raise AssertionError("foreign chat must stop before membership lookup")

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", should_not_run)
    await collector_bot._handle_command(None, {
        "text": "/lanes", "from": {"id": 9}, "chat": {"id": -1008},
    })


@pytest.mark.asyncio
async def test_lanes_command_dms_detail_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def lanes(_pool):
        return {"totals": {"queue_depth": 2, "completed": 3, "total": 5}, "platforms": []}

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AcquireAudit:
        async def __aenter__(self):
            return _Audit()

        async def __aexit__(self, *_args):
            return False

    class _AuditPool:
        def acquire(self):
            return _AcquireAudit()

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_lane_status", lanes)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/lanes", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "Queue depth" in sent[0][1]
    assert sent[1] == (-1007, "📊 <b>Lane status sent by private DM.</b>")


@pytest.mark.asyncio
async def test_lane_details_command_dms_detail_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def details(_pool, source):
        assert source == "whatsapp"
        return [{"source": "whatsapp", "account_identity": "wid", "target_identity": "chat", "cursor": {}}]

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_lane_details", details)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/lane-details whatsapp", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "Backfill lane details" in sent[0][1]
    assert sent[1] == (-1007, "📋 <b>Lane details sent by private DM.</b>")


@pytest.mark.asyncio
async def test_link_status_command_dms_detail_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def statuses(_pool, source):
        assert source == "telegram"
        return [{"source": "telegram", "account_name": "account", "state": "pending", "target_url": "https://example.invalid/invite"}]

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_link_approval_status", statuses)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/link-status telegram", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "Link approvals" in sent[0][1]
    assert sent[1] == (-1007, "🔗 <b>Link approval status sent by private DM.</b>")


@pytest.mark.asyncio
async def test_feed_status_command_dms_detail_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_realtime_feed_policy", lambda: {"enabled": True, "excluded_sources": ["whatsapp", "beeper_whatsapp"], "whatsapp_group_media_excluded": True})
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/feed-status", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "Realtime group-feed policy" in sent[0][1]
    assert sent[1] == (-1007, "📡 <b>Realtime feed policy sent by private DM.</b>")


@pytest.mark.asyncio
async def test_feed_deliveries_command_dms_detail_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def deliveries(_pool, source):
        assert source == "whatsapp"
        return {"available": True, "items": [{"source": "whatsapp", "status": "skipped", "content_type": "image"}]}

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_realtime_deliveries", deliveries)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/feed-deliveries whatsapp", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "Realtime feed deliveries" in sent[0][1]
    assert sent[1] == (-1007, "📨 <b>Realtime delivery status sent by private DM.</b>")


@pytest.mark.asyncio
async def test_seen_targets_command_dms_detail_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def targets(_pool, source):
        assert source == "instagram"
        return {"summary": {"instagram": {"total": 1}}, "targets": [{"source": "instagram", "target_display": "target", "status": "pending", "origin": "registry"}]}

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_seen_targets", targets)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/seen-targets instagram", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "Seen targets" in sent[0][1]
    assert sent[1] == (-1007, "🎯 <b>Seen targets sent by private DM.</b>")


@pytest.mark.asyncio
async def test_health_command_dms_detail_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def health(_pool):
        return {"sources": [{"source": "x", "status": "live", "age_seconds": 4}], "transport": {"failure_count": 0}}

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_control_room_health", health)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/health", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "Collector health" in sent[0][1]
    assert sent[1] == (-1007, "🩺 <b>Collector health sent by private DM.</b>")


@pytest.mark.asyncio
async def test_coverage_command_dms_detail_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def coverage(_pool):
        return {"summary": {"fresh": 1}, "sources": [{"source": "lemon8", "status": "fresh", "rows_24h": 2, "media_24h": 3}]}

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_coverage_status", coverage)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/coverage", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "Collection coverage" in sent[0][1]
    assert sent[1] == (-1007, "📈 <b>Collection coverage sent by private DM.</b>")


@pytest.mark.asyncio
async def test_capabilities_command_dms_detail_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def capabilities(_pool):
        return [{"source": "lemon8", "capability": "profile.followers_count", "state": "proven", "path": "native"}]

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_capability_status", capabilities)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/capabilities", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "Source capability receipts" in sent[0][1]
    assert sent[1] == (-1007, "🧪 <b>Capability receipts sent by private DM.</b>")


@pytest.mark.asyncio
async def test_targets_command_dms_detail_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def targets(_pool):
        return [{"source": "lemon8", "target_id": "target", "status": "pending", "priority": 9}]

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_target_status", targets)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/targets", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "Collection targets" in sent[0][1]
    assert sent[1] == (-1007, "🎯 <b>Collection targets sent by private DM.</b>")


@pytest.mark.asyncio
async def test_schedules_command_dms_detail_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def schedules(_pool):
        return [{"source": "lemon8", "enabled": True, "interval_hours": 24, "next_run": None}]

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_schedule_status", schedules)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/schedules", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "Collection schedules" in sent[0][1]
    assert sent[1] == (-1007, "🗓️ <b>Collection schedules sent by private DM.</b>")


@pytest.mark.asyncio
async def test_schedule_command_updates_only_via_current_admin_and_dms_detail(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []
    recorded = []

    async def admin(*_args):
        return True

    async def schedule(_pool, source, interval_hours, enabled):
        recorded.append((source, interval_hours, enabled))
        return {"source": source, "interval_hours": interval_hours, "enabled": enabled}

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text, **_kwargs):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_set_schedule", schedule)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/schedule telegram 24 off", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert recorded == [("telegram", 24, False)]
    assert sent[0][0] == 9
    assert "Schedule updated" in sent[0][1]
    assert sent[1] == (-1007, "🗓️ <b>Schedule update recorded.</b>")


@pytest.mark.asyncio
async def test_disable_telegram_command_is_reversible_and_dms_detail(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []
    calls = []

    async def admin(*_args):
        return True

    async def account(_pool, name, enabled):
        calls.append((name, enabled))
        return True

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text, **_kwargs):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_set_telegram_account_enabled", account)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/disable-telegram account", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert calls == [("account", False)]
    assert sent[0][0] == 9
    assert "disabled" in sent[0][1]
    assert sent[1] == (-1007, "👤 <b>Telegram account state recorded.</b>")


@pytest.mark.asyncio
async def test_add_target_command_dms_detail_and_audits(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []
    calls = []

    async def admin(*_args):
        return True

    async def add(_pool, source, target, priority):
        calls.append((source, target, priority))
        return {"source": source, "target": target}

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text, **_kwargs):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_add_target", add)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/add-target telegram target 5", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert calls == [("telegram", "target", 5)]
    assert sent[0][0] == 9
    assert "Target added" in sent[0][1]
    assert sent[1] == (-1007, "🎯 <b>Target addition recorded.</b>")


@pytest.mark.asyncio
async def test_rates_command_dms_detail_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def rates(_pool):
        return {"recent_summary": [{"source": "x", "account": "owner", "count": 1, "reason": "429", "active_now": True}]}

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_rate_limit_status", rates)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/rates", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "Rate limits" in sent[0][1]
    assert sent[1] == (-1007, "⏱️ <b>Rate limits sent by private DM.</b>")


@pytest.mark.asyncio
async def test_accounts_command_dms_detail_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def accounts(_pool):
        return {"telegram": [], "whatsapp": [{"session": "2", "status": "awaiting_scan"}], "cookie_status": {}}

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_account_status", accounts)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/accounts", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "Account state" in sent[0][1]
    assert sent[1] == (-1007, "👤 <b>Account state sent by private DM.</b>")


@pytest.mark.asyncio
async def test_graph_command_dms_bounded_detail_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def graph(_pool, source):
        assert source == "instagram"
        return {"nodes": [{"id": "owner"}, {"id": "friend"}], "edges": [{"source_user": "owner", "target_user": "friend", "edge_type": "following"}]}

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_graph_status", graph)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/graph instagram", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "relationship graph" in sent[0][1]
    assert sent[1] == (-1007, "🕸️ <b>Relationship graph sent by private DM.</b>")


@pytest.mark.asyncio
async def test_chats_command_dms_detail_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def chats(_pool, source):
        assert source == "whatsapp"
        return [{"title": "Group", "type": "group", "members_count": 2}]

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_chat_status", chats)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/chats whatsapp", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "whatsapp chats" in sent[0][1]
    assert sent[1] == (-1007, "💬 <b>Chat list sent by private DM.</b>")


@pytest.mark.asyncio
async def test_beeper_messages_are_private_dm_only(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def messages(_pool, source, chat_id):
        assert (source, chat_id) == ("beeper", "!room:beeper.local")
        return {"messages": [{"sender_name": "Alice", "text": "private"}]}

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_message_status", messages)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/messages beeper !room:beeper.local", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0] == (9, sent[0][1])
    assert "private" in sent[0][1]
    assert sent[1] == (-1007, "✉️ <b>Message history sent by private DM.</b>")


@pytest.mark.asyncio
async def test_whatsapp_user_history_is_private_dm_only(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def history(_pool, jid):
        assert jid == "person@s.whatsapp.net"
        return [{"text": "private", "media_url": "https://private.example/file"}]

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_whatsapp_user_history", history)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/wa-user person@s.whatsapp.net", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "private" in sent[0][1]
    assert "private.example" not in sent[0][1]
    assert sent[1] == (-1007, "✉️ <b>WhatsApp user history sent by private DM.</b>")


@pytest.mark.asyncio
async def test_interactions_command_dms_detail_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def interactions(_pool, source):
        assert source == "whatsapp"
        return {"reactions": [{"actor": "person", "emoji": "👍"}], "polls": []}

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_interaction_status", interactions)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/interactions whatsapp", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "whatsapp interactions" in sent[0][1]
    assert sent[1] == (-1007, "🗳️ <b>Interactions sent by private DM.</b>")


@pytest.mark.asyncio
async def test_media_command_dms_listing_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def media(_pool, source):
        assert source == "instagram"
        return [{"filename": "post.jpg", "content_type": "image", "file_size": 5}]

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_media_status", media)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/media instagram", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "instagram media" in sent[0][1]
    assert sent[1] == (-1007, "🗂️ <b>Media listing sent by private DM.</b>")


@pytest.mark.asyncio
async def test_media_get_delivers_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def media(_pool, media_id):
        assert media_id == "item-id"
        return {"path": "/media/item.jpg", "filename": "item.jpg", "source": "instagram"}

    async def send_document(chat_id, path, caption=""):
        assert chat_id == 9
        assert path == "/media/item.jpg"
        assert "item.jpg" in caption
        return True, 0, "", ""

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_private_media", media)
    monkeypatch.setattr(collector_bot.telegram, "send_document_to_detailed", send_document)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/media-get item-id", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent == [(-1007, "📎 <b>Media delivered by private DM.</b>")]


@pytest.mark.asyncio
async def test_profiles_command_dms_detail_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def profiles(_pool, source):
        assert source == "lemon8"
        return [{"username": "creator", "followers_count": 4, "posts_collected": 2}]

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_profile_status", profiles)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/profiles lemon8", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "lemon8 profiles" in sent[0][1]
    assert sent[1] == (-1007, "👥 <b>Profile list sent by private DM.</b>")


@pytest.mark.asyncio
async def test_posts_command_dms_detail_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def posts(_pool, source, username):
        assert (source, username) == ("lemon8", "creator")
        return {"posts": [{"title": "Post", "like_count": 2}]}

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_profile_posts", posts)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/posts lemon8 creator", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "lemon8 posts" in sent[0][1]
    assert sent[1] == (-1007, "📝 <b>Posts sent by private DM.</b>")


@pytest.mark.asyncio
async def test_threads_posts_command_dms_detail_only_to_requesting_admin(monkeypatch):
    monkeypatch.setenv("CONTROL_TELEGRAM_CHAT_ID", "-1007")
    sent = []

    async def admin(*_args):
        return True

    async def posts(_pool, source, username):
        assert (source, username) == ("threads", "creator")
        return {"posts": [{"caption": "Post", "media_url": "https://private.example/file"}]}

    class _Audit:
        async def execute(self, *_args):
            return "INSERT 0 1"

    class _AuditPool:
        def acquire(self):
            return _Acquire(_Audit())

    def send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(collector_bot, "_is_current_chat_admin", admin)
    monkeypatch.setattr(collector_bot, "_profile_posts", posts)
    monkeypatch.setattr(collector_bot.telegram, "send_message_to", send)
    await collector_bot._handle_command(_AuditPool(), {
        "text": "/posts threads creator", "from": {"id": 9}, "chat": {"id": -1007},
    })

    assert sent[0][0] == 9
    assert "threads posts" in sent[0][1]
    assert "private.example" not in sent[0][1]
    assert sent[1] == (-1007, "📝 <b>Posts sent by private DM.</b>")


def test_health_formatter_labels_mode_and_freshness_target():
    detail = collector_bot._format_control_room_health({
        "sources": [{
            "source": "telegram",
            "status": "live",
            "age_seconds": 12,
            "collection_modes": ["native_realtime", "history_backfill"],
            "freshness_target_seconds": 60,
        }],
    })

    assert "native_realtime/history_backfill" in detail
    assert "target 60s" in detail
