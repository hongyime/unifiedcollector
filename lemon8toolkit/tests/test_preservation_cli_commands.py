"""
Preservation Property Tests - Existing CLI Commands

These tests verify that existing CLI commands remain unchanged after bugfixes.
They test the core functionality to ensure behavior is preserved.

EXPECTED OUTCOME ON UNFIXED CODE: Tests PASS (confirms baseline behavior)
After fixes are implemented, these tests should STILL PASS (confirms no regressions)

**Validates: Requirements 3.2 (Preservation)**
"""
import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest
from hypothesis import given, strategies as st, settings, HealthCheck

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@given(pages=st.integers(min_value=1, max_value=5))
@settings(max_examples=3, deadline=None)
def test_seed_functionality_preserved(pages):
    """
    Property: seed_from_feed functionality works correctly on unfixed code.
    This establishes baseline behavior to preserve.
    
    **Validates: Requirements 3.2**
    """
    from src.main import Lemon8Toolkit
    
    with patch('src.main.Lemon8Scraper') as MockScraper, \
         patch('src.main.MediaDownloader'), \
         patch('src.main.UnifiedTracker') as MockTracker, \
         patch('src.main.ProgressManager') as MockProgress:
        
        # Set up mocks
        mock_scraper = MockScraper.return_value
        mock_scraper.scrape_for_you_feed.return_value = {
            'discovered_users': ['user1', 'user2'],
            'discovered_tags': ['tag1']
        }
        mock_scraper.session = MagicMock()
        
        mock_tracker = MockTracker.return_value
        mock_tracker.account_tracker = MagicMock()
        mock_tracker.account_tracker.is_user_tracked.return_value = False
        mock_tracker.account_tracker.get_pending_spider_users.return_value = ['user1', 'user2']
        mock_tracker.save = MagicMock()
        
        mock_progress = MockProgress.return_value
        mock_progress.start_session.return_value = 'session_123'
        mock_progress.save = MagicMock()
        
        # Test seed functionality
        toolkit = Lemon8Toolkit(auto_save=False)
        toolkit.seed_from_feed(pages=pages, download_media=False)
        
        # Verify core behavior is preserved
        mock_scraper.scrape_for_you_feed.assert_called_once()
        mock_progress.start_session.assert_called_once()
        mock_progress.end_session.assert_called_once()


@given(batch_size=st.integers(min_value=1, max_value=3))
@settings(max_examples=3, deadline=None)
def test_spider_functionality_preserved(batch_size):
    """
    Property: spider_batch functionality works correctly on unfixed code.
    This establishes baseline behavior to preserve.
    
    **Validates: Requirements 3.2**
    """
    from src.main import Lemon8Toolkit
    
    with patch('src.main.Lemon8Scraper') as MockScraper, \
         patch('src.main.MediaDownloader'), \
         patch('src.main.UnifiedTracker') as MockTracker, \
         patch('src.main.ProgressManager') as MockProgress:
        
        # Set up mocks
        mock_scraper = MockScraper.return_value
        mock_scraper.scrape_user_profile.return_value = {
            'media_items': [],
            'related_users': [],
            'hashtags': [],
            'tag_ids': [],
            'user_info': {},
            'user_id': '123'
        }
        mock_scraper.session = MagicMock()
        
        mock_tracker = MockTracker.return_value
        mock_account_tracker = MagicMock()
        mock_account_tracker.get_pending_spider_users.return_value = [f'user{i}' for i in range(batch_size)]
        mock_account_tracker.is_user_visited.return_value = False
        mock_tracker.account_tracker = mock_account_tracker
        mock_tracker.save = MagicMock()
        
        mock_progress = MockProgress.return_value
        mock_progress.start_session.return_value = 'session_123'
        mock_progress.save = MagicMock()
        
        # Test spider functionality
        toolkit = Lemon8Toolkit(auto_save=False)
        toolkit.spider_batch(batch_size=batch_size, download_media=False)
        
        # Verify core behavior is preserved
        mock_account_tracker.reset_stuck_spiders.assert_called_once()
        mock_account_tracker.get_pending_spider_users.assert_called()


