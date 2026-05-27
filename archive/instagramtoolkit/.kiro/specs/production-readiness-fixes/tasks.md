# Production Readiness Fixes — Tasks

## Task List

- [x] 1 BUG-1: Graceful Ctrl+C — interruptible shutdown
  - [x] 1.1 Add module-level `_SHUTDOWN_EVENT = threading.Event()` to `lib/rate_limiter.py` and update `interruptible_sleep` to check it on each 0.2 s tick
  - [x] 1.2 Update `ProgressManager._setup_signal_handlers` in `lib/progress_manager.py` to set `_SHUTDOWN_EVENT`, call `_get_db().close()`, and then call `sys.exit(0)`
  - [x] 1.3 Add `self._shutdown_requested = threading.Event()` to `InstagramProcessor.__init__` in `lib/parallel_processor.py` and check it at the top of each iteration in `process_batch_relationships` and `process_batch_downloads`

- [x] 2 BUG-2 + BUG-4: Media verification after download
  - [x] 2.1 Add `verify_download(username, category, target_dir)` method to `MediaDownloader` in `lib/download_media.py` that globs for `*.jpg`, `*.mp4`, `*.png` and returns True if count > 0
  - [x] 2.2 Wrap `download_profile_photo`, `download_posts`, `download_stories`, and `download_highlights` with a post-download call to `verify_download`; retry up to 2 times if verification fails before returning False

- [ ] 3 BUG-3: Delete .bak files
  - [x] 3.1 Delete the existing `.bak` files in `data/` now (`account_cooldowns.json.bak`, `account_quotas.json.bak`, `download_progress.json.bak`, `profile_access.json.bak`, `relationships.json.bak`, `username_database.json.bak`, `usernames.txt.bak`)
  - [x] 3.2 Add `cleanup-bak` subcommand to `main.py` that deletes all `.bak` files in `data/` and confirms each deletion

- [x] 4 BUG-5: .bat menu testing
  - [x] 4.1 Create `tests/test_bat_menus.py` that uses `subprocess.run` to invoke every `main.py` subcommand with `--help` or safe read-only arguments and asserts `returncode == 0`

- [x] 5 BUG-6: Production-readiness — top-level exception handling and atexit
  - [x] 5.1 Add `import atexit` to `main.py` and register an `atexit` handler that calls `db.close()` on any exit
  - [x] 5.2 Wrap the body of `main()` in `main.py` with a top-level `try/except Exception` that prints a clean single-line error message, closes the DB, and calls `sys.exit(1)`

- [x] 6 Verify all existing tests still pass
  - [x] 6.1 Run the full test suite (`pytest tests/ -x`) and confirm zero regressions
