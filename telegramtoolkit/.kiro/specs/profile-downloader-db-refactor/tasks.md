# Tasks: Profile Photo Downloader — DB-First Refactor

## Task 1 — Delete root-level draft file
- [x] Delete `REFACTORED_PROFILE_DOWNLOADER.py` from project root

## Task 2 — Refactor `download_profile_photos.py`
- [x] Remove all JSON/text-file tracking fields and imports (`json`, `csv` if unused)
- [x] Remove methods: `migrate_legacy_tracking`, `save_downloaded_photos`, `_mark_legacy_tracking_dirty`, `reconcile_tracking`, `_should_run_reconcile`, `_set_reconcile_stamp`, `_flush_hashes`, `load_downloaded_hashes`
- [x] Simplify `__init__`: remove JSON/hash-file setup, add `verify_files=False` param
- [x] Simplify `load_downloaded_photos`: DB only, no JSON fallback
- [x] Simplify `save_downloaded_photo`: DB + local set, no JSON
- [x] Simplify `save_hash`: `state.save_hash` + local set only
- [x] Simplify `is_photo_already_processed`: remove JSON fallback branches
- [x] Simplify `_setup_signal_handlers`: flush DB buffers only, print remaining count
- [x] Add `verify_files_on_disk(self) -> dict` method
- [x] Add `reset_profile_download_progress(self, user_ids=None) -> int` method
- [x] Add `_count_remaining_users(self) -> int` helper

## Task 3 — Wire reset into `manage_download_state.py`
- [x] Already handled by existing option 7 "Reset Profile-Photo Tracking" which calls `state.reset_profile_photo_tracking()`. The new `reset_profile_download_progress` on the downloader is available for direct use or future menu extension.

## Task 4 — Write tests in `tests/test_profile_downloader.py`
- [x] Test: `load_downloaded_photos` from DB
- [x] Test: `is_photo_already_processed` — DB hit + file exists
- [x] Test: `is_photo_already_processed` — DB hit + file missing → returns False
- [x] Test: `is_photo_already_processed` — hash hit → True + backfill
- [x] Test: `save_downloaded_photo` writes to DB
- [x] Test: `reset_profile_download_progress` full reset
- [x] Test: `reset_profile_download_progress` scoped reset
- [x] Test: `verify_files_on_disk` marks missing files as not downloaded
- [x] Test: graceful shutdown calls `state.flush_all_buffers`

## Task 5 — Validate
- [x] Run `pytest tests/test_profile_downloader.py -v` — 13 passed
- [x] Run full test suite `pytest --tb=short -q` — 173 passed, 0 regressions
- [x] Confirm `REFACTORED_PROFILE_DOWNLOADER.py` is gone from root
