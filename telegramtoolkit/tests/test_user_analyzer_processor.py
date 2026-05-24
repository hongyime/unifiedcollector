#!/usr/bin/env python3
import asyncio
import unittest

import src.core.state_manager as state_manager_module
from src.core.state_manager import StateManager, shutdown_state_manager
from src.managers.processors.user_analyzer_processor import UserAnalyzerProcessor


class FakeUser:
    def __init__(self, user_id, username="", first_name="Test", last_name="User", bot=False):
        self.id = user_id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.phone = ""
        self.bot = bot
        self.verified = False
        self.premium = False


class FakeChat:
    def __init__(self, entity_id, title, linked_chat_id=None):
        self.id = entity_id
        self.title = title
        self.linked_chat_id = linked_chat_id


class FakeMessageEntityMentionName:
    def __init__(self, user_id):
        self.user_id = user_id


class FakeMessageEntityMention:
    def __init__(self, offset, length):
        self.offset = offset
        self.length = length


class FakeAction:
    def __init__(self, user_id=None, users=None, inviter_id=None):
        self.user_id = user_id
        self.users = users or []
        self.inviter_id = inviter_id


class FakeForward:
    def __init__(self, from_id=None, sender_id=None):
        self.from_id = from_id
        self.sender_id = sender_id


class FakeReplyMessage:
    def __init__(self, sender_id):
        self.sender_id = sender_id


class FakeMessage:
    def __init__(
        self,
        message_id,
        text="",
        sender_id=None,
        via_bot_id=None,
        reply_to_msg_id=None,
        reply_message=None,
        entities=None,
        action=None,
        forward=None,
        raise_on_reply=False,
    ):
        self.id = message_id
        self.text = text
        self.message = text
        self.raw_text = text
        self.sender_id = sender_id
        self.via_bot_id = via_bot_id
        self.reply_to_msg_id = reply_to_msg_id
        self.entities = entities or []
        self.caption_entities = []
        self.action = action
        self.forward = forward
        self._reply_message = reply_message
        self._raise_on_reply = raise_on_reply

    async def get_reply_message(self):
        if self._raise_on_reply:
            raise RuntimeError("reply lookup failed")
        return self._reply_message


class FakeClient:
    def __init__(self, entities=None, participants_by_entity=None, messages_by_entity=None, admin_logs_by_entity=None):
        self.entities = entities or {}
        self.participants_by_entity = participants_by_entity or {}
        self.messages_by_entity = messages_by_entity or {}
        self.admin_logs_by_entity = admin_logs_by_entity or {}

    async def get_entity(self, reference):
        if reference in self.entities:
            value = self.entities[reference]
        elif isinstance(reference, str) and reference.lstrip("@") in self.entities:
            value = self.entities[reference.lstrip("@")]
        else:
            raise ValueError(f"missing entity for {reference}")

        if isinstance(value, Exception):
            raise value
        return value

    async def iter_participants(self, entity):
        participants = self.participants_by_entity.get(getattr(entity, "id", entity), [])
        if isinstance(participants, Exception):
            raise participants
        for participant in participants:
            yield participant

    async def iter_messages(self, entity, limit=None):
        messages = list(self.messages_by_entity.get(getattr(entity, "id", entity), []))
        if isinstance(messages, Exception):
            raise messages
        if limit is not None:
            messages = messages[:limit]
        for message in messages:
            yield message

    async def iter_admin_log(self, entity, **kwargs):
        events = self.admin_logs_by_entity.get(getattr(entity, "id", entity), [])
        if isinstance(events, Exception):
            raise events
        for event in events:
            yield event


class FakeAdminEvent:
    def __init__(self, user_id=None, old=None, new=None):
        self.user_id = user_id
        self.old = old
        self.new = new


