import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# Add src to path so 'src.main' resolves, and root so main.py resolves
_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_root / 'src'))
sys.path.insert(0, str(_root))

import src.main as main_module


@patch.object(main_module, 'prompt_for_download_path', return_value='C:/tmp/lemon8')
def test_user_force_reruns_all_tracked_users_target_first(_mock_prompt):
    toolkit = MagicMock()
    toolkit.tracker.account_tracker.get_all_visited_users.return_value = ['alice', 'bryan', 'carol']

    with patch.object(main_module, 'Lemon8Toolkit', return_value=toolkit):
        with patch.object(sys, 'argv', ['main.py', 'user', 'bryan', '--download', '--force']):
            main_module.main()

    toolkit.scrape_user.assert_has_calls(
        [
            call('bryan', None, True, True, include_profile_photos=None),
            call('alice', None, True, True, include_profile_photos=None),
            call('carol', None, True, True, include_profile_photos=None),
        ]
    )
    assert toolkit.scrape_user.call_count == 3


@patch.object(main_module, 'prompt_for_download_path', return_value='C:/tmp/lemon8')
def test_user_force_with_empty_tracking_falls_back_to_single_target(_mock_prompt):
    toolkit = MagicMock()
    toolkit.tracker.account_tracker.get_all_visited_users.return_value = []

    with patch.object(main_module, 'Lemon8Toolkit', return_value=toolkit):
        with patch.object(sys, 'argv', ['main.py', 'user', 'bryan', '--download', '--force']):
            main_module.main()

    toolkit.scrape_user.assert_called_once_with(
        'bryan',
        None,
        True,
        True,
        include_profile_photos=None,
    )


@patch.object(main_module, 'prompt_for_download_path', return_value='C:/tmp/lemon8')
def test_user_force_without_explicit_username_runs_all_tracked_users(_mock_prompt):
    toolkit = MagicMock()
    toolkit.tracker.account_tracker.get_all_visited_users.return_value = ['alice', 'bob']

    with patch.object(main_module, 'Lemon8Toolkit', return_value=toolkit):
        with patch.object(sys, 'argv', ['main.py', 'user', '--download', '--force']):
            main_module.main()

    toolkit.scrape_user.assert_has_calls(
        [
            call('alice', None, True, True, include_profile_photos=None),
            call('bob', None, True, True, include_profile_photos=None),
        ]
    )
    assert toolkit.scrape_user.call_count == 2


@patch.object(main_module, 'prompt_for_download_path', return_value='C:/tmp/lemon8')
def test_tag_pages_are_forwarded_to_scrape_tag(_mock_prompt):
    toolkit = MagicMock()

    with patch.object(main_module, 'Lemon8Toolkit', return_value=toolkit):
        with patch.object(sys, 'argv', ['main.py', 'tag', 'fashion', '--pages', '3', '--download']):
            main_module.main()

    toolkit.scrape_tag.assert_called_once()
    args, kwargs = toolkit.scrape_tag.call_args
    assert args == ('fashion', True, False)
    assert kwargs == {'pages': 3}
