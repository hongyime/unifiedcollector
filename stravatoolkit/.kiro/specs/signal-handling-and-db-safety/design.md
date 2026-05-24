# Signal Handling and DB Safety Bugfix Design

## Overview

The Strava sync toolkit currently lacks proper signal handling for graceful shutdown, causing the process to become unresponsive when users press Ctrl+C during long-running operations. This bug affects all operations including daily sync, backfill, and media downloads. The fix will implement proper signal handling that works with ThreadPoolExecutor, ensure database connections are properly closed during shutdown, provide verbose feedback during shutdown, and add new CLI commands for database integrity checking and activity re-scraping.

The toolkit uses SQLite with WAL mode and autocommit (isolation_level=None) for statement-level atomicity. While this design allows interrupted runs to resume safely, the lack of proper signal handling and connection cleanup creates risk when users force-terminate the process.

## Glossary

- **Bug_Condition (C)**: The condition that triggers the bug - when a user presses Ctrl+C during crawler execution with ThreadPoolExecutor operations or network requests with delays
- **Property (P)**: The desired behavior when Ctrl+C is pressed - the system should immediately acknowledge the signal, begin graceful shutdown, stop accepting new work, wait for in-flight operations to complete with a timeout, close all database connections properly, and provide verbose feedback
- **Preservation**: Existing behavior that must remain unchanged - normal operation completion, database consistency, autocommit mode, resume capability, and the top-level KeyboardInterrupt handler message
- **Crawler**: The main orchestration class in `ingestion/crawler.py` that manages daily sync and backfill operations
- **ThreadPoolExecutor**: Python's concurrent.futures executor used in `_run_backfill()` for parallel athlete backfill processing
- **Signal Handler**: A function registered with the signal module to intercept SIGINT (Ctrl+C) and SIGTERM signals
- **Graceful Shutdown**: The process of stopping new work, waiting for in-flight operations to complete, and cleaning up resources before terminating
- **WAL Mode**: SQLite's Write-Ahead Logging mode that allows concurrent reads and writes
- **Autocommit Mode**: SQLite connection mode (isolation_level=None) that commits each statement immediately

## Bug Details

### Bug Condition

The bug manifests when a user presses Ctrl+C (SIGINT) during crawler execution. The system becomes unresponsive because ThreadPoolExecutor operations and network requests with delays do not properly propagate KeyboardInterrupt to the main thread, and there is no signal handler to coordinate graceful shutdown.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type SignalEvent
  OUTPUT: boolean
  
  RETURN input.signal IN [SIGINT, SIGTERM]
         AND (crawlerIsRunning() OR threadPoolExecutorIsActive() OR networkRequestInProgress())
         AND NOT signalHandlerRegistered()
         AND NOT gracefulShutdownInitiated()
