# Bugfix Requirements Document

## Introduction

The Strava sync toolkit currently has critical signal handling issues that prevent safe interruption during execution. When users press Ctrl+C (SIGINT) during long-running operations, the process becomes unresponsive and requires terminal closure, leaving database connections open and risking data corruption. This bug affects all operations including daily sync, backfill, and media downloads.

The toolkit uses SQLite with WAL mode and autocommit (isolation_level=None) for statement-level atomicity. While this design allows interrupted runs to resume safely, the lack of proper signal handling and connection cleanup creates risk when users force-terminate the process.

Additionally, users need tools to verify database integrity after unexpected terminations and the ability to re-scrape activities if corruption is suspected.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a user presses Ctrl+C during crawler execution with ThreadPoolExecutor operations THEN the system becomes unresponsive and does not terminate

1.2 WHEN a user presses Ctrl+C during network requests with delays THEN the KeyboardInterrupt is not properly propagated to the main thread

1.3 WHEN a user force-closes the terminal during execution THEN database connections remain open without proper cleanup

1.4 WHEN a user interrupts execution THEN no verbose feedback is provided about shutdown progress or safety

1.5 WHEN database records may be corrupted THEN users have no built-in tool to verify database integrity

1.6 WHEN activity records are faulty or incomplete THEN users have no way to re-scrape activities for specific users or all users

### Expected Behavior (Correct)

2.1 WHEN a user presses Ctrl+C during any operation THEN the system SHALL immediately acknowledge the signal and begin graceful shutdown

2.2 WHEN graceful shutdown begins THEN the system SHALL stop accepting new work and wait for in-flight operations to complete with a timeout

2.3 WHEN shutdown is in progress THEN the system SHALL close all database connections properly before terminating

2.4 WHEN shutdown occurs THEN the system SHALL provide verbose feedback about shutdown progress (e.g., "Stopping workers...", "Closing database connections...", "Shutdown complete")

2.5 WHEN users need to verify database integrity THEN the system SHALL provide a command-line option to check all records for validity

2.6 WHEN users need to re-scrape activities THEN the system SHALL provide a command-line option to re-scrape activities for a specific user or all users

### Unchanged Behavior (Regression Prevention)

3.1 WHEN operations complete normally without interruption THEN the system SHALL CONTINUE TO save all work and maintain database consistency

3.2 WHEN the top-level KeyboardInterrupt handler in main.py catches an interrupt THEN the system SHALL CONTINUE TO print the safe stop message

3.3 WHEN database writes use autocommit mode (isolation_level=None) THEN the system SHALL CONTINUE TO provide statement-level atomicity

3.4 WHEN interrupted runs are resumed THEN the system SHALL CONTINUE TO resume from the last committed point

3.5 WHEN backfill operations track progress THEN the system SHALL CONTINUE TO save cursor positions and status after each athlete-month page

3.6 WHEN the crawler finalizes a run THEN the system SHALL CONTINUE TO record run status and summary in the crawl_runs table
