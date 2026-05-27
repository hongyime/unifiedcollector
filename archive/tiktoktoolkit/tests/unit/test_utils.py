"""Tests for core/utils.py."""

import re
from pathlib import Path

import pytest

from src.utils import (
    build_output_path,
    extract_username_from_url,
    read_usernames_from_file,
    remove_empty_dirs,
    safe_filename,
)


# ── safe_filename ─────────────────────────────────────────────────────────────

def test_safe_filename_strips_special_chars():
    assert safe_filename('hello world!') == 'hello_world_'


def test_safe_filename_max_length():
    long = 'a' * 200
    assert len(safe_filename(long)) <= 120


# ── build_output_path ─────────────────────────────────────────────────────────

def test_build_output_path_creates_date_subdir(tmp_path):
    # Layout is now flat — files go directly in root, no date subfolder
    result = build_output_path(tmp_path, '123456', 'mp4')
    assert result.parent == tmp_path.resolve()
    assert result.name == '123456.mp4'


def test_build_output_path_collision_increments(tmp_path):
    first = build_output_path(tmp_path, '123456', 'mp4')
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(b'data')
    # Since the file exists, build_output_path returns the existing path (dedup)
    second = build_output_path(tmp_path, '123456', 'mp4')
    # Should return the same existing file (not a new _1 copy)
    assert second == first


def test_build_output_path_no_collision_if_same_existing(tmp_path):
    first = build_output_path(tmp_path, '123456', 'mp4')
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(b'data')
    # Passing existing_path=first should not increment
    result = build_output_path(tmp_path, '123456', 'mp4', existing_path=first)
    assert result == first


# ── extract_username_from_url ─────────────────────────────────────────────────

@pytest.mark.parametrize('url,expected', [
    ('https://www.tiktok.com/@alice', 'alice'),
    ('https://www.tiktok.com/@alice/video/123', 'alice'),
    ('https://tiktok.com/@bob.smith', 'bob.smith'),
    ('https://vm.tiktok.com/abc123', None),
    ('https://example.com/notiktok', None),
    ('', None),
])
def test_extract_username_from_url(url, expected):
    assert extract_username_from_url(url) == expected


# ── read_usernames_from_file ──────────────────────────────────────────────────

def test_read_usernames_strips_at_symbol(tmp_path):
    f = tmp_path / 'users.txt'
    f.write_text('@alice\nbob\n', encoding='utf-8')
    result = read_usernames_from_file(f)
    assert result == ['alice', 'bob']


def test_read_usernames_skips_comments(tmp_path):
    f = tmp_path / 'users.txt'
    f.write_text('# comment\nalice\n', encoding='utf-8')
    result = read_usernames_from_file(f)
    assert result == ['alice']


def test_read_usernames_skips_empty_lines(tmp_path):
    f = tmp_path / 'users.txt'
    f.write_text('\nalice\n\nbob\n', encoding='utf-8')
    result = read_usernames_from_file(f)
    assert result == ['alice', 'bob']


def test_read_usernames_allows_hyphens(tmp_path):
    f = tmp_path / 'users.txt'
    f.write_text('user-name\n', encoding='utf-8')
    result = read_usernames_from_file(f)
    assert result == ['user-name']


def test_read_usernames_file_not_found():
    with pytest.raises(FileNotFoundError):
        read_usernames_from_file('/nonexistent/path/users.txt')


# ── remove_empty_dirs ─────────────────────────────────────────────────────────

def test_remove_empty_dirs_removes_nested_empty(tmp_path):
    empty = tmp_path / 'a' / 'b' / 'c'
    empty.mkdir(parents=True)
    remove_empty_dirs(tmp_path)
    assert not (tmp_path / 'a').exists()


def test_remove_empty_dirs_keeps_dirs_with_files(tmp_path):
    d = tmp_path / 'a'
    d.mkdir()
    (d / 'file.mp4').write_bytes(b'data')
    remove_empty_dirs(tmp_path)
    assert d.exists()
    assert (d / 'file.mp4').exists()


def test_remove_empty_dirs_noop_on_file(tmp_path):
    f = tmp_path / 'file.txt'
    f.write_text('hello')
    remove_empty_dirs(f)  # should not raise
    assert f.exists()
