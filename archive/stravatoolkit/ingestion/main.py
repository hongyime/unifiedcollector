from __future__ import annotations

import argparse
import atexit
import json
import signal
import sys
import threading
from dataclasses import asdict

from ingestion.config import ensure_runtime_dirs, load_settings
from ingestion.crawler import Crawler
from ingestion.db import connect, init_db, repair_backfill_state, save_session_state, check_db_integrity, reset_activity_stream_status, reset_athlete_backfill, checkpoint
from ingestion.logging_config import configure_logging, get_logger
from ingestion.tools.diagnostics.runtime import emit_requests_dependency_health_once
from ingestion.session import StravaSession
from ingestion.tools.diagnostics.db import check_backfill_health
from ingestion.tools.venv.health import check_venv_health, print_health_report
from ingestion.tools.venv.healer import heal_venv, HealConfig, print_heal_result

logger = get_logger(__name__)

# Global shutdown coordination mechanism
# This event is set when a shutdown signal (SIGINT/SIGTERM) is received
# All long-running operations should check this event to detect shutdown requests
shutdown_event = threading.Event()

# Global database connection reference for cleanup
# This is set when a connection is opened and used by the atexit handler
_db_connection = None


def _cleanup_db_connection():
    """Backup cleanup handler registered with atexit to close database connections."""
    global _db_connection
    if _db_connection is not None:
        try:
            logger.info("Closing database connections...")
            # Checkpoint the WAL before closing to merge it into the main DB file
            checkpoint(_db_connection)
            _db_connection.close()
            _db_connection = None
        except Exception as e:
            logger.error(f"DB cleanup error: {e}")


# Register atexit handler as backup to close connections
atexit.register(_cleanup_db_connection)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Followed-athlete Strava sync and backfill runner.")
    parser.add_argument("--date", help="Playback date in YYYY-MM-DD format. Required for feed refresh runs.")
    parser.add_argument(
        "--auth-mode",
        choices=["playwright", "cookiestxt"],
        default="cookiestxt",
        help="How to acquire the Strava session cookie.",
    )
    parser.add_argument("--cookies-file", help="Path to Netscape-format cookies.txt file.")
    parser.add_argument(
        "--refresh-following-roster",
        action="store_true",
        help="Refresh the authoritative list of athletes you follow before syncing.",
    )
    parser.add_argument(
        "--backfill-steps",
        type=int,
        default=None,
        help="Limit historical backfill work by athlete-month steps.",
    )
    parser.add_argument(
        "--backfill-budget-minutes",
        type=int,
        default=None,
        help="Deprecated alias kept for compatibility. Treated as backfill steps.",
    )
    parser.add_argument(
        "--backfill-only",
        action="store_true",
        help="Skip the daily feed sync and continue historical backfill only.",
    )
    parser.add_argument(
        "--sync-only",
        action="store_true",
        help="Run only the daily feed check for new activities and skip historical backfill.",
    )
    parser.add_argument(
        "--check-health",
        action="store_true",
        help="Check backfill health and report on degraded athletes.",
    )
    parser.add_argument(
        "--check-venv",
        action="store_true",
        help="Check venv health and report issues.",
    )
    parser.add_argument(
        "--heal-venv",
        action="store_true",
        help="Automatically fix venv issues (install missing packages, upgrade outdated packages, etc.).",
    )
    parser.add_argument(
        "--skip-venv-check",
        action="store_true",
        help="Skip venv health check.",
    )
    parser.add_argument(
        "--upgrade-mode",
        choices=["auto", "prompt", "never"],
        default="auto",
        help="When to upgrade outdated packages. Default: auto.",
    )
    parser.add_argument(
        "--force-reinstall",
        action="store_true",
        help="Force venv recreation.",
    )
    parser.add_argument(
        "--backfill-parallelism",
        type=int,
        default=None,
        help="How many athletes to backfill concurrently.",
    )
    parser.add_argument(
        "--backfill-year-cap",
        type=int,
        default=None,
        help="Hard stop for historical backfill, counted in years from the current year.",
    )
    parser.add_argument(
        "--auth-fallback",
        choices=["auto", "playwright", "cookiestxt", "none"],
        default="auto",
        help="Fallback auth source to try when the current session expires.",
    )
    parser.add_argument(
        "--debug-http",
        action="store_true",
        help="Print richer HTTP and parsing diagnostics for auth and backfill failures.",
    )
    parser.add_argument("--cookie-value", help="Explicit Strava session cookie value.")
    parser.add_argument(
        "--check-db-integrity",
        action="store_true",
        help="Check database records for integrity issues (orphaned records, NULL violations, invalid FK references).",
    )
    parser.add_argument(
        "--rescrape-activities",
        action="store_true",
        help="Reset stream_status to pending for re-scraping activities.",
    )
    parser.add_argument(
        "--athlete-id",
        type=int,
        default=None,
        help="Athlete ID to target for --rescrape-activities or --reset-backfill (defaults to all).",
    )
    parser.add_argument(
        "--reset-backfill",
        action="store_true",
        help="Reset backfill state to pending for one athlete (--athlete-id) or all tracked athletes. "
             "Clears all cursors, issue codes, and completion timestamps.",
    )
    return parser