END FUNCTION
```

### Examples

- **Example 1**: User presses Ctrl+C during `_run_backfill()` while ThreadPoolExecutor is processing multiple athletes concurrently
  - **Expected**: System acknowledges signal, stops accepting new work, waits for in-flight athlete backfills to complete, closes database connections, and terminates
  - **Actual**: System becomes unresponsive, requires terminal closure, database connections remain open

- **Example 2**: User presses Ctrl+C during `_fetch_streams()` while `random_delay()` is sleeping
  - **Expected**: System acknowledges signal, interrupts the delay, begins graceful shutdown
  - **Actual**: KeyboardInterrupt is not properly propagated, system continues waiting

- **Example 3**: User presses Ctrl+C during daily feed sync with multiple network requests in flight
  - **Expected**: System acknowledges signal, cancels pending requests, closes database connections, and terminates
  - **Actual**: System becomes unresponsive, no feedback provided

- **Edge Case**: User force-closes terminal during execution
  - **Expected**: Database connections are closed via atexit handler or signal handler
  - **Actual**: Database connections remain open, risking corruption

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Normal operation completion (without interruption) must continue to save all work and maintain database consistency
- The top-level KeyboardInterrupt handler in main.py must continue to print the safe stop message
- Database writes using autocommit mode (isolation_level=None) must continue to provide statement-level atomicity
- Interrupted runs must continue to resume from the last committed point
- Backfill operations must continue to save cursor positions and status after each athlete-month page
- The crawler must continue to record run status and summary in the crawl_runs table

**Scope:**
All inputs that do NOT involve signal interruption (SIGINT, SIGTERM) should be completely unaffected by this fix. This includes:
- Normal execution flow without interruption
- Database operations during normal execution
- ThreadPoolExecutor operations during normal execution
- Network requests during normal execution

## Hypothesized Root Cause

Based on the bug description, the most likely issues are:

1. **No Signal Handler Registration**: The application does not register signal handlers for SIGINT and SIGTERM, so signals are not intercepted and handled gracefully

2. **ThreadPoolExecutor Blocking**: The `_run_backfill()` method uses ThreadPoolExecutor with `as_completed()`, which blocks the main thread and prevents KeyboardInterrupt from being raised until futures complete

3. **Network Delay Blocking**: The `random_delay()` function uses `time.sleep()`, which blocks and prevents immediate signal response

4. **No Shutdown Coordination**: There is no global shutdown flag or event to coordinate graceful shutdown across threads and operations

5. **No Database Connection Cleanup**: Database connections are not explicitly closed in a finally block or atexit handler, so they remain open when the process is force-terminated

6. **No Verbose Feedback**: There is no logging or print statements to inform users about shutdown progress

## Correctness Properties

Property 1: Bug Condition - Graceful Shutdown on Signal

_For any_ signal input where SIGINT or SIGTERM is received during crawler execution, the fixed system SHALL immediately acknowledge the signal, begin graceful shutdown, stop accepting new work, wait for in-flight operations to complete with a timeout, close all database connections properly, and provide verbose feedback about shutdown progress.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

Property 2: Preservation - Normal Operation Behavior

_For any_ execution that does NOT receive a signal interruption, the fixed code SHALL produce exactly the same behavior as the original code, preserving normal operation completion, database consistency, autocommit mode, resume capability, and the top-level KeyboardInterrupt handler message.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `ingestion/main.py`

**Function**: `main()`

**Specific Changes**:
1. **Add Signal Handler Registration**: Register signal handlers for SIGINT and SIGTERM at the start of `main()` to intercept signals and set a global shutdown flag
   - Create a global `shutdown_event` threading.Event()
   - Register signal handler that sets the event and prints verbose feedback
   - Signal handler should be registered before any long-running operations

2. **Add Database Connection Cleanup**: Wrap the main execution in a try-finally block to ensure database connections are closed
   - Move `conn.close()` to a finally block
   - Add atexit handler as a backup to close connections

3. **Add Verbose Shutdown Feedback**: Print messages during shutdown to inform users of progress
   - "Shutdown signal received. Stopping gracefully..."
   - "Waiting for in-flight operations to complete..."
   - "Closing database connections..."
   - "Shutdown complete."

4. **Add CLI Commands**: Add new command-line arguments for database integrity check and activity re-scraping
   - `--check-db-integrity`: Check all database records for validity
   - `--rescrape-activities`: Re-scrape activities for a specific athlete or all athletes
   - `--athlete-id`: Specify athlete ID for re-scraping (optional, defaults to all)

**File**: `ingestion/crawler.py`

**Function**: `run()`, `_run_backfill()`, `_backfill_athlete()`

**Specific Changes**:
1. **Check Shutdown Flag**: Check the global shutdown_event at key points in the crawler execution
   - Before starting daily sync
   - Before starting backfill
   - Before processing each backfill batch
   - Inside the backfill loop

2. **ThreadPoolExecutor Graceful Shutdown**: Modify `_run_backfill()` to handle shutdown gracefully
   - Check shutdown_event before submitting new futures
   - Use executor.shutdown(wait=True, cancel_futures=False) to wait for in-flight operations
   - Add timeout to prevent indefinite waiting

3. **Propagate Shutdown to Workers**: Pass shutdown_event to worker threads
   - Modify `_backfill_athlete_in_worker()` to accept shutdown_event
   - Check shutdown_event inside `_backfill_athlete()` loop

**File**: `ingestion/delay_utils.py`

**Function**: `random_delay()`

**Specific Changes**:
1. **Interruptible Delay**: Replace `time.sleep()` with a loop that checks shutdown_event
   - Use `shutdown_event.wait(timeout=delay)` instead of `time.sleep(delay)`
   - This allows immediate interruption when shutdown is signaled

**File**: `ingestion/db.py`

**New Functions**:
1. **Add `check_db_integrity()`**: Function to verify database integrity
   - Check for orphaned records (activities without athletes, streams without activities)
   - Check for invalid foreign key references
   - Check for NULL values in NOT NULL columns
   - Return a report of issues found

2. **Add `reset_activity_stream_status()`**: Function to reset stream_status for re-scraping
   - Reset stream_status to 'pending' for specified athlete_id or all athletes
   - Clear streams_raw to force re-fetch
   - Return count of activities reset

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on unfixed code, then verify the fix works correctly and preserves existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Write tests that simulate signal interruption during various crawler operations. Run these tests on the UNFIXED code to observe failures and understand the root cause.

**Test Cases**:
1. **ThreadPoolExecutor Interruption Test**: Start a backfill run with multiple athletes, send SIGINT during execution (will hang on unfixed code)
2. **Network Delay Interruption Test**: Start a sync run, send SIGINT during `random_delay()` (will not respond on unfixed code)
3. **Daily Sync Interruption Test**: Start a daily sync, send SIGINT during feed fetch (will hang on unfixed code)
4. **Database Connection Leak Test**: Force-terminate process during execution, check for open database connections (will leak on unfixed code)

**Expected Counterexamples**:
- Process becomes unresponsive when SIGINT is sent during ThreadPoolExecutor operations
- KeyboardInterrupt is not raised during `time.sleep()` in `random_delay()`
- Database connections remain open after force-termination
- No verbose feedback is provided during shutdown

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := handleSignal_fixed(input)
  ASSERT gracefulShutdownInitiated(result)
  ASSERT inFlightOperationsCompleted(result)
  ASSERT databaseConnectionsClosed(result)
  ASSERT verboseFeedbackProvided(result)
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT crawlerRun_original(input) = crawlerRun_fixed(input)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many test cases automatically across the input domain
- It catches edge cases that manual unit tests might miss
- It provides strong guarantees that behavior is unchanged for all non-interrupted executions

**Test Plan**: Observe behavior on UNFIXED code first for normal operations, then write property-based tests capturing that behavior.

**Test Cases**:
1. **Normal Sync Preservation**: Verify that daily sync without interruption produces the same results
2. **Normal Backfill Preservation**: Verify that backfill without interruption produces the same results
3. **Database Consistency Preservation**: Verify that database state is identical after normal operations
4. **Resume Capability Preservation**: Verify that interrupted runs can still resume from the last committed point

### Unit Tests

- Test signal handler registration and shutdown flag setting
- Test ThreadPoolExecutor graceful shutdown with timeout
- Test interruptible delay function
- Test database connection cleanup in finally block
- Test database integrity check function
- Test activity re-scraping function
- Test verbose feedback messages during shutdown

### Property-Based Tests

- Generate random crawler configurations and verify graceful shutdown works correctly
- Generate random execution states and verify preservation of normal operation behavior
- Test that all database connections are closed across many scenarios
- Test that shutdown timeout prevents indefinite waiting

### Integration Tests

- Test full crawler run with signal interruption at various points
- Test that database integrity check detects known issues
- Test that activity re-scraping correctly resets and re-fetches activities
- Test that verbose feedback is provided during shutdown
- Test that interrupted runs can resume from the last committed point
