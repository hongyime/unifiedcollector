# Production Readiness Fixes Bugfix Design

## Overview

This document formalizes the fix approach for six production-readiness bugs in the Instagram toolkit.
The bugs collectively cause unreliable behavior during long-running unattended operations: signal
interrupts hang, downloaded media is never verified, stale .bak files clutter data/, download
success is reported without confirming files were written, .bat menus are untested, and unhandled
exceptions produce raw tracebacks instead of clean error messages.

The fix strategy is minimal and surgical: each bug is addressed in the smallest possible scope,
no DB schema changes are made, sessions and .env are untouched, and all existing tests must
continue to pass.

## Glossary

- **Bug_Condition (C)**: The set of runtime conditions that trigger one of the six defective behaviors.
- **Property (P)**: The desired correct behavior when the bug condition holds.
- **Preservation**: Existing behaviors that must remain unchanged after the fix.
- **interruptible_sleep**: The RateLimiter.interruptible_sleep() method in lib/rate_limiter.py that
  sleeps in 0.2 s ticks to allow Ctrl+C to interrupt long waits.
- **_shutdown_requested**: A threading.Event added to InstagramProcessor that all processing loops
  check each iteration to support graceful shutdown.
- **verify_download**: A helper method added to MediaDownloader that scans a target directory for
  media files after a download operation and retries up to 2 times if none are found.
- **cleanup-bak**: A new main.py CLI subcommand that deletes all .bak files in data/.
- **WAL checkpoint**: SQLite Write-Ahead Log flush triggered by db.close() to ensure DB consistency.
## Bug Details

### BUG-1: Graceful Ctrl+C / Signal Handling

The bug manifests when the user presses Ctrl+C during a long interruptible_sleep call or during
an active network request. The process either continues sleeping for the full interval or hangs
until the request times out. The signal handler also does not guarantee DB WAL flush before exit.

### BUG-2 + BUG-4: No Post-Download Media Verification

The bug manifests when any download method completes without raising an exception. The system
returns True without checking whether any files were actually written to disk.

### BUG-3: Stale .bak Files in data/

The bug manifests after the JSON-to-SQLite migration: seven .bak files remain in data/ with
no automated way to remove them.

### BUG-5: .bat Menu Options Not End-to-End Tested

The bug manifests when any numbered option in start_toolkit.bat or quick_actions.bat invokes
a Python command that crashes, hangs, or produces an unhandled exception.

### BUG-6: Production-Readiness Gaps

The bug manifests when an unhandled exception propagates out of main() in main.py, producing
a raw Python traceback, or when the process exits without closing DB connections.


## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Normal sleep completions (no Ctrl+C) must continue to sleep for the full configured duration.
- Download operations that succeed and write files must continue to report success without extra latency beyond a fast os.path.exists check.
- The DB schema must not change; all existing queries and repositories must work identically.
- All existing tests must continue to pass.
- Sessions in sessions/ and .env must not be modified or deleted.
- The download_all() return dict structure must remain: {success, partial_success, success_count, total_count, results}.

**Scope:**
All inputs that do NOT trigger one of the six bug conditions should be completely unaffected by
these fixes. This includes:
- Normal (non-interrupted) batch spider and download operations.
- Download operations where files are successfully written to disk.
- CLI commands that already work correctly.
- DB operations in a healthy state.


## Hypothesized Root Cause

### BUG-1: Signal Handling

1. **Missing global cancellation event**: `interruptible_sleep` already loops in 0.2 s ticks, but
   it only checks `time.time()`. It does not check a shared cancellation `threading.Event`, so
   Ctrl+C only interrupts the current 0.2 s tick via `KeyboardInterrupt` — which is then caught
   by the outer `try/except` in the signal handler, not by the sleep loop itself.

2. **No _shutdown_requested flag on InstagramProcessor**: The batch loops in
   `process_batch_relationships` and `process_batch_downloads` do not check any cancellation flag
   between iterations, so they continue processing the next user even after Ctrl+C.

3. **Signal handler does not call db.close()**: `ProgressManager._setup_signal_handlers` calls
   `save_progress()` and `sys.exit(0)` but never calls `db.close()`, leaving the SQLite WAL
   potentially uncommitted.

### BUG-2 + BUG-4: Media Verification

1. **No post-download file existence check**: `download_profile_photo`, `download_posts`,
   `download_stories`, and `download_highlights` all return `True` as long as no exception is
   raised. They never call `os.listdir()` or `glob()` on the target directory to confirm files
   were written.

2. **No retry on empty directory**: Even if a check were added, there is no retry loop to
   re-attempt the download when the directory is empty.

### BUG-3: .bak Files

1. **No cleanup command**: `main.py` has no `cleanup-bak` subcommand. The `.bak` files were
   created by the JSON-to-SQLite migration script and were never cleaned up.

### BUG-5: .bat Testing