def main() -> None:
    # Configure logging first
    configure_logging()
    
    # Register signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        """Handle SIGINT and SIGTERM by initiating graceful shutdown."""
        logger.info("Shutdown signal received. Stopping gracefully...")
        shutdown_event.set()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal_handler)
    
    args = build_parser().parse_args()

    # Handle --check-venv flag
    if args.check_venv:
        report = check_venv_health()
        print_health_report(report)
        return

    # Handle --heal-venv flag
    if args.heal_venv:
        config = HealConfig(
            auto_upgrade=args.upgrade_mode == "auto",
            auto_recreate=not args.skip_venv_check,
            prompt_before_action=args.upgrade_mode == "prompt",
            dry_run=False,
            verbose=True,
        )
        result = heal_venv(config=config)
        print_heal_result(result)
        if result.restart_required:
            logger.info("Please restart the toolkit for changes to take effect.")
        raise SystemExit(0 if result.success else 1)

    # Auto health check (unless skipped)
    if not args.skip_venv_check and not (args.check_health or args.backfill_only):
        report = check_venv_health()
        if not report.is_healthy() and report.can_auto_fix:
            config = HealConfig(
                auto_upgrade=True,
                auto_recreate=True,
                prompt_before_action=False,
                dry_run=False,
                verbose=False,
            )
            heal_venv(config=config)

    # Handle --check-health flag
    if args.check_health:
        check_backfill_health()
        return

    # Handle --check-db-integrity flag
    if args.check_db_integrity:
        settings = load_settings()
        init_db(settings.db_path)
        conn = connect(settings.db_path)
        try:
            report = check_db_integrity(conn)
        finally:
            conn.close()
        total_issues = report["orphaned_activities"] + report["orphaned_streams"] + report["invalid_fk"] + report["null_violations"]
        logger.info(f"Database integrity check complete. Found {total_issues} issue(s).")
        if report["issues"]:
            for issue in report["issues"]:
                logger.warning(f"  - {issue}")
        raise SystemExit(0 if total_issues == 0 else 1)

    # Handle --rescrape-activities flag
    if args.rescrape_activities:
        settings = load_settings()
        init_db(settings.db_path)
        conn = connect(settings.db_path)
        try:
            count = reset_activity_stream_status(conn, athlete_id=args.athlete_id)
        finally:
            conn.close()
        scope = f"athlete {args.athlete_id}" if args.athlete_id else "all athletes"
        logger.info(f"Reset {count} activity/activities for re-scraping ({scope}).")
        logger.info("Run a normal sync or backfill to re-fetch the activities.")
        raise SystemExit(0)

    # Handle --reset-backfill flag
    if args.reset_backfill:
        settings = load_settings()
        init_db(settings.db_path)
        conn = connect(settings.db_path)
        try:
            count = reset_athlete_backfill(conn, athlete_id=args.athlete_id)
        finally:
            conn.close()
        scope = f"athlete {args.athlete_id}" if args.athlete_id else "all tracked athletes"
        logger.info(f"Reset backfill state for {count} athlete(s) ({scope}) to pending.")
        logger.info("Run a backfill to re-process from the beginning.")
        raise SystemExit(0)

    if not args.backfill_only and not args.date:
        raise SystemExit("--date is required unless you are running --backfill-only.")

    settings = load_settings()
    emit_requests_dependency_health_once()
    if args.backfill_parallelism is not None:
        settings.backfill_parallelism = max(1, args.backfill_parallelism)
    if args.backfill_year_cap is not None:
        settings.backfill_year_cap = max(0, args.backfill_year_cap)
    if args.debug_http:
        settings.debug_http = True
    ensure_runtime_dirs(settings)
    init_db(settings.db_path)
    run_mode = "backfill-only" if args.backfill_only else "sync-only" if args.sync_only else "sync+backfill"
    target_label = args.date or "saved backfill cursors"
    logger.info(f"Starting {run_mode} run for {target_label} using auth mode '{args.auth_mode}'.")
    session = StravaSession.from_sources(
        settings,
        auth_mode=args.auth_mode,
        auth_fallback=args.auth_fallback,
        cookie_value=args.cookie_value,
        cookies_file=args.cookies_file,
    )
    session.shutdown_event = shutdown_event
    session.persist_cookie()
    logger.info("Session ready. Database opened and run state will be saved as work completes.")

    conn = connect(settings.db_path)
    global _db_connection
    _db_connection = conn
    try:
        repair_backfill_state(conn)
        save_session_state(conn, session.cookie_value, args.auth_mode)
        session.set_persist_callback(lambda cookie_value, auth_mode: save_session_state(conn, cookie_value, auth_mode))
        summary = Crawler(conn, session, settings, shutdown_event).run(
            args.date,
            refresh_following_roster=args.refresh_following_roster,
            backfill_steps=args.backfill_steps or args.backfill_budget_minutes,
            backfill_only=args.backfill_only,
            sync_only=args.sync_only,
        )
    finally:
        logger.info("Closing database connections...")
        conn.close()
        _db_connection = None
        
        # Print shutdown complete message if shutdown was requested
        if shutdown_event.is_set():
            logger.info("Shutdown complete.")

    logger.info(json.dumps(asdict(summary), indent=2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Run stopped safely. Saved work remains intact, and the next run will resume from the last committed point.")
        raise SystemExit(130)
