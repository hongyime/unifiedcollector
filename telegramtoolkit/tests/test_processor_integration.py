#!/usr/bin/env python3
"""
End-to-End Processor and Manager Integration Tests

Tests that all processors are registered correctly, accept proper dependencies,
and that standalone managers work with account injection.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from pathlib import Path


# Mark all tests in this module as end-to-end tests
pytestmark = pytest.mark.e2e


class TestProcessorRegistration:
    """Test that all processors are correctly registered"""

    def test_all_expected_processors_registered(self):
        """Test that all expected processors are in the registry"""
        from src.core.feature_registry import PROCESSOR_FEATURES
        
        # Updated for Phase 2: Only unified pipeline processors remain
        expected_keys = [
            "links",      # LinkCollectorProcessor
            "users",      # UserAnalyzerProcessor
            "media",      # MediaDownloaderProcessor
        ]
        
        for key in expected_keys:
            assert key in PROCESSOR_FEATURES, f"Processor '{key}' not registered"

    def test_processor_feature_key_consistency(self):
        """Test that processors have consistent feature keys"""
        from src.core.feature_registry import PROCESSOR_FEATURES
        from src.core.feature_processor import FeatureProcessor
        
        for key, definition in PROCESSOR_FEATURES.items():
            processor = definition.build_processor()
            
            # Verify processor is a FeatureProcessor
            assert isinstance(processor, FeatureProcessor), \
                f"Processor for '{key}' is not a FeatureProcessor"
            
            # Verify feature_key matches registry key
            assert processor.feature_key == key, \
                f"Processor feature_key '{processor.feature_key}' doesn't match registry key '{key}'"

    def test_processor_display_names(self):
        """Test that all processors have meaningful display names"""
        from src.core.feature_registry import PROCESSOR_FEATURES
        
        for key, definition in PROCESSOR_FEATURES.items():
            assert definition.display_name, \
                f"Processor '{key}' missing display_name"
            assert len(definition.display_name) > 0, \
                f"Processor '{key}' has empty display_name"

    def test_unified_processors_list(self):
        """Test processors marked for unified mode"""
        from src.core.feature_registry import PROCESSOR_FEATURES
        
        # All remaining processors should be in unified mode
        unified_processors = ["links", "users", "media"]
        
        for key in unified_processors:
            assert key in PROCESSOR_FEATURES
            assert PROCESSOR_FEATURES[key].include_in_unified, \
                f"Processor '{key}' should be included in unified mode"

    def test_only_unified_processors_in_registry(self):
        """Test that only unified processors remain in registry (Phase 2 fix)"""
        from src.core.feature_registry import PROCESSOR_FEATURES
        
        # Verify non-unified processors are NOT in registry
        non_unified_processors = ["join", "leave", "profiles", "photos"]
        
        for key in non_unified_processors:
            assert key not in PROCESSOR_FEATURES, \
                f"Processor '{key}' should NOT be in registry (Phase 2 removed standalone managers)"


class TestProcessorInstantiation:
    """Test that processors can be instantiated with dependencies"""

    def test_link_collector_processor_instantiation(self):
        """Test LinkCollectorProcessor can be instantiated"""
        from src.core.feature_registry import PROCESSOR_FEATURES
        
        definition = PROCESSOR_FEATURES["links"]
        processor = definition.build_processor()
        
        assert processor.name == "link_collector"
        assert processor.feature_key == "links"

    def test_media_processor_with_runtime_options(self):
        """Test MediaDownloaderProcessor accepts runtime options"""
        from src.core.feature_registry import PROCESSOR_FEATURES
        
        definition = PROCESSOR_FEATURES["media"]
        
        # Build with runtime options
        processor = definition.build_processor({"save_path": "custom_downloads"})
        
        assert processor.name == "media_downloader"
        assert processor.feature_key == "media"
        # Verify runtime option was applied
        if hasattr(processor, 'save_path'):
            assert processor.save_path == Path("custom_downloads")


class TestProcessorBuildFunctions:
    """Test the build_processors utility functions"""

    def test_build_all_processors_default(self):
        """Test build_processors creates all processors by default"""
        from src.core.feature_registry import build_processors, list_processor_feature_definitions
        
        processors = build_processors()
        
        # Should only get unified processors
        unified_count = len(list_processor_feature_definitions(include_in_unified_only=True))
        assert len(processors) == unified_count

    def test_build_targeted_processors(self):
        """Test build_processors creates specific processors"""
        from src.core.feature_registry import build_processors
        
        processors = build_processors(["links", "users"])
        
        assert len(processors) == 2
        assert processors[0].name == "link_collector"
        assert processors[1].name == "user_analyzer"

    def test_build_processors_with_runtime_options(self):
        """Test build_processors accepts runtime options"""
        from src.core.feature_registry import build_processors
        
        processors = build_processors(
            ["media"],
            runtime_options_by_key={"media": {"save_path": "test_downloads"}}
        )
        
        assert len(processors) == 1
        # Verify runtime option was applied
        if hasattr(processors[0], 'save_path'):
            assert processors[0].save_path == Path("test_downloads")


class TestManagerIntegration:
    """Test standalone managers work with account injection"""

    @pytest.mark.asyncio
    async def test_group_joiner_account_injection(self, mock_accounts):
        """Test GroupJoiner accepts account_dicts injection"""
        from src.managers.join_groups import GroupJoiner
        
        joiner = GroupJoiner()
        joiner.selected_accounts = mock_accounts
        
        # Verify accounts are set
        assert joiner.selected_accounts == mock_accounts
        assert len(joiner.selected_accounts) > 0

    @pytest.mark.asyncio
    async def test_group_cleaner_account_injection(self, mock_accounts):
        """Test GroupCleaner accepts account_dicts injection"""
        from src.managers.leave_groups import GroupCleaner
        
        cleaner = GroupCleaner()
        cleaner.selected_accounts = mock_accounts
        
        # Verify accounts are set
        assert cleaner.selected_accounts == mock_accounts
        assert len(cleaner.selected_accounts) > 0

    @pytest.mark.asyncio
    async def test_profile_photo_downloader_parallel_processor_injection(self):
        """Test ProfilePhotoDownloader accepts parallel_processor injection"""
        from src.managers.download_profile_photos import ProfilePhotoDownloader
        from src.core.parallel_processor import TelegramParallelProcessor
        
        # Create mock parallel processor
        parallel_processor = MagicMock(spec=TelegramParallelProcessor)
        parallel_processor.max_concurrent_per_account = 3
        parallel_processor.max_total_concurrent = 10
        
        downloader = ProfilePhotoDownloader(
            save_path="downloads/profiles",
            parallel_processor=parallel_processor
        )
        
        # Verify parallel_processor is set
        assert downloader.parallel_processor == parallel_processor

    @pytest.mark.asyncio
    async def test_photo_sender_instantiation(self):
        """Test that PhotoSender can be instantiated"""
        from src.managers.send_photos import PhotoSender
        
        # PhotoSender accepts no constructor arguments (Phase 2 fix)
        sender = PhotoSender()
        
        # Verify it's created
        assert sender is not None
        assert hasattr(sender, 'data_dir')


class TestOrchestratorIntegration:
    """Test MessageOrchestrator integration with processors"""

    def test_orchestrator_registers_processors(self):
        """Test orchestrator can register processors"""
        from src.core.message_orchestrator import MessageOrchestrator
        from src.core.feature_registry import build_processors
        
        orchestrator = MessageOrchestrator()
        processors = build_processors()
        
        # Register all processors
        for processor in processors:
            orchestrator.register_processor(processor)
        
        # Verify processors are registered
        assert len(orchestrator.processors) == len(processors)

    def test_orchestrator_gets_unified_start_message_id(self):
        """Test orchestrator calculates unified start message ID"""
        from src.core.message_orchestrator import MessageOrchestrator
        
        # Create orchestrator with mock state
        orchestrator = MessageOrchestrator()
        
        class MockState:
            def get_feature_progress_all(self, account_name, group_id):
                return {
                    "links": 100,
                    "users": 80,
                    "media": 90,
                }
        
        orchestrator.state = MockState()
        
        # Add mock processors
        class MockProcessor:
            def __init__(self, name, feature_key):
                self.name = name
                self.feature_key = feature_key
        
        orchestrator.processors = [
            MockProcessor("link_collector", "links"),
            MockProcessor("user_analyzer", "users"),
            MockProcessor("media_downloader", "media"),
        ]
        
        # Get unified start message ID
        start_id = orchestrator.get_unified_start_message_id("account1", "chat123")
        
        # Should be minimum of all progress (80)
        assert start_id == 80

    def test_orchestrator_gets_unified_progress_snapshot(self):
        """Test orchestrator gets progress snapshot"""
        from src.core.message_orchestrator import MessageOrchestrator
        
        orchestrator = MessageOrchestrator()
        
        class MockState:
            def get_feature_progress_all(self, account_name, group_id):
                return {
                    "links": 100,
                    "users": 80,
                }
        
        orchestrator.state = MockState()
        
        # Add mock processors
        class MockProcessor:
            def __init__(self, name, feature_key):
                self.name = name
                self.feature_key = feature_key
        
        orchestrator.processors = [
            MockProcessor("link_collector", "links"),
            MockProcessor("user_analyzer", "users"),
            MockProcessor("future_processor", "future_feature"),
        ]
        
        # Get progress snapshot
        snapshot = orchestrator.get_unified_progress_snapshot("account1", "chat123")
        
        # Should include all processors, defaulting missing ones to 0
        assert snapshot == {
            "links": 100,
            "users": 80,
            "future_feature": 0,
        }


class TestToolkitOrchestratorBuilding:
    """Test TelegramToolkit builds correct orchestrators"""

    def test_toolkit_builds_unified_orchestrator(self, mock_accounts):
        """Test toolkit builds unified orchestrator with all processors"""
        import main
        
        toolkit = main.TelegramToolkit()
        
        # Build unified orchestrator
        orchestrator = toolkit.build_unified_orchestrator()
        
        # Should have 3 unified processors (links, users, media)
        assert len(orchestrator.processors) == 3
        
        processor_names = [p.name for p in orchestrator.processors]
        assert "link_collector" in processor_names
        assert "user_analyzer" in processor_names
        assert "media_downloader" in processor_names

    def test_toolkit_builds_targeted_orchestrator(self, mock_accounts):
        """Test toolkit builds targeted orchestrator"""
        import main
        
        toolkit = main.TelegramToolkit()
        
        # Build targeted orchestrator
        orchestrator = toolkit.build_unified_orchestrator(["links", "users"])
        
        # Should have 2 processors
        assert len(orchestrator.processors) == 2
        
        processor_names = [p.name for p in orchestrator.processors]
        assert processor_names == ["link_collector", "user_analyzer"]

    def test_toolkit_builds_orchestrator_with_runtime_options(self, mock_accounts):
        """Test toolkit builds orchestrator with runtime options"""
        import main
        
        toolkit = main.TelegramToolkit()
        
        # Build orchestrator with runtime options
        orchestrator = toolkit.build_unified_orchestrator(
            processor_keys=["media"],
            runtime_options_by_key={"media": {"save_path": "custom_downloads"}}
        )
        
        # Should have 1 processor with custom save_path
        assert len(orchestrator.processors) == 1
        assert orchestrator.processors[0].name == "media_downloader"

    def test_toolkit_account_selection_returns_accounts(self):
        """Test toolkit account selection works correctly"""
        import main
        from src.core.parallel_processor import AccountManager
        
        # Mock available accounts
        with patch.object(AccountManager, 'get_available_accounts', return_value=['account1', 'account2']):
            with patch.object(AccountManager, 'get_accounts_by_names', return_value=[
                {'name': 'account1', 'phone': '+1234567890'},
                {'name': 'account2', 'phone': '+1234567891'},
            ]):
                with patch('builtins.input', return_value='1'):
                    toolkit = main.TelegramToolkit()
                    account_names, account_dicts = toolkit.select_accounts("test")
                    
                    # Verify accounts were returned
                    assert account_names == ['account1', 'account2']
                    assert len(account_dicts) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
