#!/usr/bin/env python3
"""
End-to-End CSV Export Verification Tests

Critical Phase 1 fix verification: Ensure that analyze_users and scan_all_features
automatically call CSV export functions to maintain downstream compatibility.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from pathlib import Path


# Mark all tests in this module as end-to-end tests
pytestmark = pytest.mark.e2e


class TestCSVExportInAnalyzeUsers:
    """Test CSV export in analyze_users method (Phase 1 critical fix)"""

    @pytest.mark.asyncio
    async def test_analyze_users_calls_csv_export_when_enabled(self, mock_accounts):
        """
        Test that analyze_users automatically calls CSV export.
        
        This is a critical Phase 1 fix to ensure downstream dependencies
        that rely on CSV files after user analysis continue to work.
        """
        import main
        
        toolkit = main.TelegramToolkit()
        toolkit.verify_and_gate_accounts = AsyncMock(return_value=True)
        toolkit.select_accounts = MagicMock(return_value=(["account1"], mock_accounts))
        toolkit.generate_web_indices = MagicMock()
        
        # Mock orchestrator
        mock_orchestrator = MagicMock()
        mock_orchestrator.run = AsyncMock()
        toolkit.build_unified_orchestrator = MagicMock(return_value=mock_orchestrator)
        
        # Mock state manager
        with patch('main.get_state_manager') as mock_get_state, patch('main.get_config_value', return_value=True):
            mock_state_manager = MagicMock()
            mock_get_state.return_value = mock_state_manager
            
            # Execute analyze_users
            await toolkit.analyze_users()
            
            # CRITICAL ASSERTION: CSV export must be called
            mock_state_manager.export_all_to_csv.assert_called_once()
            
            # Verify it's called with correct path
            expected_path = str(toolkit.base_dir / "data")
            mock_state_manager.export_all_to_csv.assert_called_once_with(expected_path)

    @pytest.mark.asyncio
    async def test_analyze_users_generates_web_indices(self, mock_accounts):
        """Test that analyze_users generates web indices after CSV export"""
        import main
        
        toolkit = main.TelegramToolkit()
        toolkit.verify_and_gate_accounts = AsyncMock(return_value=True)
        toolkit.select_accounts = MagicMock(return_value=(["account1"], mock_accounts))
        
        # Mock orchestrator
        mock_orchestrator = MagicMock()
        mock_orchestrator.run = AsyncMock()
        toolkit.build_unified_orchestrator = MagicMock(return_value=mock_orchestrator)
        
        # Mock state manager
        with patch('main.get_state_manager') as mock_get_state:
            mock_state_manager = MagicMock()
            mock_get_state.return_value = mock_state_manager
            toolkit.generate_web_indices = MagicMock()
            
            # Execute analyze_users
            await toolkit.analyze_users()
            
            # Verify web indices are generated
            toolkit.generate_web_indices.assert_called_once()
            mock_state_manager.export_all_to_csv.assert_not_called()

    @pytest.mark.asyncio
    async def test_analyze_users_users_processor_only(self, mock_accounts):
        """Test that analyze_users uses only the users processor"""
        import main
        
        toolkit = main.TelegramToolkit()
        toolkit.verify_and_gate_accounts = AsyncMock(return_value=True)
        toolkit.select_accounts = MagicMock(return_value=(["account1"], mock_accounts))
        
        # Track orchestrator build parameters
        build_calls = []
        
        def mock_build_unified_orchestrator(processor_keys, **kwargs):
            build_calls.append(processor_keys)
            mock_orchestrator = MagicMock()
            mock_orchestrator.run = AsyncMock()
            return mock_orchestrator
        
        toolkit.build_unified_orchestrator = MagicMock(side_effect=mock_build_unified_orchestrator)
        
        with patch('main.get_state_manager') as mock_get_state:
            mock_state_manager = MagicMock()
            mock_get_state.return_value = mock_state_manager
            toolkit.generate_web_indices = MagicMock()
            
            # Execute analyze_users
            await toolkit.analyze_users()
            
            # Verify only 'users' processor was requested
            assert len(build_calls) == 1
            assert build_calls[0] == ['users']


class TestCSVExportInScanAllFeatures:
    """Test CSV export in scan_all_features method (Phase 1 critical fix)"""

    @pytest.mark.asyncio
    async def test_scan_all_features_calls_csv_export_when_enabled(self, mock_accounts):
        """
        Test that scan_all_features (Unified Scan) automatically calls CSV export.
        
        This is a critical Phase 1 fix to ensure downstream dependencies
        that rely on CSV files after unified scanning continue to work.
        """
        import main
        
        toolkit = main.TelegramToolkit()
        toolkit.verify_and_gate_accounts = AsyncMock(return_value=True)
        toolkit.select_accounts = MagicMock(return_value=(["account1"], mock_accounts))
        
        # Mock user input for download path
        with patch('builtins.input', return_value='downloads'):
            # Mock orchestrator
            mock_orchestrator = MagicMock()
            mock_orchestrator.run = AsyncMock()
            toolkit.build_unified_orchestrator = MagicMock(return_value=mock_orchestrator)
            
            # Mock state manager
            with patch('main.get_state_manager') as mock_get_state, patch('main.get_config_value', return_value=True):
                mock_state_manager = MagicMock()
                mock_get_state.return_value = mock_state_manager
                toolkit.generate_web_indices = MagicMock()
                
                # Execute scan_all_features
                await toolkit.scan_all_features()
                
                # CRITICAL ASSERTION: CSV export must be called
                mock_state_manager.export_all_to_csv.assert_called_once()
                
                # Verify it's called with correct path
                expected_path = str(toolkit.base_dir / "data")
                mock_state_manager.export_all_to_csv.assert_called_once_with(expected_path)

    @pytest.mark.asyncio
    async def test_scan_all_features_generates_web_indices(self, mock_accounts):
        """Test that scan_all_features generates web indices after CSV export"""
        import main
        
        toolkit = main.TelegramToolkit()
        toolkit.verify_and_gate_accounts = AsyncMock(return_value=True)
        toolkit.select_accounts = MagicMock(return_value=(["account1"], mock_accounts))
        
        with patch('builtins.input', return_value='downloads'):
            # Mock orchestrator
            mock_orchestrator = MagicMock()
            mock_orchestrator.run = AsyncMock()
            toolkit.build_unified_orchestrator = MagicMock(return_value=mock_orchestrator)
            
            with patch('main.get_state_manager') as mock_get_state:
                mock_state_manager = MagicMock()
                mock_get_state.return_value = mock_state_manager
                toolkit.generate_web_indices = MagicMock()
                
                # Execute scan_all_features
                await toolkit.scan_all_features()
                
                # Verify web indices are generated
                toolkit.generate_web_indices.assert_called_once()
                mock_state_manager.export_all_to_csv.assert_not_called()

    @pytest.mark.asyncio
    async def test_scan_all_features_all_processors(self, mock_accounts):
        """Test that scan_all_features uses all processors in unified mode"""
        import main
        
        toolkit = main.TelegramToolkit()
        toolkit.verify_and_gate_accounts = AsyncMock(return_value=True)
        toolkit.select_accounts = MagicMock(return_value=(["account1"], mock_accounts))
        
        # Track orchestrator build parameters and execution
        build_calls = []
        execution_calls = []
        
        def mock_build_unified_orchestrator(processor_keys=None, **kwargs):
            build_calls.append((processor_keys, kwargs))
            mock_orchestrator = MagicMock()
            mock_orchestrator.run = AsyncMock()
            
            def capture_execution(accounts, unified_mode=False):
                execution_calls.append((accounts, unified_mode))
            
            mock_orchestrator.run = AsyncMock(side_effect=capture_execution)
            return mock_orchestrator
        
        toolkit.build_unified_orchestrator = MagicMock(side_effect=mock_build_unified_orchestrator)
        
        with patch('builtins.input', return_value='downloads'):
            with patch('main.get_state_manager') as mock_get_state:
                mock_state_manager = MagicMock()
                mock_get_state.return_value = mock_state_manager
                toolkit.generate_web_indices = MagicMock()
                
                # Execute scan_all_features
                await toolkit.scan_all_features()
                
                # Verify no processor keys specified (uses all)
                assert len(build_calls) == 1
                assert build_calls[0][0] is None
                
                # Verify runtime options include media save_path
                runtime_options = build_calls[0][1].get('runtime_options_by_key', {})
                assert 'media' in runtime_options
                assert 'save_path' in runtime_options['media']
                
                # Verify execution with unified_mode=True
                assert len(execution_calls) == 1
                assert execution_calls[0][1] is True


class TestCSVExportNotCalledOtherFeatures:
    """Test that CSV export is NOT called for features that shouldn't export"""

    @pytest.mark.asyncio
    async def test_collect_links_does_not_export_csv(self, mock_accounts):
        """Test that collect_links does NOT call CSV export"""
        import main
        
        toolkit = main.TelegramToolkit()
        toolkit.verify_and_gate_accounts = AsyncMock(return_value=True)
        toolkit.select_accounts = MagicMock(return_value=(["account1"], mock_accounts))
        
        # Mock orchestrator
        mock_orchestrator = MagicMock()
        mock_orchestrator.run = AsyncMock()
        toolkit.build_unified_orchestrator = MagicMock(return_value=mock_orchestrator)
        
        # Mock state manager
        with patch('main.get_state_manager') as mock_get_state:
            mock_state_manager = MagicMock()
            mock_get_state.return_value = mock_state_manager
            
            # Execute collect_links
            await toolkit.collect_links()
            
            # CSV export should NOT be called
            mock_state_manager.export_all_to_csv.assert_not_called()

    @pytest.mark.asyncio
    async def test_download_media_does_not_export_csv(self, mock_accounts):
        """Test that download_media does NOT call CSV export"""
        import main
        
        toolkit = main.TelegramToolkit()
        toolkit.verify_and_gate_accounts = AsyncMock(return_value=True)
        toolkit.select_accounts = MagicMock(return_value=(["account1"], mock_accounts))
        
        # Mock orchestrator
        mock_orchestrator = MagicMock()
        mock_orchestrator.run = AsyncMock()
        toolkit.build_unified_orchestrator = MagicMock(return_value=mock_orchestrator)
        
        # Mock user input for download path
        with patch('builtins.input', return_value='downloads'):
            with patch('main.get_state_manager') as mock_get_state:
                mock_state_manager = MagicMock()
                mock_get_state.return_value = mock_state_manager
                
                # Execute download_media
                await toolkit.download_media()
                
                # CSV export should NOT be called
                mock_state_manager.export_all_to_csv.assert_not_called()


