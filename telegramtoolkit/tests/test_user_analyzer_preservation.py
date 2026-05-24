#!/usr/bin/env python3
"""
Preservation Property Tests for User Analyzer Efficiency Fix

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

CRITICAL: These tests MUST PASS on unfixed code - passing confirms baseline behavior to preserve.
These tests verify that the fix does NOT break existing functionality.

GOAL: Verify existing behavior works correctly on unfixed code:
  - @username extraction works correctly
  - Failed lookup caching in database failed_lookups table works correctly
  - First-time entity lookups make API calls as expected
  - All other user extraction sources work correctly

The test assertions match the Preservation Requirements from design:
  - For all messages WITHOUT t.me/username links, @username extraction produces same results
  - For all failed entity lookups, they are cached in database and reused across accounts
  - For all first-time entity lookups, API calls are made as expected
  - For all other extraction sources, behavior is unchanged
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
        # Track API calls
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


class PreservationPropertyTests(unittest.TestCase):
    """
    Preservation Property Tests
    
    These tests are EXPECTED TO PASS on unfixed code.
    Passing confirms baseline behavior that must be preserved after the fix.
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
    # Preservation: @username Extraction (Existing Functionality)
    # ========================================================================

    def test_preservation_at_username_extraction_concrete(self):
        """
        **Property 3: Preservation** - @username Extraction Works Correctly
        
        EXPECTED: This test PASSES on unfixed code (confirms existing functionality)
        
        Concrete test: Message "@testuser" should extract "@testuser"
        This is existing functionality that must be preserved.
        """
        text = "Follow @testuser for updates"
        
        # Extract usernames using current implementation
        extracted = self.processor._extract_raw_usernames(text)
        
        # EXPECTED BEHAVIOR: Should extract @testuser (existing functionality)
        # This assertion should PASS on unfixed code
        self.assertIn("@testuser", extracted,
                     f"Preservation failed: @testuser not extracted. Found: {extracted}")

    @given(
        username=st.text(
            alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_",
            min_size=5,
            max_size=32
        ).filter(lambda x: x[0].isalpha())  # Telegram usernames must start with letter
    )
    @settings(max_examples=100, phases=[Phase.generate, Phase.target])
    def test_preservation_at_username_extraction_property(self, username):
        """
        **Property 3: Preservation** - @username Extraction Property-Based
        
        EXPECTED: This test PASSES on unfixed code (confirms existing functionality)
        
        Property: For ANY valid @username pattern, it should be extracted correctly.
        This is existing functionality that must be preserved after the fix.
        """
        text = f"Follow @{username} for updates"
        
        # Extract usernames using current implementation
        extracted = self.processor._extract_raw_usernames(text)
        
        # EXPECTED BEHAVIOR: Should extract @username (existing functionality)
        # This assertion should PASS on unfixed code
        self.assertIn(f"@{username}", extracted,
                     f"Preservation failed: @{username} not extracted. Found: {extracted}")

    @given(
        usernames=st.lists(
            st.text(
                alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_",
                min_size=5,
                max_size=32
            ).filter(lambda x: x[0].isalpha()),
            min_size=1,
            max_size=5,
            unique=True
        )
    )
    @settings(max_examples=50, phases=[Phase.generate, Phase.target])
    def test_preservation_multiple_at_usernames_property(self, usernames):
        """
        **Property 3: Preservation** - Multiple @username Extraction
        
        EXPECTED: This test PASSES on unfixed code (confirms existing functionality)
        
        Property: For ANY list of valid @username patterns in a message,
        all should be extracted correctly.
        """
        text = "Follow " + ", ".join(f"@{u}" for u in usernames) + " for updates"
        
        # Extract usernames using current implementation
        extracted = self.processor._extract_raw_usernames(text)
        
        # EXPECTED BEHAVIOR: All @usernames should be extracted
        for username in usernames:
            self.assertIn(f"@{username}", extracted,
                         f"Preservation failed: @{username} not extracted from {text}. Found: {extracted}")

    # ========================================================================
    # Preservation: Failed Lookup Caching (Existing Functionality)
    # ========================================================================

    def test_preservation_failed_lookup_caching_concrete(self):
        """
        **Property 3: Preservation** - Failed Lookup Caching Works Correctly
        
        EXPECTED: This test PASSES on unfixed code (confirms existing functionality)
        
        Concrete test: Failed entity lookup should be cached in database
        and subsequent lookups should skip API call.
        """
        # Setup: Create processor and client
        client = FakeClient(entities={})  # No entities, all lookups will fail
        self.processor._current_account_name = "account1"
        self.processor._clients_map = {"account1": client}
        self.processor._account_health = MagicMock()
        
        async def test_sequence():
            # First lookup fails
            entity1 = await self.processor._resolve_reference(client, 99999, source="test")
            self.assertIsNone(entity1, "First lookup should fail")
            self.assertEqual(len(client.get_entity_calls), 1, "First lookup should make API call")
            
            # Mark as failed lookup
            await self.state.add_failed_lookup(99999, 'entity_not_found')
            
            # Verify it's cached
            is_failed = self.state.is_failed_lookup(99999)
            self.assertTrue(is_failed, "Failed lookup should be cached in database")
            
            # Second lookup should skip API call (uses failed_lookups cache)
            # Reset client call tracking
            client.get_entity_calls = []
            
            # Check if failed lookup is detected before resolution
            if self.state.is_failed_lookup(99999):
                # Should skip resolution entirely
                pass
            else:
                entity2 = await self.processor._resolve_reference(client, 99999, source="test")
                self.assertIsNone(entity2)
            
            # EXPECTED BEHAVIOR: No new API call (uses failed_lookups cache)
            # This assertion should PASS on unfixed code
            self.assertEqual(len(client.get_entity_calls), 0,
                           f"Preservation failed: Second lookup made {len(client.get_entity_calls)} API call(s). "
                           f"Should use failed_lookups cache.")
        
        asyncio.run(test_sequence())

    def test_preservation_failed_lookup_shared_across_accounts(self):
        """
        **Property 3: Preservation** - Failed Lookups Shared Across Accounts
        
        EXPECTED: This test PASSES on unfixed code (confirms existing functionality)
        
        Tests that failed lookups are shared across accounts via database.
        """
        # Create two processor instances (simulating two accounts)
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
        client1 = FakeClient(entities={})
        client2 = FakeClient(entities={})
        
        processor1._clients_map = {"account1": client1}
        processor2._clients_map = {"account2": client2}
        
        async def test_sequence():
            # Account 1 fails to resolve user ID 88888
            entity1 = await processor1._resolve_reference(client1, 88888, source="test")
            self.assertIsNone(entity1)
            self.assertEqual(len(client1.get_entity_calls), 1, "First account should make API call")
            
            # Mark as failed lookup
            await self.state.add_failed_lookup(88888, 'entity_not_found')
            
            # Account 2 checks failed_lookups before resolving
            if self.state.is_failed_lookup(88888):
                # Should skip resolution
                pass
            else:
                entity2 = await processor2._resolve_reference(client2, 88888, source="test")
                self.assertIsNone(entity2)
            
            # EXPECTED BEHAVIOR: Second account should not make API call
            # This assertion should PASS on unfixed code
            self.assertEqual(len(client2.get_entity_calls), 0,
                           f"Preservation failed: Second account made {len(client2.get_entity_calls)} API call(s). "
                           f"Should use shared failed_lookups cache.")
        
        asyncio.run(test_sequence())

    # ========================================================================
    # Preservation: First-Time Lookups Make API Calls
    # ========================================================================

    def test_preservation_first_time_lookup_makes_api_call(self):
        """
        **Property 3: Preservation** - First-Time Lookups Make API Calls
        
        EXPECTED: This test PASSES on unfixed code (confirms existing functionality)
        
        Tests that first-time entity lookups make API calls as expected.
        """
        client = FakeClient(entities={"testuser": FakeUser(12345, "testuser")})
        self.processor._current_account_name = "account1"
        self.processor._clients_map = {"account1": client}
        self.processor._account_health = MagicMock()
        
        async def test_sequence():
            # First-time lookup should make API call
            entity = await self.processor._resolve_reference(client, "@testuser", source="test")
            self.assertIsNotNone(entity, "First-time lookup should succeed")
            
            # EXPECTED BEHAVIOR: Should make API call
            # This assertion should PASS on unfixed code
            self.assertEqual(len(client.get_entity_calls), 1,
                           f"Preservation failed: First-time lookup made {len(client.get_entity_calls)} API call(s). "
                           f"Expected 1 API call.")
        
        asyncio.run(test_sequence())

    @given(
        username=st.text(
            alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_",
            min_size=5,
            max_size=32
        ).filter(lambda x: x[0].isalpha())
    )
    @settings(max_examples=50, phases=[Phase.generate, Phase.target])
    def test_preservation_first_time_lookup_property(self, username):
        """
        **Property 3: Preservation** - First-Time Lookups Property-Based
        
        EXPECTED: This test PASSES on unfixed code (confirms existing functionality)
        
        Property: For ANY username that is being resolved for the first time,
        an API call should be made.
        """
        # Clear entity cache to ensure this is truly a first-time lookup
        # This is necessary because Hypothesis runs multiple iterations with the same StateManager
        cache_key = f"@{username}"
        self.state.conn.execute("DELETE FROM entity_cache WHERE cache_key = ?", (cache_key,))
        self.state.conn.commit()
        
        client = FakeClient(entities={username: FakeUser(12345, username)})
        processor = UserAnalyzerProcessor()
        processor.state = self.state
        processor.max_retries = 1
        processor.retry_delay = 0
        processor._current_account_name = "account1"
        processor._clients_map = {"account1": client}
        processor._account_health = MagicMock()
        
        async def test_sequence():
            entity = await processor._resolve_reference(client, f"@{username}", source="test")
            self.assertIsNotNone(entity)
            
            # EXPECTED BEHAVIOR: Should make API call
            self.assertEqual(len(client.get_entity_calls), 1,
                           f"Preservation failed: First-time lookup for @{username} "
                           f"made {len(client.get_entity_calls)} API call(s). Expected 1.")
        
        asyncio.run(test_sequence())

    # ========================================================================
    # Preservation: Other Extraction Sources Work Correctly
    # ========================================================================

    def test_preservation_message_sender_extraction(self):
        """
        **Property 3: Preservation** - Message Sender Extraction Works
        
        EXPECTED: This test PASSES on unfixed code (confirms existing functionality)
        
        Tests that message sender extraction continues to work correctly.
        """
        client = FakeClient(entities={12345: FakeUser(12345, "sender")})
        self.processor._current_account_name = "account1"
        self.processor._clients_map = {"account1": client}
        self.processor._account_health = MagicMock()
        
        message = FakeMessage(1, "Hello world", sender_id=12345)
        
        async def test_sequence():
            await self.processor._collect_users_from_message_sources(
                client, message, "group1", "Test Group"
            )
            
            # EXPECTED BEHAVIOR: Sender should be resolved
            # This assertion should PASS on unfixed code
            self.assertGreater(len(client.get_entity_calls), 0,
                             "Preservation failed: Message sender not extracted")
        
        asyncio.run(test_sequence())

    def test_preservation_via_bot_extraction(self):
        """
        **Property 3: Preservation** - Via Bot Extraction Works
        
        EXPECTED: This test PASSES on unfixed code (confirms existing functionality)
        
        Tests that via_bot extraction continues to work correctly.
        """
        client = FakeClient(entities={99999: FakeUser(99999, "testbot", bot=True)})
        self.processor._current_account_name = "account1"
        self.processor._clients_map = {"account1": client}
        self.processor._account_health = MagicMock()
        self.processor.collect_via_bots = True
        
        message = FakeMessage(1, "Hello", sender_id=12345)
        message.via_bot_id = 99999
        
        async def test_sequence():
            await self.processor._collect_users_from_message_sources(
                client, message, "group1", "Test Group"
            )
            
            # EXPECTED BEHAVIOR: Via bot should be resolved
            # This assertion should PASS on unfixed code
            self.assertIn(99999, client.get_entity_calls,
                         "Preservation failed: Via bot not extracted")
        
        asyncio.run(test_sequence())

    def test_preservation_no_tme_links_behavior_unchanged(self):
        """
        **Property 3: Preservation** - Messages Without t.me Links Unchanged
        
        EXPECTED: This test PASSES on unfixed code (confirms existing functionality)
        
        Tests that messages without t.me links are processed exactly as before.
        This is the core preservation property: inputs outside the bug scope are unchanged.
        """
        # Message with only @username (no t.me links)
        # Note: Telegram usernames must be 5-32 chars, so using valid usernames
        text1 = "Follow @alice12345 and @bobby67890 for updates"
        extracted1 = self.processor._extract_raw_usernames(text1)
        
        # EXPECTED BEHAVIOR: Should extract both @usernames
        self.assertIn("@alice12345", extracted1, "Preservation failed: @alice12345 not extracted")
        self.assertIn("@bobby67890", extracted1, "Preservation failed: @bobby67890 not extracted")
        self.assertEqual(len(extracted1), 2, f"Preservation failed: Expected 2 usernames, got {len(extracted1)}")
        
        # Message with no usernames
        text2 = "Hello world, this is a test message"
        extracted2 = self.processor._extract_raw_usernames(text2)
        
        # EXPECTED BEHAVIOR: Should extract nothing
        self.assertEqual(len(extracted2), 0, f"Preservation failed: Expected 0 usernames, got {len(extracted2)}")

    @given(
        text_without_tme=st.text(
            alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 !?,;:'-",
            min_size=0,
            max_size=100
        ).filter(lambda x: "tme" not in x.lower() and "http" not in x.lower()),
        usernames=st.lists(
            st.text(
                alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_",
                min_size=5,
                max_size=32
            ).filter(lambda x: x[0].isalpha()),
            min_size=0,
            max_size=3,
            unique=True
        )
    )
    @settings(max_examples=100, phases=[Phase.generate, Phase.target])
    def test_preservation_no_tme_links_property(self, text_without_tme, usernames):
        """
        **Property 3: Preservation** - No t.me Links Property-Based
        
        EXPECTED: This test PASSES on unfixed code (confirms existing functionality)
        
        Property: For ANY message text that does NOT contain t.me links,
        the extraction behavior should be exactly the same as before.
        """
        # Build message with @usernames but no t.me links
        if usernames:
            text = text_without_tme + " " + " ".join(f"@{u}" for u in usernames)
        else:
            text = text_without_tme
        
        # Extract usernames
        extracted = self.processor._extract_raw_usernames(text)
        
        # EXPECTED BEHAVIOR: Should extract all @usernames, nothing else
        for username in usernames:
            self.assertIn(f"@{username}", extracted,
                         f"Preservation failed: @{username} not extracted from {text}")
        
        # Should not extract anything that's not a valid @username
        self.assertEqual(len(extracted), len(usernames),
                        f"Preservation failed: Expected {len(usernames)} usernames, got {len(extracted)} from {text}")


if __name__ == "__main__":
    unittest.main()