1. **No test file for CLI commands**: There is no `tests/test_bat_menus.py` that exercises every
   CLI command in `main.py` with `--help` or safe read-only arguments.

### BUG-6: Production-Readiness

1. **No top-level try/except in main()**: Exceptions from individual command handlers propagate
   as raw tracebacks. While each command block has its own `try/except`, the outer `main()`
   function has no catch-all.

2. **No atexit handler**: There is no `atexit.register(db.close)` call to guarantee DB cleanup
   on any exit path.


## Correctness Properties

Property 1: Bug Condition - Graceful Ctrl+C Interruption

_For any_ signal event (SIGINT/SIGTERM) received during an interruptible_sleep call, the fixed
code SHALL wake within one check-interval tick (<=0.2 s), set the _shutdown_requested flag,
flush the DB WAL via db.close(), and exit cleanly without hanging.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Bug Condition - Download File Verification

_For any_ download operation where the download method returns True but the target directory
contains zero media files (.jpg, .mp4, .png), the fixed verify_download helper SHALL retry
the download up to 2 times before marking the user as failed.

**Validates: Requirements 2.4, 2.5, 2.6, 2.9, 2.10, 2.11**

Property 3: Bug Condition - .bak File Cleanup

_For any_ invocation of python main.py cleanup-bak, the fixed code SHALL delete all .bak files
in data/ and confirm each deletion, and SHALL skip silently if a file does not exist.

**Validates: Requirements 2.7, 2.8**

Property 4: Bug Condition - CLI Command Safety

_For any_ CLI command in main.py invoked with --help or safe read-only arguments, the fixed
code SHALL complete without raising an unhandled Python exception.

**Validates: Requirements 2.12, 2.13**

Property 5: Bug Condition - Top-Level Exception Handling

_For any_ unhandled exception that propagates out of a main.py command handler, the fixed
main() SHALL catch it, print a single-line user-readable error message, close the DB, and
exit with code 1 (no raw traceback visible to the end user).

**Validates: Requirements 2.14, 2.15**

Property 6: Preservation - Normal Operation Unchanged

_For any_ input where none of the bug conditions hold (normal sleep completion, successful
download with files present, no Ctrl+C, no unhandled exception), the fixed code SHALL produce
exactly the same behavior as the original code, preserving all existing functionality.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8**


## Fix Implementation

### Changes Required

#### BUG-1: Graceful Ctrl+C (lib/rate_limiter.py + lib/parallel_processor.py + lib/progress_manager.py)

**File**: `lib/rate_limiter.py`

**Function**: `RateLimiter.interruptible_sleep`

**Specific Changes**:
1. **Add cancellation event parameter**: Accept an optional `threading.Event` (or a module-level
   `_SHUTDOWN_EVENT`) that `interruptible_sleep` checks on each 0.2 s tick. If the event is set,
   return immediately.
2. **Module-level shutdown event**: Add `_SHUTDOWN_EVENT = threading.Event()` at module level in
   `rate_limiter.py` so all `RateLimiter` instances share the same cancellation signal.

**File**: `lib/parallel_processor.py`

**Class**: `InstagramProcessor`

**Specific Changes**:
1. **Add _shutdown_requested flag**: Add `self._shutdown_requested = threading.Event()` in `__init__`.
2. **Check flag in batch loops**: In `process_batch_relationships` and `process_batch_downloads`,
   check `self._shutdown_requested.is_set()` at the top of each iteration and break if set.
3. **Set flag in signal handler**: Override or extend the signal handler to set `_shutdown_requested`
   and call `_get_db().close()` before `sys.exit(0)`.

**File**: `lib/progress_manager.py`

**Function**: `_setup_signal_handlers`

**Specific Changes**:
1. **Call db.close() in signal handler**: Before calling `sys.exit(0)`, call `_get_db().close()`
   to flush the WAL and close the SQLite connection.
2. **Set rate_limiter shutdown event**: Import and set `_SHUTDOWN_EVENT` from `rate_limiter` so
   any in-progress `interruptible_sleep` wakes immediately.

#### BUG-2 + BUG-4: Media Verification (lib/download_media.py)

**File**: `lib/download_media.py`

**Class**: `MediaDownloader`

**Specific Changes**:
1. **Add verify_download helper**: Add a `verify_download(username, category, target_dir)` method
   that uses `glob.glob` to count files matching `*.jpg`, `*.mp4`, `*.png` in `target_dir`.
   Returns `True` if count > 0, `False` otherwise.
2. **Wrap download methods with verification + retry**: After each of `download_profile_photo`,
   `download_posts`, `download_stories`, `download_highlights` completes successfully, call
   `verify_download`. If it returns `False`, retry the download up to 2 times before returning
   `False` to the caller.
3. **Preserve return structure**: `download_all()` return dict structure is unchanged.

#### BUG-3: .bak File Cleanup (main.py)

**File**: `main.py`