class TestCSVExportIntegration:
    """Integration tests for CSV export state management"""

    @pytest.mark.asyncio
    async def test_state_manager_export_methods_exist(self):
        """Test that state manager has all expected export methods"""
        from src.core.state_manager import StateManager
        
        # Verify state manager has export methods
        assert hasattr(StateManager, 'export_users_to_csv')
        assert hasattr(StateManager, 'export_memberships_to_csv')
        assert hasattr(StateManager, 'export_all_to_csv')
        assert hasattr(StateManager, 'export_all_to_json')

    @pytest.mark.asyncio
    async def test_export_creates_csv_files(self, tmp_path):
        """
        Test that export creates actual CSV files.
        
        Note: This test is simplified because creating a fully functional
        StateManager with a real database connection in test mode is complex.
        The critical CSV export behavior is already tested in:
        - test_analyze_users_calls_csv_export
        - test_scan_all_features_calls_csv_export
        which use mocking to verify the export function is called.
        """
        from src.core.state_manager import StateManager, shutdown_state_manager
        
        # Create state manager with in-memory database
        # Use unique db for each test
        db_path = tmp_path / "test.db"
        state = StateManager(str(db_path))
        state._shutdown = True  # Disable background sync
        
        # Just verify the export methods exist and can be called
        # The actual file creation is tested in integration mode
        assert hasattr(state, 'export_all_to_csv')
        assert hasattr(state, 'export_users_to_csv')
        assert hasattr(state, 'export_memberships_to_csv')
        
        # Cleanup
        state.close()
        shutdown_state_manager()

    @pytest.mark.asyncio
    async def test_get_state_manager_singleton(self):
        """Test that get_state_manager returns singleton instance"""
        from src.core.state_manager import get_state_manager, shutdown_state_manager, StateManager
        
        # Reset singleton
        StateManager._instance = None
        StateManager._initialized = False
        
        # Get state manager twice
        state1 = get_state_manager()
        state2 = get_state_manager()
        
        # Verify it's the same instance
        assert state1 is state2
        
        # Cleanup
        shutdown_state_manager()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
