"""Unit tests for downloader shutdown behavior."""

from pathlib import Path

from src import resilience
from src.downloader import TikTokDownloader
from src.models import DownloadResult


class _DummyProvider:
    def __init__(self):
        self.calls = []

    def download_user(self, username: str, limit: int, output_dir: Path, download_type: str = 'videos'):
        self.calls.append(username)
        if username == 'first':
            resilience.signal_shutdown()
        return [DownloadResult(ok=True, url=f'https://www.tiktok.com/@{username}', status='downloaded')]


def test_download_users_bulk_stops_when_shutdown_requested(tmp_path):
    resilience.reset_shutdown()
    provider = _DummyProvider()
    downloader = TikTokDownloader(provider)

    try:
        results = downloader.download_users_bulk(
            ['first', 'second', 'third'],
            limit_per_user=5,
            output_dir=tmp_path,
            download_type='videos',
        )
    finally:
        resilience.reset_shutdown()

    assert provider.calls == ['first']
    assert list(results.keys()) == ['first']
