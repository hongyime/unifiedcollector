# Requirements: Profile Photo Downloader — DB-First Refactor

## Background

`toolkit/managers/download_profile_photos.py` currently uses a mix of:
- `data/downloaded_profile_photos.json` — per-photo identifier tracking
- `data/downloaded_hashes.txt` — file-hash deduplication
- `data/profile_reconcile_state.json` — reconcile stamp
- In-memory sets that must be manually flushed

The DB schema already has the columns needed (`profile_photo_downloaded`, `profile_photo_last_checked`, `profile_photo_count` on `users`, and the `profile_photo_tracking` table). The `StateManager` already exposes `save_profile_photo`, `is_profile_photo_downloaded`, `mark_profile_photo_summary`, and `iter_users_for_profile_download`.

The goal is to remove all JSON/text-file tracking from the downloader and make SQLite the single source of truth, while keeping the download logic (account rotation, concurrency, folder structure, file hashing) intact.

## Requirements

### R1 — DB-only tracking
- All per-photo download state is stored in `profile_photo_tracking` (user_id, photo_id).
- Per-user summary (`profile_photo_downloaded`, `profile_photo_count`, `profile_photo_last_checked`) is updated on the `users` table via `state.mark_profile_photo_summary`.
- No writes to `downloaded_profile_photos.json` or `profile_reconcile_state.json`.
- Hash deduplication continues to use `StateManager.save_hash` / `hash_exists` (DB-backed). The legacy `.txt` file write is removed.

### R2 — File-existence re-download check
- On startup (or on-demand via reset), the downloader can verify that every `profile_photo_tracking` record with `downloaded = 1` still has a corresponding file on disk.
- Missing files are reset to `downloaded = 0` so they will be re-downloaded on the next run.
- This check must be fast: use the in-memory `user_folder_index` + `folder_photo_index` (already built from `os.scandir`) — no full-file hashing required unless explicitly requested.
- The check is triggered by a `verify_files` flag passed at construction time, or via the reset/manage menu.

### R3 — Progress reset
- A `reset_profile_download_progress(user_ids=None)` method on the downloader:
  - With no arguments: resets ALL users (`profile_photo_downloaded = 0`, clears `profile_photo_tracking`).
  - With a list of `user_ids`: resets only those users.
- Exposed in the existing `DownloadStateManager` menu (option 14 in main menu).

### R4 — Graceful exit
- Ctrl+C flushes all pending DB buffers via `state.flush_all_buffers()` and exits cleanly.
- No JSON files to flush — just the DB.
- A clear resume message is printed: how many users remain.

### R5 — Resume on restart
- On startup, the downloader queries `profile_photo_tracking` to build the in-memory `downloaded_photos` set (already done).
- Users with `profile_photo_downloaded = 1` are skipped unless the file-existence check (R2) marks them as missing.

### R6 — Remove dead code
- Remove `migrate_legacy_tracking`, `save_downloaded_photos`, `_mark_legacy_tracking_dirty`, `_legacy_json_dirty`, `_legacy_json_pending`, `_legacy_json_save_interval`, `legacy_photos_file`, `profile_photos_file` fields and all JSON write paths.
- Remove `reconcile_tracking`, `_should_run_reconcile`, `_set_reconcile_stamp`, `reconcile_stamp_file` (replaced by R2 file-existence check).
- Keep `_flush_hashes` removed; hash writes go through `state.save_hash` only.

### R7 — Root-level cleanup
- Delete `REFACTORED_PROFILE_DOWNLOADER.py` from the project root (it is already archived in `docs/archive/`).

### R8 — Tests
- Unit tests in `tests/test_profile_downloader.py` covering:
  - `is_photo_already_processed` — DB hit, DB miss, file missing after DB hit
  - `reset_profile_download_progress` — full reset and scoped reset
  - `verify_files_on_disk` — marks missing files as not downloaded
  - Graceful shutdown flushes DB buffers (mock `state.flush_all_buffers`)
  - `load_downloaded_photos` loads from DB correctly
- All tests use in-memory SQLite (via `StateManager(":memory:")`) — no real filesystem or Telegram calls.
