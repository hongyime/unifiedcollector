from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from src.cli import cli
from src.models import DownloadResult


def test_cli_help_shows_commands():
    runner = CliRunner()
    result = runner.invoke(cli, ['--help'])

    assert result.exit_code == 0
    assert 'download' in result.output
    assert 'utils' in result.output


def test_download_user_prompts_for_path_when_out_missing(monkeypatch, tmp_path):
    captured = {}

    def fake_load_config(_base_path):
        return SimpleNamespace(
            output_root='downloads',
            log_level='INFO',
            cookies_file='configs/tiktok_cookies.txt',
            cookies_browser=None,
            providers={},
        )

    class FakeDownloader:
        def __init__(self, _provider):
            pass

        def download_user(self, user, limit, output_dir, download_type='videos'):
            captured['user'] = user
            captured['limit'] = limit
            captured['output_dir'] = output_dir
            return [
                DownloadResult(
                    ok=True,
                    url='https://www.tiktok.com/@alice',
                    status='downloaded',
                    filepath=output_dir / '2026-03-26' / '123456.mp4',
                    meta={'video_id': '123456'},
                )
            ]

    def fake_prompt_for_download_path(**kwargs):
        captured['prompt_context'] = kwargs['context']
        return str(tmp_path)

    monkeypatch.setattr('src.cli.load_config', fake_load_config)
    monkeypatch.setattr('src.cli.setup_logging', lambda *_args, **_kwargs: SimpleNamespace(info=lambda *_a, **_k: None, error=lambda *_a, **_k: None))
    monkeypatch.setattr('src.cli.create_provider', lambda *_args, **_kwargs: SimpleNamespace(cookies_file=None))
    monkeypatch.setattr('src.cli.TikTokDownloader', FakeDownloader)
    monkeypatch.setattr('src.cli.prompt_for_download_path', fake_prompt_for_download_path)

    runner = CliRunner()
    result = runner.invoke(cli, ['download', 'user', '--user', 'alice'])

    if result.exception:
        import traceback
        traceback.print_exception(type(result.exception), result.exception, result.exception.__traceback__)

    assert result.exit_code == 0
    assert captured['prompt_context'] == 'TikTok videos from @alice'
    assert captured['output_dir'] == Path(tmp_path)


def test_download_user_renders_skipped_separately(monkeypatch, tmp_path):
    def fake_load_config(_base_path):
        return SimpleNamespace(
            output_root='downloads',
            log_level='INFO',
            cookies_file='configs/tiktok_cookies.txt',
            cookies_browser=None,
            providers={},
        )

    class FakeDownloader:
        def __init__(self, _provider):
            pass

        def download_user(self, user, limit, output_dir, download_type='videos'):
            return [
                DownloadResult(
                    ok=True,
                    url=f'https://www.tiktok.com/@{user}',
                    status='skipped',
                    reason='already tracked',
                )
            ]

    monkeypatch.setattr('src.cli.load_config', fake_load_config)
    monkeypatch.setattr('src.cli.setup_logging', lambda *_args, **_kwargs: SimpleNamespace(info=lambda *_a, **_k: None, error=lambda *_a, **_k: None))
    monkeypatch.setattr('src.cli.create_provider', lambda *_args, **_kwargs: SimpleNamespace(cookies_file=None))
    monkeypatch.setattr('src.cli.TikTokDownloader', FakeDownloader)
    monkeypatch.setattr('src.cli.prompt_for_download_path', lambda **_kwargs: str(tmp_path))

    runner = CliRunner()
    result = runner.invoke(cli, ['download', 'user', '--user', 'alice'])

    assert result.exit_code == 0
    assert 'Skipped 1 items for user @alice' in result.output
    assert 'already tracked' in result.output
    assert 'Failed 1 items' not in result.output
