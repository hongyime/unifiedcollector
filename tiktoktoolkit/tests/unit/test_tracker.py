"""Tests for core/tracker.py — SQLite tracker, JSON backup, and FileLock."""

import json
import threading
import time
from pathlib import Path

import pytest

from src.tracker import (
    JSONBackup,
    SQLiteDownloadTracker,
    create_tracker,
    FileLock,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_tracker(tmp_path, compute_hash=False):
    db = tmp_path / 'tracker.sqlite'
    backup = tmp_path / 'backup.json'
    return create_tracker(db, backup, compute_hash=compute_hash)


# ── mark_downloaded / is_downloaded ──────────────────────────────────────────

def test_mark_and_is_downloaded(tmp_path):
    t = _make_tracker(tmp_path)
    assert not t.is_downloaded('alice', '111222')
    t.mark_downloaded('alice', '111222', filepath='/downloads/111222.mp4', size=1024)
    assert t.is_downloaded('alice', '111222')


def test_is_downloaded_different_user(tmp_path):
    t = _make_tracker(tmp_path)
    t.mark_downloaded('alice', '111222')
    assert not t.is_downloaded('bob', '111222')


def test_mark_downloaded_idempotent(tmp_path):
    t = _make_tracker(tmp_path)
    t.mark_downloaded('alice', '111222', filepath='/a.mp4', size=100)
    t.mark_downloaded('alice', '111222', filepath='/a.mp4', size=100)
    assert t.count_for_user('alice') == 1


def test_count_for_user(tmp_path):
    t = _make_tracker(tmp_path)
    for vid in ['1', '2', '3']:
        t.mark_downloaded('alice', vid)
    assert t.count_for_user('alice') == 3
    assert t.count_for_user('bob') == 0


# ── is_downloaded_in_folder ───────────────────────────────────────────────────

def test_is_downloaded_in_folder_match(tmp_path):
    t = _make_tracker(tmp_path)
    folder = str(tmp_path / 'downloads' / 'username_alice')
    filepath = str(tmp_path / 'downloads' / 'username_alice' / '2026-01-01' / '111.mp4')
    t.mark_downloaded('alice', '111', filepath=filepath)
    assert t.is_downloaded_in_folder('alice', '111', folder)


def test_is_downloaded_in_folder_no_match_different_folder(tmp_path):
    t = _make_tracker(tmp_path)
    filepath = str(tmp_path / 'downloads' / 'username_alice' / '111.mp4')
    t.mark_downloaded('alice', '111', filepath=filepath)
    other_folder = str(tmp_path / 'downloads' / 'username_bob')
    assert not t.is_downloaded_in_folder('alice', '111', other_folder)


def test_is_downloaded_in_folder_no_false_positive_substring(tmp_path):
    """username_alice should NOT match username_alice_backup."""
    t = _make_tracker(tmp_path)
    filepath = str(tmp_path / 'downloads' / 'username_alice_backup' / '111.mp4')
    t.mark_downloaded('alice', '111', filepath=filepath)
    alice_folder = str(tmp_path / 'downloads' / 'username_alice')
    assert not t.is_downloaded_in_folder('alice', '111', alice_folder)


def test_is_downloaded_in_folder_no_filepath(tmp_path):
    t = _make_tracker(tmp_path)
    t.mark_downloaded('alice', '111')  # no filepath
    assert not t.is_downloaded_in_folder('alice', '111', str(tmp_path))


# ── import_directory ──────────────────────────────────────────────────────────

def test_import_directory_registers_videos(tmp_path):
    user_dir = tmp_path / 'username_alice' / '2026-01-01'
    user_dir.mkdir(parents=True)
    (user_dir / '123456789.mp4').write_bytes(b'fake')
    (user_dir / '987654321.mp4').write_bytes(b'fake')

    t = _make_tracker(tmp_path)
    added = t.import_directory(tmp_path)
    assert added == 2
    assert t.is_downloaded('alice', '123456789')
    assert t.is_downloaded('alice', '987654321')


def test_import_directory_skips_non_video_files(tmp_path):
    user_dir = tmp_path / 'username_alice'
    user_dir.mkdir()
    (user_dir / 'readme.txt').write_text('hello')
    (user_dir / '123456.mp4').write_bytes(b'fake')

    t = _make_tracker(tmp_path)
    added = t.import_directory(tmp_path)
    assert added == 1


def test_import_directory_assume_username(tmp_path):
    (tmp_path / '111222333.mp4').write_bytes(b'fake')
    t = _make_tracker(tmp_path)
    added = t.import_directory(tmp_path, assume_username='charlie')
    assert added == 1
    assert t.is_downloaded('charlie', '111222333')


def test_import_directory_idempotent(tmp_path):
    user_dir = tmp_path / 'username_alice'
    user_dir.mkdir()
    (user_dir / '123456.mp4').write_bytes(b'fake')

    t = _make_tracker(tmp_path)
    first = t.import_directory(tmp_path)
    second = t.import_directory(tmp_path)
    assert first == 1
    assert second == 0  # already tracked


# ── JSON backup restore ───────────────────────────────────────────────────────

def test_json_backup_restored_into_empty_db(tmp_path):
    db = tmp_path / 'tracker.sqlite'
    backup_path = tmp_path / 'backup.json'

    # Seed the JSON backup manually
    backup_data = {
        'version': 1,
        'users': {
            'alice': {
                '999888': {
                    'first_downloaded': '2026-01-01T00:00:00Z',
                    'last_seen': '2026-01-01T00:00:00Z',
                    'filepath': '/some/path/999888.mp4',
                    'size': 512,
                }
            }
        }
    }
    backup_path.write_text(json.dumps(backup_data), encoding='utf-8')

    t = create_tracker(db, backup_path)
    assert t.is_downloaded('alice', '999888')


# ── vacuum ────────────────────────────────────────────────────────────────────

def test_vacuum_runs_without_error(tmp_path):
    t = _make_tracker(tmp_path)
    t.mark_downloaded('alice', '1')
    t.vacuum()  # should not raise


# ── FileLock ──────────────────────────────────────────────────────────────────

def test_filelock_exclusive(tmp_path):
    lock_path = tmp_path / 'test.lock'
    results = []

    def worker(idx):
        lock = FileLock(lock_path, timeout=5.0)
        with lock:
            results.append(f'start-{idx}')
            time.sleep(0.05)
            results.append(f'end-{idx}')

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    # Verify no interleaving: each start must be immediately followed by its end
    for i in range(0, len(results) - 1, 2):
        idx = results[i].split('-')[1]
        assert results[i + 1] == f'end-{idx}', f"Interleaved lock: {results}"


def test_filelock_timeout(tmp_path):
    lock_path = tmp_path / 'test.lock'
    outer = FileLock(lock_path, timeout=0.3)
    outer.acquire()
    try:
        inner = FileLock(lock_path, timeout=0.1)
        with pytest.raises(TimeoutError):
            inner.acquire()
    finally:
        outer.release()


def test_filelock_clears_stale_lock_from_dead_pid(tmp_path):
    """A lockfile containing a non-existent PID must be removed and the lock acquired."""
    lock_path = tmp_path / 'stale.lock'
    # Write a PID that is guaranteed not to exist
    lock_path.write_text('999999999', encoding='utf-8')

    lock = FileLock(lock_path, timeout=2.0)
    lock.acquire()
    try:
        assert lock_path.exists()
    finally:
        lock.release()
    assert not lock_path.exists()


# ── Concurrent mark_downloaded (threading.Lock on JSON backup) ────────────────

def test_concurrent_mark_downloaded_no_corruption(tmp_path):
    """Multiple threads calling mark_downloaded concurrently must not corrupt
    the SQLite DB or the JSON backup."""
    t = _make_tracker(tmp_path)
    errors = []

    def worker(vid_id):
        try:
            t.mark_downloaded('alice', str(vid_id), filepath=f'/fake/{vid_id}.mp4', size=vid_id * 100)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errors, f"Concurrent writes raised: {errors}"
    assert t.count_for_user('alice') == 20

    # JSON backup must be valid JSON
    backup_path = tmp_path / 'backup.json'
    data = json.loads(backup_path.read_text(encoding='utf-8'))
    assert len(data['users']['alice']) == 20