**Specific Changes**:
1. **Add cleanup-bak subcommand**: Register `subparsers.add_parser('cleanup-bak')` in the
   argument parser.
2. **Implement cleanup_bak() function**: Iterate over the known `.bak` filenames in `DATA_DIR`,
   delete each with `os.remove()` if it exists, print a confirmation per file, and skip silently
   if not found.
3. **Delete .bak files now**: As part of task execution, delete the existing `.bak` files in
   `data/` immediately (one-time cleanup).

#### BUG-5: .bat Testing (tests/test_bat_menus.py)

**File**: `tests/test_bat_menus.py` (new file)

**Specific Changes**:
1. **Create test file**: Write a pytest test module that uses `subprocess.run` to invoke
   `python main.py <command> --help` (or safe read-only args) for every CLI command.
2. **Cover all subcommands**: list, login --help, test-all --help, access-stats --help,
   priority-analysis --help, spider --help, download --help, following-download --help,
   selective-download --help, analyze --help, analyze-profiles --help, progress --help,
   db-migrate --help, cleanup-bak --help.
3. **Assert no crash**: Each test asserts `returncode == 0` and no unhandled exception in stderr.

#### BUG-6: Production-Readiness (main.py)

**File**: `main.py`

**Function**: `main()`

**Specific Changes**:
1. **Top-level try/except**: Wrap the entire `main()` body in a `try/except Exception as e` block
   that prints a clean single-line error message and calls `sys.exit(1)`.
2. **atexit handler**: Register `atexit.register(_close_db)` where `_close_db` calls
   `_get_db().close()` (using the same singleton pattern as `progress_manager.py`).
3. **Import atexit**: Add `import atexit` at the top of `main.py`.


## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate
each bug on unfixed code, then verify the fix works correctly and preserves existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate each bug BEFORE implementing the fix.
Confirm or refute the root cause analysis.

**Test Plan**: Write tests that simulate the bug conditions and assert the defective behavior
on UNFIXED code to understand the root cause.

**Test Cases**:
1. **BUG-1 Sleep Interrupt Test**: Set the shutdown event and verify interruptible_sleep returns
   within 0.2 s (will fail on unfixed code where no event is checked).
2. **BUG-2 Empty Directory Test**: Mock a download method to return True without writing files,
   then assert verify_download returns False (will fail on unfixed code with no verify_download).
3. **BUG-3 Cleanup Command Test**: Run python main.py cleanup-bak and assert exit code 0
   (will fail on unfixed code where the command does not exist).
4. **BUG-5 CLI Help Test**: Run python main.py list --help and assert exit code 0
   (may pass on unfixed code for some commands, fail for others).
5. **BUG-6 Exception Handling Test**: Trigger an exception in a command handler and assert
   exit code 1 with no traceback in stderr (will fail on unfixed code).

**Expected Counterexamples**:
- interruptible_sleep does not check any cancellation event, so it cannot be interrupted early.
- No verify_download method exists on MediaDownloader.
- cleanup-bak command does not exist in main.py argument parser.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed code produces
the expected behavior.

**Pseudocode:**
```
FOR ALL event WHERE isBugCondition(event) DO
  result := fixed_handler(event)
  ASSERT expectedBehavior(result)
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed code
produces the same result as the original code.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT original_handler(input) == fixed_handler(input)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many test cases automatically across the input domain.
- It catches edge cases that manual unit tests might miss.
- It provides strong guarantees that behavior is unchanged for all non-buggy inputs.

**Test Cases**:
1. **Normal Sleep Preservation**: Verify that interruptible_sleep with no cancellation event
   still sleeps for the full duration.
2. **Successful Download Preservation**: Verify that download_all() with files present still
   returns the same dict structure with success=True.
3. **No .bak Files Preservation**: Verify that cleanup-bak with no .bak files present exits
   cleanly without errors.
4. **Existing CLI Commands Preservation**: Verify all existing CLI commands still work after
   adding the cleanup-bak command and top-level exception handler.

### Unit Tests

- Test interruptible_sleep wakes immediately when shutdown event is set.
- Test verify_download returns False for empty directory, True for directory with media files.
- Test cleanup_bak deletes existing .bak files and skips missing ones.
- Test main() catches unhandled exceptions and exits with code 1.
- Test atexit handler calls db.close() on exit.

### Property-Based Tests

- Generate random sleep durations and verify that setting the shutdown event always causes
  early return within one check interval.
- Generate random directory contents and verify verify_download correctly identifies
  presence/absence of media files.
- Generate random exception types and verify main() always exits with code 1 and no traceback.

### Integration Tests

- Test full Ctrl+C flow: start a batch operation, send SIGINT, verify clean exit within 1 s.
- Test download verification retry: mock a download that writes files on the second attempt,
  verify the retry logic works end-to-end.
- Test cleanup-bak CLI command end-to-end with actual .bak files in data/.
- Test all bat menu commands via subprocess with --help to verify no crashes.