class UserAnalyzerProcessorTests(unittest.TestCase):
    def setUp(self):
        shutdown_state_manager()
        StateManager._instance = None
        self.state = StateManager(":memory:")
        self.state._shutdown = True
        state_manager_module._state_manager = self.state

        self.processor = UserAnalyzerProcessor()
        self.processor.state = self.state
        self.processor.max_retries = 1
        self.processor.retry_delay = 0

    def tearDown(self):
        shutdown_state_manager()
        StateManager._instance = None
        state_manager_module._state_manager = None

    def test_process_message_collects_from_multiple_sources(self):
        client = FakeClient(
            entities={
                1: FakeUser(1, "sender"),
                2: FakeUser(2, "via_bot", bot=True),
                3: FakeUser(3, "reply_user"),
                4: FakeUser(4, "mention_name"),
                5: FakeUser(5, "action_user"),
                6: FakeUser(6, "action_user_two"),
                7: FakeUser(7, "forward_user"),
                "@knownuser": FakeUser(8, "knownuser"),
                "knownuser": FakeUser(8, "knownuser"),
            }
        )
        text = "hello @knownuser and @missinguser"
        message = FakeMessage(
            123,
            text=text,
            sender_id=1,
            via_bot_id=2,
            reply_to_msg_id=99,
            reply_message=FakeReplyMessage(3),
            entities=[
                FakeMessageEntityMentionName(4),
                FakeMessageEntityMention(offset=text.index("@knownuser"), length=len("@knownuser")),
            ],
            action=FakeAction(user_id=5, users=[6]),
            forward=FakeForward(from_id=7),
        )

        asyncio.run(
            self.processor.process_message(
                {
                    "message": message,
                    "group_id": "1000",
                    "group_name": "Example Group",
                    "account_name": "acct1",
                    "client": client,
                }
            )
        )

        self.assertEqual(self.state.get_user_count(), 8)
        self.assertEqual(self.state.get_membership_count(), 8)
        self.assertEqual(self.state.get_feature_progress("acct1", "1000", "users"), 123)

    def test_process_message_continues_after_non_fatal_reply_error(self):
        client = FakeClient(entities={1: FakeUser(1, "sender_only")})
        message = FakeMessage(
            124,
            text="reply failure test",
            sender_id=1,
            reply_to_msg_id=50,
            raise_on_reply=True,
        )

        asyncio.run(
            self.processor.process_message(
                {
                    "message": message,
                    "group_id": "1001",
                    "group_name": "Graceful Group",
                    "account_name": "acct1",
                    "client": client,
                }
            )
        )

        self.assertEqual(self.state.get_user_count(), 1)
        self.assertGreaterEqual(self.processor.stats["non_fatal_errors"], 1)
        self.assertEqual(self.state.get_feature_progress("acct1", "1001", "users"), 124)

    def test_on_scan_start_collects_group_and_linked_chat_participants(self):
        main_group = FakeChat(2000, "Main Group", linked_chat_id=3000)
        linked_group = FakeChat(3000, "Linked Group")
        client = FakeClient(
            entities={3000: linked_group},
            participants_by_entity={
                2000: [FakeUser(11, "main_member"), FakeUser(12, "main_member_two")],
                3000: [FakeUser(21, "linked_member")],
            },
        )

        asyncio.run(
            self.processor.on_scan_start(
                {
                    "client": client,
                    "entity": main_group,
                    "group_id": "2000",
                    "group_name": "Main Group",
                    "account_name": "acct1",
                }
            )
        )

        self.assertEqual(self.state.get_user_count(), 3)
        self.assertEqual(self.state.get_membership_count(), 3)
        memberships = self.state.conn.execute(
            "SELECT group_id FROM memberships ORDER BY group_id, user_id"
        ).fetchall()
        self.assertEqual([row["group_id"] for row in memberships], ["2000", "2000", "3000"])

    def test_on_scan_start_collects_linked_chat_message_users(self):
        main_group = FakeChat(4000, "Channel", linked_chat_id=5000)
        linked_group = FakeChat(5000, "Discussion")
        linked_message = FakeMessage(
            301,
            text="hi from discussion",
            sender_id=31,
            via_bot_id=32,
            action=FakeAction(user_id=33),
        )
        client = FakeClient(
            entities={
                5000: linked_group,
                31: FakeUser(31, "discussion_sender"),
                32: FakeUser(32, "discussion_bot", bot=True),
                33: FakeUser(33, "discussion_action"),
            },
            messages_by_entity={5000: [linked_message]},
        )

        asyncio.run(
            self.processor.on_scan_start(
                {
                    "client": client,
                    "entity": main_group,
                    "group_id": "4000",
                    "group_name": "Channel",
                    "account_name": "acct1",
                }
            )
        )

        self.assertEqual(self.state.get_user_count(), 3)
        memberships = self.state.conn.execute(
            "SELECT DISTINCT group_id FROM memberships ORDER BY group_id"
        ).fetchall()
        self.assertEqual([row["group_id"] for row in memberships], ["5000"])
        self.assertEqual(self.processor.stats["linked_chat_messages_processed"], 1)

    def test_on_scan_start_admin_log_is_non_fatal_when_not_permitted(self):
        main_group = FakeChat(6000, "Restricted Group")
        client = FakeClient(admin_logs_by_entity={6000: RuntimeError("admin log denied")})

        asyncio.run(
            self.processor.on_scan_start(
                {
                    "client": client,
                    "entity": main_group,
                    "group_id": "6000",
                    "group_name": "Restricted Group",
                    "account_name": "acct1",
                }
            )
        )

        self.assertGreaterEqual(self.processor.stats["non_fatal_errors"], 1)
        self.assertEqual(self.state.get_user_count(), 0)

    def test_on_scan_start_collects_admin_log_users_when_available(self):
        main_group = FakeChat(7000, "Admin Group")
        admin_message = FakeMessage(401, text="admin event message", sender_id=41, action=FakeAction(user_id=42))
        admin_event = FakeAdminEvent(
            user_id=40,
            new=admin_message,
        )
        client = FakeClient(
            entities={
                40: FakeUser(40, "admin_actor"),
                41: FakeUser(41, "event_sender"),
                42: FakeUser(42, "event_target"),
            },
            admin_logs_by_entity={7000: [admin_event]},
        )

        asyncio.run(
            self.processor.on_scan_start(
                {
                    "client": client,
                    "entity": main_group,
                    "group_id": "7000",
                    "group_name": "Admin Group",
                    "account_name": "acct1",
                }
            )
        )

        self.assertEqual(self.state.get_user_count(), 3)
        self.assertEqual(self.processor.stats["admin_events_processed"], 1)


if __name__ == "__main__":
    unittest.main()
