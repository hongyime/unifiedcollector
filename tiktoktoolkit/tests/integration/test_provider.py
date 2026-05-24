import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.errors import ProviderError
from src.provider import GalleryDLProvider


def _completed(args, returncode=0, stdout='', stderr=''):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def test_tracker_initialization_failure_is_fatal(monkeypatch):
    def fake_run(args, **_kwargs):
        if '--version' in args:
            return _completed(args, stdout='1.31.0')
        if '--help' in args:
            return _completed(args, stdout='--list-urls')
        raise AssertionError(args)

    monkeypatch.setattr('src.provider.subprocess.run', fake_run)
    monkeypatch.setattr('src.provider.create_tracker', lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('bad tracker path')))

    with pytest.raises(ProviderError, match='download tracker'):
        GalleryDLProvider({'gallerydl': {'tracker_required': True}})


def test_build_gallery_dl_args_uses_effective_provider_config(monkeypatch, tmp_path):
    cookies_file = tmp_path / 'cookies.txt'
    cookies_file.write_text('cookie-data', encoding='utf-8')

    def fake_run(args, **_kwargs):
        if '--version' in args:
            return _completed(args, stdout='1.31.0')
        if '--help' in args:
            return _completed(args, stdout='--list-urls')
        raise AssertionError(args)

    monkeypatch.setattr('src.provider.subprocess.run', fake_run)
    monkeypatch.setattr('src.provider.create_tracker', lambda *_args, **_kwargs: SimpleNamespace())

    provider = GalleryDLProvider({
        'cookies_file': str(cookies_file),
        'gallerydl': {
            'retries': 5,
            'sleep': 2,
            'timeout': 30,
            'user_agent': 'UA',
            'proxy': 'http://proxy.local',
            'cookies_file': str(cookies_file),
        },
    })

    args = provider._build_gallery_dl_args('https://www.tiktok.com/@alice', tmp_path, 4)

    assert '--config' in args
    assert '--retries' in args and '5' in args
    assert '--sleep' in args and '2' in args
    assert '--user-agent' in args and 'UA' in args
    assert '--proxy' in args and 'http://proxy.local' in args
    assert '--range' in args and '1-4' in args
    assert '--cookies' in args and str(cookies_file) in args


def test_download_user_returns_skipped_result_when_tracker_precheck_is_zero(monkeypatch, tmp_path):
    def fake_run(args, **_kwargs):
        if '--version' in args:
            return _completed(args, stdout='1.31.0')
        if '--help' in args:
            return _completed(args, stdout='--list-urls')
        raise AssertionError(args)

    tracker = SimpleNamespace(count_for_user=lambda _username: 3)

    monkeypatch.setattr('src.provider.subprocess.run', fake_run)
    monkeypatch.setattr('src.provider.create_tracker', lambda *_args, **_kwargs: tracker)

    provider = GalleryDLProvider({'gallerydl': {}})
    monkeypatch.setattr(provider, '_tracker_precheck', lambda *_args, **_kwargs: 0)

    results = provider.download_user('alice', 5, tmp_path)

    assert len(results) == 1
    assert results[0].status == 'skipped'
    assert results[0].ok is True
    assert 'already downloaded' in results[0].reason


def test_run_gallery_dl_normalizes_files_into_date_layout(monkeypatch, tmp_path):
    target_dir = tmp_path / 'username_alice'
    nested_dir = target_dir / 'nested'
    nested_dir.mkdir(parents=True)
    original_file = nested_dir / '123456.mp4'
    original_file.write_text('video-data', encoding='utf-8')

    def fake_run(args, **_kwargs):
        if '--version' in args:
            return _completed(args, stdout='1.31.0')
        if '--help' in args:
            return _completed(args, stdout='--list-urls')
        return _completed(args)

    monkeypatch.setattr('src.provider.subprocess.run', fake_run)
    monkeypatch.setattr('src.provider.create_tracker', lambda *_args, **_kwargs: SimpleNamespace())

    provider = GalleryDLProvider({'gallerydl': {}})
    normalized_files, skipped = provider._run_gallery_dl('https://www.tiktok.com/@alice', target_dir, 1)

    assert len(normalized_files) == 1
    assert skipped == 0
    normalized = normalized_files[0]
    assert normalized.exists()
    # Flat layout — file goes directly in target_dir (no date subfolder)
    assert normalized.parent == target_dir.resolve()
    assert normalized.name == '123456.mp4'
    assert not original_file.exists()


def test_setup_browser_cookies_uses_supported_tiktok_profile_url(monkeypatch, tmp_path):
    cookies_file = tmp_path / 'tiktok_cookies.txt'

    def fake_run(args, **_kwargs):
        if '--version' in args:
            return _completed(args, stdout='1.31.10')
        if '--help' in args:
            return _completed(args, stdout='--list-urls')
        if '--cookies-export' in args:
            exported_path = Path(args[args.index('--cookies-export') + 1])
            exported_path.write_text('# Netscape HTTP Cookie File\n', encoding='utf-8')

            assert args[-1] == 'https://www.tiktok.com/@tiktok'
            assert args[-1] != 'https://www.tiktok.com/'
            return _completed(args)
        raise AssertionError(args)

    monkeypatch.setattr('src.provider.subprocess.run', fake_run)
    monkeypatch.setattr('src.provider.create_tracker', lambda *_args, **_kwargs: SimpleNamespace())

    provider = GalleryDLProvider({'gallerydl': {'cookies_file': str(cookies_file)}})
    monkeypatch.setattr('src.utils.secure_file_permissions', lambda *_args, **_kwargs: True)
    output = provider.setup_browser_cookies('chrome')

    assert output == cookies_file
    assert output.exists()
