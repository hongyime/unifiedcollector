# Implementation Plan

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Signal Interruption Unresponsiveness
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - **Scoped PBT Approach**: For deterministic bugs, scope the property to the concrete failing case(s) to ensure reproducibility
  - Test implementation details from Bug Condition in design
  - The test assertions should match the Expected Behavior Properties from design
  - Write property-based test that simulates signal interruption during crawler operations
  - Test cases: SIGINT during ThreadPoolExecutor operations, SIGINT during random_delay(), SIGINT during daily sync, force-termination leaving database connections open
  - For all inputs where isBugCondition(input) = True (signal received during crawler execution), assert that gracefulShutdownInitiated(result) AND inFlightOperationsCompleted(result) AND databaseConnectionsClosed(result) AND verboseFeedbackProvided(result)
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (this is correct - it proves the bug exists)
  - Document counterexamples found to understand root cause (e.g., "Process hangs when SIGINT sent during ThreadPoolExecutor.as_completed()", "KeyboardInterrupt not raised during time.sleep()", "Database connections remain open after force-termination")
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Normal Operation Behavior
  - **IMPORTANT**: Follow observation-first methodology
  - Observe behavior on UNFIXED code for non-buggy inputs (normal execution without signal interruption)
  - Write property-based tests capturing observed behavior patterns from Preservation Requirements
  - Property-based testing generates many test cases for stronger guarantees
  - Test cases: Normal sync without interruption produces same results, normal backfill without interruption produces same results, database consistency preserved, resume capability preserved, top-level KeyboardInterrupt handler message preserved
  - For all inputs where NOT isBugCondition(input) (no signal interruption), assert that crawlerRun_fixed(input) = crawlerRun_original(input)
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [x] 3. Implement signal handling and graceful shutdown

  - [x] 3.1 Add global shutdown coordination mechanism
    - Create global `shutdown_event` using threading.Event() in ingestion/main.py
    - This event will be checked by all long-running operations to detect shutdown requests
    - _Bug_Condition: isBugCondition(input) where input.signal IN [SIGINT, SIGTERM] AND crawlerIsRunning()_
    - _Expected_Behavior: gracefulShutdownInitiated(result) from design_
    - _Preservation: Normal operation completion must continue to save all work (3.1)_
    - _Requirements: 2.1, 3.1_

  - [x] 3.2 Register signal handlers in main.py
    - Register signal handlers for SIGINT and SIGTERM at the start of main()
    - Signal handler should set shutdown_event and print verbose feedback: "Shutdown signal received. Stopping gracefully..."
    - Signal handler should be registered before any long-running operations
    - _Bug_Condition: isBugCondition(input) where NOT signalHandlerRegistered()_
    - _Expected_Behavior: gracefulShutdownInitiated(result) AND verboseFeedbackProvided(result) from design_
    - _Preservation: Top-level KeyboardInterrupt handler message must be preserved (3.2)_
    - _Requirements: 2.1, 2.4, 3.2_

  - [x] 3.3 Add database connection cleanup in main.py
    - Wrap main execution in try-finally block to ensure database connections are closed
    - Move conn.close() to finally block
    - Add atexit handler as backup to close connections
    - Print verbose feedback: "Closing database connections..."
    - _Bug_Condition: isBugCondition(input) where database connections remain open_
    - _Expected_Behavior: databaseConnectionsClosed(result) AND verboseFeedbackProvided(result) from design_
    - _Preservation: Database consistency must be maintained (3.1, 3.3)_
    - _Requirements: 2.3, 2.4, 3.1, 3.3_

  - [x] 3.4 Implement interruptible delay in delay_utils.py
    - Modify random_delay() to accept optional shutdown_event parameter
    - Replace time.sleep() with shutdown_event.wait(timeout=delay) to allow immediate interruption
    - If shutdown_event is set during delay, return immediately
    - Maintain backward compatibility: if shutdown_event is None, use time.sleep()
    - _Bug_Condition: isBugCondition(input) where networkRequestInProgress() AND delay is blocking_
    - _Expected_Behavior: gracefulShutdownInitiated(result) from design_
    - _Preservation: Normal delay behavior must be unchanged (3.1)_
    - _Requirements: 2.1, 3.1_

  - [x] 3.5 Add shutdown checks in crawler.py run() method
    - Check shutdown_event before starting daily sync
    - Check shutdown_event before starting backfill
    - If shutdown_event is set, finalize crawl_run with status='aborted' and return early
    - Print verbose feedback: "Shutdown requested before [operation]. Stopping..."
    - _Bug_Condition: isBugCondition(input) where crawlerIsRunning()_
    - _Expected_Behavior: gracefulShutdownInitiated(result) AND verboseFeedbackProvided(result) from design_
    - _Preservation: Normal crawler operation must be unchanged (3.1, 3.6)_
    - _Requirements: 2.1, 2.2, 2.4, 3.1, 3.6_

  - [x] 3.6 Implement graceful ThreadPoolExecutor shutdown in crawler.py
    - Modify _run_backfill() to check shutdown_event before submitting new futures
    - Use executor.shutdown(wait=True, cancel_futures=False) to wait for in-flight operations
    - Add timeout (e.g., 30 seconds) to prevent indefinite waiting
    - Print verbose feedback: "Waiting for in-flight operations to complete..."
    - If timeout is reached, print: "Timeout reached. Forcing shutdown..."
    - _Bug_Condition: isBugCondition(input) where threadPoolExecutorIsActive()_
    - _Expected_Behavior: gracefulShutdownInitiated(result) AND inFlightOperationsCompleted(result) AND verboseFeedbackProvided(result) from design_
    - _Preservation: Normal backfill operation must be unchanged (3.1, 3.5)_
    - _Requirements: 2.1, 2.2, 2.4, 3.1, 3.5_

  - [x] 3.7 Propagate shutdown_event to worker threads
    - Modify _backfill_athlete_in_worker() to accept shutdown_event parameter
    - Pass shutdown_event to worker Crawler instance
    - Check shutdown_event inside _backfill_athlete() loop before processing each month-page
    - If shutdown_event is set, save progress and return early
    - _Bug_Condition: isBugCondition(input) where threadPoolExecutorIsActive()_
    - _Expected_Behavior: gracefulShutdownInitiated(result) AND inFlightOperationsCompleted(result) from design_
    - _Preservation: Backfill progress tracking must be preserved (3.5)_
    - _Requirements: 2.1, 2.2, 3.5_

  - [x] 3.8 Pass shutdown_event to delay functions
    - Modify crawler._fetch_streams() to pass shutdown_event to random_delay()
    - Modify any other locations that call random_delay() to pass shutdown_event
    - Ensure shutdown_event is available in Crawler class (store as instance variable)
    - _Bug_Condition: isBugCondition(input) where networkRequestInProgress()_
    - _Expected_Behavior: gracefulShutdownInitiated(result) from design_
    - _Preservation: Normal delay behavior must be unchanged (3.1)_
    - _Requirements: 2.1, 3.1_

  - [x] 3.9 Add verbose shutdown feedback throughout
    - Add print statements at key shutdown points: signal received, stopping workers, waiting for operations, closing connections, shutdown complete
    - Ensure feedback is clear and informative for users
    - _Bug_Condition: isBugCondition(input) where no verbose feedback is provided_
    - _Expected_Behavior: verboseFeedbackProvided(result) from design_
    - _Preservation: Existing log messages must be preserved (3.2)_
    - _Requirements: 2.4, 3.2_

  - [x] 3.10 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Graceful Shutdown on Signal
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - The test from task 1 encodes the expected behavior
    - When this test passes, it confirms the expected behavior is satisfied
    - Run bug condition exploration test from step 1
    - **EXPECTED OUTCOME**: Test PASSES (confirms bug is fixed)
    - _Requirements: Expected Behavior Properties from design (2.1, 2.2, 2.3, 2.4)_

  - [x] 3.11 Verify preservation tests still pass
    - **Property 2: Preservation** - Normal Operation Behavior
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run preservation property tests from step 2
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - Confirm all tests still pass after fix (no regressions)

