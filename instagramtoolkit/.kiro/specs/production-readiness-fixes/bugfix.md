# Bugfix Requirements Document

## Introduction

The Instagram toolkit has several production-readiness gaps that cause unreliable behavior in real-world use:
signal interrupts hang instead of stopping quickly; downloaded media is never verified to exist on disk;
migrated `.bak` files clutter the `data/` directory; download success is reported without confirming files
were written; and the `.bat` menus have not been end-to-end tested. Together these issues mean the toolkit
cannot be trusted for unattended or long-running production use. This document captures the defective
behaviors, the required correct behaviors, and the existing behaviors that must not regress.

---

## Bug Analysis

### Current Behavior (Defect)

**BUG-1 — Ctrl+C / signal handling hangs during long operations**

1.1 WHEN the user presses Ctrl+C during a long `interruptible_sleep` call (e.g., an emergency break or
    long pause) THEN the system continues sleeping for the remainder of the interval before responding,
    causing the process to appear frozen for minutes.

1.2 WHEN the user presses Ctrl+C during an active Instaloader network request (e.g., fetching followers,
    downloading a post) THEN the system does not interrupt the in-flight request and may hang until the
    request times out.

1.3 WHEN a signal handler fires and calls `save_progress()` followed by `sys.exit(0)` THEN the system
    may leave the SQLite database in an uncommitted WAL state if a write was in progress, resulting in
    potential data corruption or an inconsistent DB state.

**BUG-2 — No post-download media verification**

1.4 WHEN a download operation for a user completes (posts, stories, highlights, or profile photo) THEN
    the system reports success without checking whether any files were actually written to the target
    directory on disk.

1.5 WHEN a file write fails silently (e.g., disk full, permission error, external HDD disconnected mid-
    download) THEN the system marks the user as completed in the DB and does not attempt a redownload.

1.6 WHEN `download_all()` returns `partial_success=True` THEN the system does not identify which
    specific files are missing and does not retry them.

**BUG-3 — Stale `.bak` files remain in `data/`**

1.7 WHEN the toolkit runs after the JSON-to-SQLite migration THEN the files
    `account_cooldowns.json.bak`, `account_quotas.json.bak`, `download_progress.json.bak`,
    `profile_access.json.bak`, `relationships.json.bak`, `username_database.json.bak`, and
    `usernames.txt.bak` remain in `data/`, consuming space and creating confusion about the
    authoritative data source.

1.8 WHEN a developer inspects `data/` THEN the presence of `.bak` files alongside the live
    `instagram_toolkit.db` creates ambiguity about whether the migration was successful.

**BUG-4 — Download success reported without file existence check**

1.9 WHEN `download_profile_photo()` calls `self.loader.download_pic()` and the call returns without
    raising an exception THEN the system returns `True` regardless of whether the image file exists at
    the expected path on disk.

1.10 WHEN `download_posts()`, `download_stories()`, or `download_highlights()` iterate and call
     Instaloader download methods THEN the system increments the `downloaded` counter and returns `True`
     without verifying that the corresponding media files are present in the target directory.

1.11 WHEN the target directory is on an external HDD that becomes unavailable mid-download THEN the
     system does not detect the missing files and reports the operation as successful.

**BUG-5 — .bat menu options not end-to-end tested**

1.12 WHEN any numbered option in `start_toolkit.bat` or `quick_actions.bat` is selected THEN the
     system may invoke a Python command that crashes, hangs, or produces an unhandled error without
     surfacing a clear message to the user.

1.13 WHEN sub-menu options (e.g., Progress Manager sub-options 1–8, Following Download sub-options
     1–4, Selective Download sub-options 1–6) are selected THEN the system may fail silently or
     produce stack traces that are not caught by the `.bat` error handling.

**BUG-6 — General production-readiness gaps**

1.14 WHEN an unhandled exception propagates out of a top-level `main.py` command THEN the system
     exits with a raw Python traceback instead of a clean, user-readable error message and a non-zero
     exit code.

1.15 WHEN the toolkit exits after a Ctrl+C or error THEN the system does not guarantee that all open
     DB connections are closed and WAL checkpoints are flushed before the process terminates.

---

### Expected Behavior (Correct)

**BUG-1 — Ctrl+C / signal handling**

2.1 WHEN the user presses Ctrl+C during any `interruptible_sleep` call THEN the system SHALL wake
    immediately (within one check-interval tick, ≤ 0.2 s), save progress, and exit cleanly.

