#!/usr/bin/env python3
import asyncio
import types
import unittest

from src.core.message_orchestrator import MessageOrchestrator
from src.core.scan_targets import cleanup_scan_target, discover_scan_targets


class FakeDialog:
    def __init__(self, entity, *, is_group=False, is_channel=False, is_user=False, name=None, title=None):
        self.entity = entity
        self.is_group = is_group
        self.is_channel = is_channel
        self.is_user = is_user
        self.name = name
        self.title = title or name


class FakeClient:
    def __init__(self, dialogs, entity_lookup):
        self._dialogs = dialogs
        self._entity_lookup = entity_lookup
        self.joined_ids = []
        self.left_ids = []
        self._connected = True

    async def get_entity(self, reference):
        return self._entity_lookup[reference]

    async def iter_dialogs(self):
        for dialog in self._dialogs:
            yield dialog

    def is_connected(self):
        return self._connected

    async def connect(self):
        self._connected = True

    async def disconnect(self):
        self._connected = False

    async def __call__(self, request):
        request_name = request.__class__.__name__
        if request_name == "GetParticipantsRequest":
            raise RuntimeError("not a participant")
        if request_name == "JoinChannelRequest":
            channel = getattr(request, "channel", None)
            self.joined_ids.append(getattr(channel, "id", None))
            return None
        if request_name == "LeaveChannelRequest":
            channel = getattr(request, "channel", None)
            self.left_ids.append(getattr(channel, "id", None))
            return None
        raise AssertionError(f"Unexpected request: {request_name}")


class ScanTargetDiscoveryTests(unittest.TestCase):
    def test_discover_scan_targets_prefers_linked_discussions_and_private_chats(self):
        linked_entity = types.SimpleNamespace(id=200, title="Announcements Chat")
        dialogs = [
            FakeDialog(
                types.SimpleNamespace(id=100, title="Announcements", linked_chat_id=200, megagroup=False),
                is_channel=True,
                name="Announcements",
            ),
            FakeDialog(
                types.SimpleNamespace(id=300, title="Community", megagroup=False),
                is_group=True,
                name="Community",
            ),
            FakeDialog(
                types.SimpleNamespace(id=400, first_name="Alice", bot=False),
                is_user=True,
                name="Alice",
            ),
        ]
        client = FakeClient(dialogs, {200: linked_entity})

        targets = asyncio.run(
            discover_scan_targets(
                client,
                include_private_chats=True,
                prefer_linked_discussions=True,
            )
        )

        self.assertEqual([target["group_id"] for target in targets], ["200", "300", "400"])
        self.assertEqual(client.joined_ids, [200])
        self.assertIn("[Discussion: Announcements Chat]", targets[0]["group_name"])

        asyncio.run(cleanup_scan_target(client, targets[0]))
        self.assertEqual(client.left_ids, [200])

    def test_discover_scan_targets_matches_original_channel_id_filter(self):
        linked_entity = types.SimpleNamespace(id=200, title="Announcements Chat")
        dialogs = [
            FakeDialog(
                types.SimpleNamespace(id=100, title="Announcements", linked_chat_id=200, megagroup=False),
                is_channel=True,
                name="Announcements",
            ),
            FakeDialog(
                types.SimpleNamespace(id=300, title="Community", megagroup=False),
                is_group=True,
                name="Community",
            ),
        ]
        client = FakeClient(dialogs, {200: linked_entity})

        targets = asyncio.run(
            discover_scan_targets(
                client,
                group_ids=["100"],
                include_private_chats=False,
                prefer_linked_discussions=True,
            )
        )

        self.assertEqual([target["group_id"] for target in targets], ["200"])


class OrchestratorTargetDiscoveryTests(unittest.TestCase):
    def test_scan_account_uses_processor_discovered_targets(self):
        cleanup_entity = types.SimpleNamespace(id=777)
        explicit_entity = types.SimpleNamespace(id=555, title="Explicit Target")
        fake_client = FakeClient([], {})
        scanned_targets = []

        class FakeProcessor:
            name = "fake_processor"
            feature_key = "fake"

            async def discover_scan_targets(self, client, account, group_ids=None):
                return [
                    {
                        "entity": explicit_entity,
                        "group_id": "555",
                        "group_name": "Explicit Target",
                        "scan_priority": 1,
                        "discovery_order": 0,
                        "cleanup_entities": [cleanup_entity],
                    }
                ]

        orchestrator = MessageOrchestrator.__new__(MessageOrchestrator)
        orchestrator.processors = [FakeProcessor()]
        orchestrator.stats = {
            "groups_scanned": 0,
            "messages_processed": 0,
            "processors_active": 1,
        }
        orchestrator.should_exit = False
        orchestrator.scan_group_delay_seconds = 0.0

        async def fake_scan_group(self, client, entity, group_id, group_name, account_name):
            scanned_targets.append((group_id, group_name, getattr(entity, "id", None), account_name))

        orchestrator.scan_group = types.MethodType(fake_scan_group, orchestrator)

        asyncio.run(orchestrator.scan_account(fake_client, {"name": "acct"}))

        self.assertEqual(scanned_targets, [("555", "Explicit Target", 555, "acct")])
        self.assertEqual(fake_client.left_ids, [777])


if __name__ == "__main__":
    unittest.main()
