from src.config import load_config


def test_load_config_reads_env_and_merges_provider_settings(tmp_path, monkeypatch):
    configs_dir = tmp_path / 'configs'
    configs_dir.mkdir()
    (tmp_path / '.env').write_text(
        '\n'.join([
            'TIKTOK_OUTPUT_ROOT=archive',
            'TIKTOK_LOG_LEVEL=DEBUG',
            'TIKTOK_COOKIES_FILE=configs/app_cookies.txt',
            'TIKTOK_DB_PATH=data/custom.db',
            'TIKTOK_RETRIES=9',
            'TIKTOK_TIMEOUT_SECONDS=123',
        ]),
        encoding='utf-8',
    )
    (configs_dir / 'providers.yaml').write_text(
        '\n'.join([
            'providers:',
            '  gallerydl:',
            '    retries: 7',
            '    timeout: 40',
        ]),
        encoding='utf-8',
    )

    monkeypatch.delenv('TIKTOK_OUTPUT_ROOT', raising=False)
    monkeypatch.delenv('TIKTOK_LOG_LEVEL', raising=False)
    monkeypatch.delenv('TIKTOK_COOKIES_FILE', raising=False)
    monkeypatch.delenv('TIKTOK_DB_PATH', raising=False)
    monkeypatch.delenv('TIKTOK_RETRIES', raising=False)
    monkeypatch.delenv('TIKTOK_TIMEOUT_SECONDS', raising=False)

    config = load_config(tmp_path)

    assert config.output_root == 'archive'
    assert config.log_level == 'DEBUG'
    assert config.tracker_db == 'data/custom.db'
    assert config.providers['gallerydl']['retries'] == 7
    assert config.providers['gallerydl']['timeout'] == 40
    assert config.providers['gallerydl']['timeout_seconds'] == 123
    assert config.providers['gallerydl']['cookies_file'] == 'configs/app_cookies.txt'


def test_load_config_keeps_provider_cookie_override(tmp_path, monkeypatch):
    configs_dir = tmp_path / 'configs'
    configs_dir.mkdir()
    (tmp_path / '.env').write_text(
        'TIKTOK_COOKIES_FILE=configs/app_cookies.txt\n',
        encoding='utf-8',
    )
    (configs_dir / 'providers.yaml').write_text(
        '\n'.join([
            'providers:',
            '  gallerydl:',
            '    cookies_file: configs/provider_cookies.txt',
        ]),
        encoding='utf-8',
    )

    monkeypatch.delenv('TIKTOK_COOKIES_FILE', raising=False)

    config = load_config(tmp_path)

    assert config.providers['gallerydl']['cookies_file'] == 'configs/provider_cookies.txt'