2.2 WHEN the user presses Ctrl+C during an active network request THEN the system SHALL set a
    shared cancellation flag that causes the next loop iteration or retry to abort, print a
    "Stopping…" message, save progress, and exit within a few seconds.

2.3 WHEN the signal handler fires THEN the system SHALL close all open DB connections and flush the
    WAL before calling `sys.exit(0)`, ensuring the database is in a consistent state.

**BUG-2 — Post-download media verification**

2.4 WHEN a per-user download operation completes THEN the system SHALL scan the target directory and
    verify that at least one media file (`.jpg`, `.mp4`, `.png`) was written for each category that
    reported success.

2.5 WHEN one or more expected media files are missing after a download THEN the system SHALL
    automatically attempt up to 2 redownload retries for the missing items before marking the user
    as failed.

2.6 WHEN redownload retries are exhausted and files are still missing THEN the system SHALL mark
    the user as failed in the DB with an error message listing the missing categories, so the
    operator can investigate.

**BUG-3 — `.bak` file cleanup**

2.7 WHEN the cleanup operation is run THEN the system SHALL delete all `.bak` files in `data/`
    (`account_cooldowns.json.bak`, `account_quotas.json.bak`, `download_progress.json.bak`,
    `profile_access.json.bak`, `relationships.json.bak`, `username_database.json.bak`,
    `usernames.txt.bak`) and confirm each deletion to the user.

2.8 WHEN a `.bak` file does not exist at deletion time THEN the system SHALL skip it silently
    without raising an error.

**BUG-4 — Download success validation**

2.9 WHEN `download_profile_photo()` completes without exception THEN the system SHALL verify that
    the expected profile image file exists on disk at the target path before returning `True`.

2.10 WHEN `download_posts()`, `download_stories()`, or `download_highlights()` complete a batch
     THEN the system SHALL verify that the number of files in the target directory increased by at
     least the number of items reported as downloaded before returning `True`.

2.11 WHEN a file existence check fails after a download call THEN the system SHALL return `False`
     for that category so the caller can handle the failure correctly.

**BUG-5 — .bat menu end-to-end testing**

2.12 WHEN every numbered option in `start_toolkit.bat` is exercised THEN the system SHALL complete
     without unhandled Python exceptions, producing either a success message or a clear, actionable
     error message.

2.13 WHEN every numbered sub-menu option in `start_toolkit.bat` and `quick_actions.bat` is
     exercised THEN the system SHALL return to the parent menu cleanly without hanging or leaving
     orphaned processes.

**BUG-6 — Production-readiness**

2.14 WHEN an unhandled exception propagates out of a top-level `main.py` command THEN the system
     SHALL catch it, print a single-line user-readable error message, and exit with code 1 (no raw
     traceback visible to the end user).

2.15 WHEN the toolkit exits for any reason (normal, Ctrl+C, or unhandled error) THEN the system
     SHALL close all SQLite connections and issue a WAL checkpoint before the process terminates,
     guaranteeing DB integrity.

---

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the user does NOT press Ctrl+C and a sleep completes normally THEN the system SHALL
    CONTINUE TO sleep for the full configured duration and resume the next operation as before.

3.2 WHEN a download completes and all expected files are present on disk THEN the system SHALL
    CONTINUE TO mark the user as completed and report success exactly as it does today.

3.3 WHEN `data/` contains no `.bak` files THEN the system SHALL CONTINUE TO operate normally
    without any errors or warnings related to missing `.bak` files.

3.4 WHEN a download target directory is on a local (non-external) drive and files are written
    successfully THEN the system SHALL CONTINUE TO report success without any additional latency
    from the verification step beyond a fast `os.path.exists` check.

3.5 WHEN the user selects a `.bat` menu option that was already working correctly THEN the system
    SHALL CONTINUE TO execute that option with the same behavior and output as before.

3.6 WHEN the toolkit runs a batch spider or download operation without interruption THEN the system
    SHALL CONTINUE TO rotate accounts, respect rate limits, save progress periodically, and resume
    from the last checkpoint on restart.

3.7 WHEN the DB is in a healthy state at startup THEN the system SHALL CONTINUE TO load progress,
    batch state, and account quotas from SQLite exactly as before, with no change to the DB schema
    or query behavior.

3.8 WHEN `download_all()` is called and all four categories succeed THEN the system SHALL CONTINUE
    TO return `{'success': True, 'partial_success': False, 'success_count': 4, 'total_count': 4,
    'results': {...}}` with the same dict structure as today.