- [x] 4. Add database integrity check CLI command

  - [x] 4.1 Implement check_db_integrity() function in db.py
    - Check for orphaned records (activities without athletes, streams without activities)
    - Check for invalid foreign key references
    - Check for NULL values in NOT NULL columns
    - Check for invalid data types or ranges
    - Return a report dict with issues found: {"orphaned_activities": count, "orphaned_streams": count, "invalid_fk": count, "null_violations": count, "issues": [list of issue descriptions]}
    - _Requirements: 2.5_

  - [x] 4.2 Add --check-db-integrity CLI argument in main.py
    - Add argument to parser: --check-db-integrity
    - When flag is set, call check_db_integrity() and print report
    - Print summary: "Database integrity check complete. Found X issues."
    - If issues found, print detailed list
    - Exit with code 0 if no issues, 1 if issues found
    - _Requirements: 2.5_

  - [x] 4.3 Write unit tests for check_db_integrity()
    - Test detection of orphaned activities
    - Test detection of orphaned streams
    - Test detection of invalid foreign keys
    - Test detection of NULL violations
    - Test clean database returns no issues
    - _Requirements: 2.5_

- [x] 5. Add activity re-scraping CLI command

  - [x] 5.1 Implement reset_activity_stream_status() function in db.py
    - Accept optional athlete_id parameter (None = all athletes)
    - Reset stream_status to 'pending' for specified athlete(s)
    - Clear streams_raw to force re-fetch
    - Delete existing streams records for affected activities
    - Return count of activities reset
    - _Requirements: 2.6_

  - [x] 5.2 Add --rescrape-activities CLI argument in main.py
    - Add argument to parser: --rescrape-activities
    - Add optional --athlete-id argument to specify athlete (defaults to all)
    - When flag is set, call reset_activity_stream_status() with athlete_id
    - Print summary: "Reset X activities for re-scraping."
    - Inform user to run normal sync/backfill to re-fetch activities
    - _Requirements: 2.6_

  - [x] 5.3 Write unit tests for reset_activity_stream_status()
    - Test resetting activities for specific athlete
    - Test resetting activities for all athletes
    - Test that stream_status is set to 'pending'
    - Test that streams_raw is cleared
    - Test that streams records are deleted
    - Test return count is correct
    - _Requirements: 2.6_

- [x] 6. Checkpoint - Ensure all tests pass
  - Run all unit tests
  - Run all property-based tests
  - Run integration tests with signal interruption
  - Verify database integrity check works correctly
  - Verify activity re-scraping works correctly
  - Verify graceful shutdown works at all interruption points
  - Verify preservation tests still pass (no regressions)
  - Ask the user if questions arise
