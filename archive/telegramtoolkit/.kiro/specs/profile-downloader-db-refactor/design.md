# Design: Profile Photo Downloader — DB-First Refactor

## What changes

### `toolkit/managers/download_profile_photos.py`

**Remove entirely:**
- `self.profile_photos_file` / `self.legacy_photos_file`
- `self.reconcile_stamp_file`, `self.reconcile_mode`, `self.reconcile_strategy`
- `self._legacy_json_dirty`, `self._legacy_json_pending`, `self._legacy_json_save_interval`
- `self._pending_hashes`, `self._save_interval`, `self.hashes_file`
- Methods: `migrate_legacy_tracking`, `save_downloaded_photos`, `_mark_legacy_tracking_dirty`, `reconcile_tracking`, `_should_run_reconcile`, `_set_reconcile_stamp`, `_flush_hashes`, `load_downloaded_hashes`

**Keep / simplify:**
- `self.downloaded_photos: set` — in-memory cache populated from DB on init, used for O(1) skip checks
- `self.downloaded_hashes: set` — populated from `state.get_all_hashes()` on init (or lazily)
- `_build_user_folder_index` — unchanged, used by file-existence check
- `_get_photo_filename_map` — unchanged
- `_iter_profile_files` — unchanged
- `file_hash` — unchanged
- `save_hash` — simplified: calls `state.save_hash(hash)` only, adds to local set
- `is_file_already_downloaded` — unchanged logic, uses local set
- `is_photo_already_processed` — simplified: DB check → file check, no JSON fallback
- `save_downloaded_photo` — simplified: DB write + local set, no JSON
- `load_downloaded_photos` — DB only, no JSON fallback
- All download/account-rotation methods — unchanged

**New methods:**
```python
def verify_files_on_disk(self) -> dict:
    """
    Fast file-existence check using the in-memory folder index.
    For every user with profile_photo_downloaded=1, checks if at least one
    profile_*.jpg exists in their folder. If not, resets their DB record.
    Returns {"checked": N, "missing": M, "reset": M}
    """

def reset_profile_download_progress(self, user_ids: list[int] | None = None) -> int:
    """
    Reset download tracking.
    - user_ids=None  → full reset (all users + all profile_photo_tracking rows)
    - user_ids=[...] → scoped reset for those users only
    Returns count of users reset.
    """
```

**`__init__` changes:**
```python
def __init__(self, save_path, parallel_processor=None, verify_files=False):
    ...
    # Load tracking from DB only
    self.load_downloaded_photos()
    self._build_user_folder_index()
    if verify_files:
        result = self.verify_files_on_disk()
        print(f"🔍 File check: {result['checked']} users, {result['missing']} missing → reset")
    self._setup_signal_handlers()
```

**Signal handler:**
```python
def _setup_signal_handlers(self):
    def graceful_shutdown(signum, frame):
        print("\n⚠️  Ctrl+C — saving state...")
        self.state.flush_all_buffers()
        remaining = self._count_remaining_users()
        print(f"✅ State saved. {remaining} users still pending.")
        print("💡 Run again to resume.")
        sys.exit(0)
    signal.signal(signal.SIGINT, graceful_shutdown)

def _count_remaining_users(self) -> int:
    try:
        row = self.state.conn.execute(
            "SELECT COUNT(*) as c FROM users WHERE COALESCE(is_bot,0)=0 AND COALESCE(profile_photo_downloaded,0)=0"
        ).fetchone()
        return row['c'] if row else 0
    except Exception:
        return 0
```

### `toolkit/managers/manage_download_state.py`

Add a new menu option: **"Reset profile photo download progress"**
- Calls `ProfilePhotoDownloader(save_path).reset_profile_download_progress()`
- Prompts for save_path (or uses a default)
- Optionally prompts for specific user IDs

### Root cleanup

Delete `REFACTORED_PROFILE_DOWNLOADER.py` from project root.

## Data flow (after refactor)

```
startup
  └─ load_downloaded_photos()
       └─ SELECT user_id, photo_id FROM profile_photo_tracking WHERE downloaded=1
            → self.downloaded_photos (set)

per-photo download
  └─ is_photo_already_processed(filepath, photo_identifier)
       1. photo_identifier in self.downloaded_photos  → skip
       2. state.is_profile_photo_downloaded(uid, pid) → skip + backfill cache
       3. file exists + hash in self.downloaded_hashes → skip + backfill
       4. proceed with download

  └─ after successful download
       save_downloaded_photo(photo_identifier)
         → state.save_profile_photo(uid, pid, downloaded=True)
         → self.downloaded_photos.add(...)
       save_hash(file_hash)
         → state.save_hash(hash)
         → self.downloaded_hashes.add(...)

  └─ after all photos for a user
       state.mark_profile_photo_summary(user_id, photos_downloaded=N)

Ctrl+C
  └─ state.flush_all_buffers()
  └─ print remaining count
  └─ sys.exit(0)
```

## Test design

File: `tests/test_profile_downloader.py`

Uses:
- `StateManager(":memory:")` singleton reset pattern (same as `test_state_manager.py`)
- `tmp_path` for fake download directories
- `unittest.mock.patch` for signal handlers

Key test cases:
1. `test_load_downloaded_photos_from_db` — inserts rows into `profile_photo_tracking`, asserts set is populated
2. `test_is_photo_already_processed_db_hit` — photo in DB + file exists → True
3. `test_is_photo_already_processed_file_missing` — photo in DB but file gone → False, DB reset
4. `test_is_photo_already_processed_hash_hit` — not in DB, file exists with known hash → True + backfill
5. `test_save_downloaded_photo_writes_db` — asserts `profile_photo_tracking` row inserted
6. `test_reset_full` — full reset clears all tracking rows and user summary columns
7. `test_reset_scoped` — scoped reset only touches specified user_ids
8. `test_verify_files_on_disk_marks_missing` — folder index has no files → resets DB record
9. `test_graceful_shutdown_flushes_buffers` — patches `state.flush_all_buffers`, triggers handler
