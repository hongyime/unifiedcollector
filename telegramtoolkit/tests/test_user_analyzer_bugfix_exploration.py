#!/usr/bin/env python3
"""
Bug Condition Exploration Tests for User Analyzer Efficiency Fix

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5**

CRITICAL: These tests MUST FAIL on unfixed code - failure confirms the bugs exist.
DO NOT attempt to fix the tests or the code when they fail.

These tests encode the expected behavior - they will validate the fixes when they pass after implementation.

GOAL: Surface counterexamples that demonstrate both bugs exist:
  - Bug 1: Message text contains "t.me/username" or "https://t.me/username" but _extract_raw_usernames does NOT extract the username
  - Bug 2: Multiple accounts resolve the same username via separate get_entity() API calls instead of using shared cache

The test assertions match the Expected Behavior Properties from design:
  - Property 1: All username patterns (@username, t.me/username, https://t.me/username) are extracted from message text
  - Property 2: Entity resolutions are cached in shared storage and reused across accounts
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from hypothesis import given, strategies as st, settings, Phase

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


class FakeMessage:
    def __init__(self, message_id, text="", sender_id=None):
        self.id = message_id
        self.text = text
        self.message = text
        self.raw_text = text
        self.sender_id = sender_id
        self.entities = []
        self.caption_entities = []
        self.action = None
        self.forward = None
        self.via_bot_id = None
        self.reply_to_msg_id = None

    async def get_reply_message(self):
        return None


class FakeClient:
    def __init__(self, entities=None):
        self.entities = entities or {}
        self.get_entity_calls = []

    async def get_entity(self, reference):
        # Track API calls for Bug 2 testing
        self.get_entity_calls.append(reference)
        
        if reference in self.entities:
            value = self.entities[reference]
        elif isinstance(reference, str) and reference.lstrip("@") in self.entities:
            value = self.entities[reference.lstrip("@")]
        else:
            raise ValueError(f"missing entity for {reference}")

        if isinstance(value, Exception):
            raise value
        return value


class BugConditionExplorationTests(unittest.TestCase):
    """
    Bug Condition Exploration Tests
    
    These tests are EXPECTED TO FAIL on unfixed code.
    Failure confirms the bugs exist and provides counterexamples.
    """

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

    # ========================================================================
    # Bug 1: Missing t.me/username Extraction
    # ========================================================================

    def test_bug1_tme_link_extraction_concrete(self):
        """
        **Property 1: Bug Condition** - Missing t.me/username Extraction
        
        EXPECTED: This test FAILS on unfixed code (confirms bug exists)
        
        Concrete test case: Message "Check out t.me/testuser" should extract "testuser"
        but unfixed code does NOT extract it.
        """
        # Concrete failing case from design document
        text = "Check out t.me/testuser"
        
        # Extract usernames using current implementation
        extracted = self.processor._extract_raw_usernames(text)
        
        # EXPECTED BEHAVIOR: Should extract @testuser from t.me/testuser link
        # This assertion will FAIL on unfixed code, confirming Bug 1 exists
        self.assertIn("@testuser", extracted, 
                     f"Bug 1 confirmed: t.me/testuser not extracted. Found: {extracted}")

    def test_bug1_https_tme_link_extraction_concrete(self):
        """
        **Property 1: Bug Condition** - Missing https://t.me/username Extraction
        
        EXPECTED: This test FAILS on unfixed code (confirms bug exists)
        
        Concrete test case: Message "Visit https://t.me/testuser" should extract "testuser"
        but unfixed code does NOT extract it.
        """
        # Concrete failing case from design document
        text = "Visit https://t.me/testuser"
        
        # Extract usernames using current implementation
        extracted = self.processor._extract_raw_usernames(text)
        
        # EXPECTED BEHAVIOR: Should extract @testuser from https://t.me/testuser link
        # This assertion will FAIL on unfixed code, confirming Bug 1 exists
        self.assertIn("@testuser", extracted,
                     f"Bug 1 confirmed: https://t.me/testuser not extracted. Found: {extracted}")

    @given(
        username=st.text(
            alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
            min_size=5,
            max_size=32
        ).filter(lambda x: x[0].isalpha())  # Telegram usernames must start with letter (ASCII only)
    )
    @settings(max_examples=50, phases=[Phase.generate, Phase.target])
    def test_bug1_tme_link_extraction_property(self, username):
        """
        **Property 1: Bug Condition** - Missing t.me/username Extraction (Property-Based)
        
        EXPECTED: This test FAILS on unfixed code (confirms bug exists)
        
        Property: For ANY valid username in t.me/username format, it should be extracted.
        Generates many test cases to find counterexamples.
        """
        text = f"Check out t.me/{username}"
        
        # Extract usernames using current implementation
        extracted = self.processor._extract_raw_usernames(text)
        
        # EXPECTED BEHAVIOR: Should extract username from t.me link
        # This assertion will FAIL on unfixed code, providing counterexamples
        self.assertIn(f"@{username}", extracted,
                     f"Bug 1 counterexample: t.me/{username} not extracted. Found: {extracted}")

    @given(
        username=st.text(
            alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
            min_size=5,
            max_size=32
        ).filter(lambda x: x[0].isalpha())
    )
    @settings(max_examples=50, phases=[Phase.generate, Phase.target])
    def test_bug1_https_tme_link_extraction_property(self, username):
        """
        **Property 1: Bug Condition** - Missing https://t.me/username Extraction (Property-Based)
        
        EXPECTED: This test FAILS on unfixed code (confirms bug exists)
        
        Property: For ANY valid username in https://t.me/username format, it should be extracted.
        Generates many test cases to find counterexamples.
        """
        text = f"Visit https://t.me/{username}"
        
        # Extract usernames using current implementation
        extracted = self.processor._extract_raw_usernames(text)
        
        # EXPECTED BEHAVIOR: Should extract username from https://t.me link
        # This assertion will FAIL on unfixed code, providing counterexamples
        self.assertIn(f"@{username}", extracted,
                     f"Bug 1 counterexample: https://t.me/{username} not extracted. Found: {extracted}")

    def test_bug1_mixed_patterns_concrete(self):
        """
        **Property 1: Bug Condition** - Mixed Username Patterns
        
        EXPECTED: This test FAILS on unfixed code (confirms bug exists)
        
        Tests message with multiple username patterns to verify all are extracted.
        """
        text = "Follow @alice, check t.me/bobby, and visit https://t.me/charlie"
        
        extracted = self.processor._extract_raw_usernames(text)
        
        # EXPECTED BEHAVIOR: All three usernames should be extracted
        # @alice works (existing functionality), but t.me patterns will FAIL
        self.assertIn("@alice", extracted, "Existing @username extraction should work")
        self.assertIn("@bobby", extracted, "Bug 1: t.me/bobby not extracted")
        self.assertIn("@charlie", extracted, "Bug 1: https://t.me/charlie not extracted")

    # ========================================================================
    # Bug 2: Redundant API Calls Across Accounts
    # ========================================================================

    def test_bug2_redundant_api_calls_concrete(self):
        """
        **Property 2: Bug Condition** - Redundant API Calls Across Accounts
        
        EXPECTED: This test FAILS on unfixed code (confirms bug exists)
        
        Concrete test: 4 accounts resolve same username → should make 1 API call (shared cache)
        but unfixed code makes 4 separate API calls (per-instance cache).
        """
        # Setup: Create 4 processor instances (simulating 4 accounts)
        processors = []
        clients = []
        
        for i in range(4):
            # Each processor has its own instance (simulating different accounts)
            processor = UserAnalyzerProcessor()
            processor.state = self.state
            processor.max_retries = 1
            processor.retry_delay = 0
            
            # Each client can resolve @testuser
            client = FakeClient(entities={"testuser": FakeUser(12345, "testuser")})
            
            processors.append(processor)
            clients.append(client)
        
        # Execute: Each account resolves the same username
        async def resolve_all():
            for i, (processor, client) in enumerate(zip(processors, clients)):
                processor._current_account_name = f"account{i}"
                processor._clients_map = {f"account{i}": client}
                processor._account_health = MagicMock()
                
                # Resolve @testuser
                entity = await processor._resolve_reference(client, "@testuser", source="test")
                self.assertIsNotNone(entity, f"Account {i} should resolve @testuser")
        
        asyncio.run(resolve_all())
        
        # Verify: Count total API calls across all clients
        total_api_calls = sum(len(client.get_entity_calls) for client in clients)
        
        # EXPECTED BEHAVIOR: Only 1 API call (first account), others use shared cache
        # This assertion will FAIL on unfixed code (will show 4 calls), confirming Bug 2 exists
        self.assertEqual(total_api_calls, 1,
                        f"Bug 2 confirmed: Expected 1 API call (shared cache), but got {total_api_calls} calls. "
                        f"Each account made separate API calls instead of using shared cache.")

    def test_bug2_cache_not_shared_across_instances(self):
        """
        **Property 2: Bug Condition** - Entity Cache Not Shared Across Instances
        
        EXPECTED: This test FAILS on unfixed code (confirms bug exists)
        
        Tests that entity cache is per-instance (in-memory only) and not shared.
        """
        # Create two processor instances
        processor1 = UserAnalyzerProcessor()
        processor1.state = self.state
        processor1.max_retries = 1
        processor1.retry_delay = 0
        processor1._current_account_name = "account1"
        processor1._account_health = MagicMock()
        
        processor2 = UserAnalyzerProcessor()
        processor2.state = self.state
        processor2.max_retries = 1
        processor2.retry_delay = 0
        processor2._current_account_name = "account2"
        processor2._account_health = MagicMock()
        
        # Create clients
        client1 = FakeClient(entities={"testuser": FakeUser(12345, "testuser")})
        client2 = FakeClient(entities={"testuser": FakeUser(12345, "testuser")})
        
        processor1._clients_map = {"account1": client1}
        processor2._clients_map = {"account2": client2}
        
        # Account 1 resolves @testuser (should make API call)
        async def test_sequence():
            entity1 = await processor1._resolve_reference(client1, "@testuser", source="test")
            self.assertIsNotNone(entity1)
            self.assertEqual(len(client1.get_entity_calls), 1, "First resolution should make API call")
            
            # Account 2 resolves same @testuser
            entity2 = await processor2._resolve_reference(client2, "@testuser", source="test")
            self.assertIsNotNone(entity2)
            
            # EXPECTED BEHAVIOR: Should use shared cache, no API call
            # This assertion will FAIL on unfixed code (will make API call), confirming Bug 2
            self.assertEqual(len(client2.get_entity_calls), 0,
                           f"Bug 2 confirmed: Second account made {len(client2.get_entity_calls)} API call(s). "
                           f"Should use shared cache instead of making redundant API call.")
        
        asyncio.run(test_sequence())

    @given(
        num_accounts=st.integers(min_value=2, max_value=10),
        username=st.text(
            alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
            min_size=5,
            max_size=32
        ).filter(lambda x: x[0].isalpha())
    )
    @settings(max_examples=20, phases=[Phase.generate, Phase.target])
    def test_bug2_redundant_api_calls_property(self, num_accounts, username):
        """
        **Property 2: Bug Condition** - Redundant API Calls Property-Based
        
        EXPECTED: This test FAILS on unfixed code (confirms bug exists)
        
        Property: For ANY number of accounts resolving the SAME username,
        only 1 API call should be made (shared cache).
        """
        # Clear the database cache for this example (Hypothesis runs multiple examples in same test)
        # This ensures each example starts with an empty cache
        self.state.conn.execute("DELETE FROM entity_cache")
        self.state.conn.commit()
        
        processors = []
        clients = []
        
        for i in range(num_accounts):
            processor = UserAnalyzerProcessor()
            processor.state = self.state
            processor.max_retries = 1
            processor.retry_delay = 0
            
            client = FakeClient(entities={username: FakeUser(12345, username)})
            
            processors.append(processor)
            clients.append(client)
        
        async def resolve_all():
            for i, (processor, client) in enumerate(zip(processors, clients)):
                processor._current_account_name = f"account{i}"
                processor._clients_map = {f"account{i}": client}
                processor._account_health = MagicMock()
                
                entity = await processor._resolve_reference(client, f"@{username}", source="test")
                self.assertIsNotNone(entity)
        
        asyncio.run(resolve_all())
        
        total_api_calls = sum(len(client.get_entity_calls) for client in clients)
        
        # EXPECTED BEHAVIOR: Only 1 API call regardless of number of accounts
        # This will FAIL on unfixed code, providing counterexamples
        self.assertEqual(total_api_calls, 1,
                        f"Bug 2 counterexample: {num_accounts} accounts resolving @{username} "
                        f"made {total_api_calls} API calls instead of 1 (shared cache).")


if __name__ == "__main__":
    unittest.main()
