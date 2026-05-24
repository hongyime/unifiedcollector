"""Tests for config.py — duplicate detection and website management"""
import pytest
from unittest.mock import patch, MagicMock
from config import Config


@pytest.fixture
def isolated_config(tmp_path):
    """Config instance backed by a temp DB so tests don't touch production data."""
    db_path = str(tmp_path / "test.db")
    backup_dir = str(tmp_path)
    with patch("config.get_db_manager") as mock_get_db:
        from db_manager import DatabaseManager
        real_db = DatabaseManager(db_path, backup_dir)
        mock_get_db.return_value = real_db
        cfg = Config()
        # Remove default example_site placeholder so tests start clean
        cfg.websites = []
        yield cfg


# URL equivalence

@pytest.mark.parametrize("url1,url2", [
    ("https://example.com", "http://example.com"),
    ("https://www.google.com", "https://google.com"),
    ("https://github.com/", "https://github.com"),
])
def test_url_equivalence(isolated_config, url1, url2):
    assert isolated_config._urls_are_equivalent(url1, url2)


def test_distinct_urls_not_equivalent(isolated_config):
    assert not isolated_config._urls_are_equivalent("https://github.com", "https://gitlab.com")


# Duplicate prevention

def test_add_website_success(isolated_config):
    result = isolated_config.add_website("test_site", "https://www.test-example.com")
    assert result is True
    names = [w.get("name") if isinstance(w, dict) else w for w in isolated_config.websites]
    assert "test_site" in names


def test_add_website_duplicate_blocked(isolated_config):
    isolated_config.add_website("test_site", "https://www.test-example.com")
    result = isolated_config.add_website("test_site", "https://www.test-example.com")
    assert result is False


def test_add_website_www_variant_blocked(isolated_config):
    isolated_config.add_website("test_site", "https://www.test-example.com")
    result = isolated_config.add_website("test_site_www", "https://test-example.com")
    assert result is False


def test_remove_website(isolated_config):
    isolated_config.add_website("test_site", "https://www.test-example.com")
    result = isolated_config.remove_website("test_site")
    assert result is True
    assert all(
        (w.get("name") if isinstance(w, dict) else w) != "test_site"
        for w in isolated_config.websites
    )