def test_graph_build_functionality_preserved():
    """
    Property: build_graph functionality works correctly on unfixed code.
    This establishes baseline behavior to preserve.
    
    **Validates: Requirements 3.2**
    """
    from src.main import Lemon8Toolkit
    
    with patch('src.main.Lemon8Scraper'), \
         patch('src.main.MediaDownloader'), \
         patch('src.main.UnifiedTracker'), \
         patch('src.main.ProgressManager'), \
         patch('src.main.GraphBuilder') as MockGraphBuilder:
        
        # Set up mocks
        mock_graph = MockGraphBuilder.return_value
        mock_graph.build_graph_from_users.return_value = {'follows': 10}
        mock_graph.get_graph_stats.return_value = {
            'total_edges': 10,
            'unique_nodes': 5,
            'edges_by_type': {},
            'top_sources': [],
            'top_targets': []
        }
        
        # Test graph build functionality
        toolkit = Lemon8Toolkit(auto_save=False)
        toolkit.build_graph(limit=None)
        
        # Verify core behavior is preserved
        mock_graph.build_graph_from_users.assert_called_once()
        mock_graph.get_graph_stats.assert_called_once()
        mock_graph.close.assert_called_once()


def test_graph_stats_functionality_preserved():
    """
    Property: show_graph_stats functionality works correctly on unfixed code.
    This establishes baseline behavior to preserve.
    
    **Validates: Requirements 3.2**
    """
    from src.main import Lemon8Toolkit
    
    with patch('src.main.Lemon8Scraper'), \
         patch('src.main.MediaDownloader'), \
         patch('src.main.UnifiedTracker'), \
         patch('src.main.ProgressManager'), \
         patch('src.main.GraphBuilder') as MockGraphBuilder:
        
        # Set up mocks
        mock_graph = MockGraphBuilder.return_value
        mock_graph.get_graph_stats.return_value = {
            'total_edges': 10,
            'unique_nodes': 5,
            'edges_by_type': {'follows': 10},
            'top_sources': [],
            'top_targets': []
        }
        
        # Test graph stats functionality
        toolkit = Lemon8Toolkit(auto_save=False)
        toolkit.show_graph_stats()
        
        # Verify core behavior is preserved
        mock_graph.get_graph_stats.assert_called_once()
        mock_graph.close.assert_called_once()


def test_stats_functionality_preserved():
    """
    Property: show_stats functionality works correctly on unfixed code.
    This establishes baseline behavior to preserve.
    
    **Validates: Requirements 3.2**
    """
    from src.main import Lemon8Toolkit
    
    with patch('src.main.Lemon8Scraper'), \
         patch('src.main.MediaDownloader') as MockDownloader, \
         patch('src.main.UnifiedTracker') as MockTracker, \
         patch('src.main.ProgressManager') as MockProgress:
        
        # Set up mocks
        mock_downloader = MockDownloader.return_value
        mock_downloader.get_stats.return_value = {'total_downloaded': 100}
        
        mock_tracker = MockTracker.return_value
        mock_tracker.get_combined_stats.return_value = {
            'accounts': {'total_visited_users': 50},
            'tags': {'total_processed_tags': 10}
        }
        
        mock_progress = MockProgress.return_value
        mock_progress.get_stats.return_value = {
            'total_sessions': 25,
            'completed_sessions': 20,
            'in_progress_sessions': 1,
            'total_media_scraped': 500,
            'total_media_downloaded': 450,
            'overall_success_rate': 90.0
        }
        mock_progress.get_current_session.return_value = None
        
        # Test stats functionality
        toolkit = Lemon8Toolkit(auto_save=False)
        toolkit.show_stats()
        
        # Verify core behavior is preserved
        mock_progress.get_stats.assert_called_once()
        mock_tracker.get_combined_stats.assert_called_once()
        mock_downloader.get_stats.assert_called_once()


def test_clear_functionality_preserved():
    """
    Property: clear_all functionality works correctly on unfixed code.
    This establishes baseline behavior to preserve.
    
    **Validates: Requirements 3.2**
    """
    from src.main import Lemon8Toolkit
    
    with patch('src.main.Lemon8Scraper'), \
         patch('src.main.MediaDownloader') as MockDownloader, \
         patch('src.main.UnifiedTracker') as MockTracker, \
         patch('src.main.ProgressManager') as MockProgress:
        
        # Set up mocks
        mock_downloader = MockDownloader.return_value
        mock_tracker = MockTracker.return_value
        mock_progress = MockProgress.return_value
        
        # Test clear functionality
        toolkit = Lemon8Toolkit(auto_save=False)
        toolkit.clear_all()
        
        # Verify core behavior is preserved
        mock_downloader.clear_download_history.assert_called_once()
        mock_tracker.clear_all_tracking.assert_called_once()
        mock_progress.clear_progress_history.assert_called_once()


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "--tb=short"])

