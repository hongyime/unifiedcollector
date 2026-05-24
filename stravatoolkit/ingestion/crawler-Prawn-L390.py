from __future__ import annotations

import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime

from ingestion import db
from ingestion.core.delays import random_delay
from ingestion.core.scrapers import FollowingFeedScraper, FollowRosterScraper, HistoricalActivityScraper
from ingestion.transform import transform_streams

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CrawlSummary:
    target_date: str | None
    roster_size: int = 0
    daily_activity_count: int = 0
    new_activity_count: int = 0
    backfill_athletes_processed: int = 0
    backfill_completed: int = 0
    degraded_backfill: bool = False
    daily_sync_degraded: bool = False
    daily_sync_issue: str | None = None


class Crawler:
    def __init__(self, conn, session, settings, shutdown_event=None):
        self.conn = conn
        self.session = session
        self.settings = settings
        self.shutdown_event = shutdown_event
        self.feed_scraper = FollowingFeedScraper(session, shutdown_event=shutdown_event)
        self.roster_scraper = FollowRosterScraper(session, shutdown_event=shutdown_event)
        self.history_scraper = HistoricalActivityScraper(session, shutdown_event=shutdown_event)
        # Configure delay range for stream requests
        if settings is not None:
            self._stream_delay_range = (
                getattr(settings, "stream_delay_min_seconds", 1.0),
                getattr(settings, "stream_delay_max_seconds", 2.5),
            )
            self._debug_delays = getattr(settings, "debug_delays", False)
        else:
            # Default values for testing without settings
            self._stream_delay_range = (1.0, 2.5)
            self._debug_delays = False

    def run(
        self,
        target_date: str | None,
        *,
        refresh_following_roster: bool = False,
        backfill_steps: int | None = None,
        backfill_only: bool = False,
        sync_only: bool = False,
    ) -> CrawlSummary:
        step_limit = backfill_steps or self.settings.backfill_steps
        target_label = target_date or "saved backfill cursors"
        logger.info(f"Validating session and preparing crawl for {target_label}.")
        authenticated_athlete = self.session.validate()
        athlete_id = int(authenticated_athlete["id"])
        self._ensure_self_athlete(authenticated_athlete)
        summary = CrawlSummary(target_date=target_date)
        run_id = db.create_crawl_run(
            self.conn,
            run_type="backfill_continue" if backfill_only else "daily_sync",
            target_date=target_date,
            roster_refreshed=refresh_following_roster,
            backfill_step_limit=step_limit,
        )

        try:
            if refresh_following_roster:
                logger.info("Refreshing following roster from Strava.")
                roster = self.roster_scraper.fetch_following_roster(athlete_id)
                summary.roster_size = db.sync_following_roster(self.conn, roster)
                logger.info(f"Following roster refreshed. {summary.roster_size} athletes currently followed.")
            else:
                summary.roster_size = db.get_status_summary(self.conn)["follow_roster_size"]
                logger.info(f"Using saved following roster with {summary.roster_size} athletes.")

            if not backfill_only:
                # Check for shutdown request before starting daily sync
                if self.shutdown_event is not None and self.shutdown_event.is_set():
                    logger.info("Shutdown requested before daily sync. Stopping...")
                    db.finalize_crawl_run(
                        self.conn,
                        run_id,
                        status="aborted",
                        notes=json.dumps(asdict(summary)),
                    )
                    return summary
                
                logger.info(f"Refreshing feed for {target_date}.")
                try:
                    daily_activities = self.feed_scraper.fetch_activities_for_date(athlete_id, target_date)
                    summary.daily_activity_count = len(daily_activities)
                    logger.info(f"Feed returned {summary.daily_activity_count} activities for {target_date}.")
                    summary.new_activity_count += self._ingest_activity_batch(daily_activities)
                except Exception as exc:
                    summary.daily_sync_degraded = True
                    summary.daily_sync_issue = str(exc)
                    logger.warning(f"Feed refresh degraded: {exc}")

            if not sync_only:
                # Check for shutdown request before starting backfill
                if self.shutdown_event is not None and self.shutdown_event.is_set():
                    logger.info("Shutdown requested before backfill. Stopping...")
                    db.finalize_crawl_run(
                        self.conn,
                        run_id,
                        status="aborted",
                        notes=json.dumps(asdict(summary)),
                    )
                    return summary
                
                logger.info(
                    f"Starting historical backfill with step budget {step_limit} athlete-month page(s) "
                    f"at parallelism {self.settings.backfill_parallelism}."
                )
                results = self._run_backfill(step_limit)
                summary.backfill_athletes_processed = len({result["athlete_id"] for result in results if result["steps_used"] > 0})
                summary.new_activity_count += sum(result["new_activity_count"] for result in results)
                summary.degraded_backfill = any(result["degraded"] for result in results)
                summary.backfill_completed += sum(1 for result in results if result["completed"])

            db.finalize_crawl_run(
                self.conn,
                run_id,
                status="ok",
                notes=json.dumps(asdict(summary)),
            )
            logger.info("Run finished successfully.")
        except KeyboardInterrupt:
            db.finalize_crawl_run(
                self.conn,
                run_id,
                status="aborted",
                notes=json.dumps(asdict(summary)),
            )
            raise
        except Exception as exc:
            db.finalize_crawl_run(self.conn, run_id, status="failed", notes=str(exc))
            raise

        return summary

    def _ensure_self_athlete(self, authenticated_athlete: dict) -> None:
        first_name = str(authenticated_athlete.get("firstname") or "").strip()
        last_name = str(authenticated_athlete.get("lastname") or "").strip()
        display_name = str(authenticated_athlete.get("display_name") or authenticated_athlete.get("name") or "").strip()
        full_name = display_name or " ".join(part for part in [first_name, last_name] if part).strip() or f"Athlete {authenticated_athlete['id']}"
        db.upsert_athlete(
            self.conn,
            athlete_id=int(authenticated_athlete["id"]),
            name=full_name,
            avatar_url=authenticated_athlete.get("profile") or authenticated_athlete.get("avatar_url"),
            is_private=bool(authenticated_athlete.get("private", False)),
            source="self",
            is_following=False,
            is_tracked=True,
        )

    def _run_backfill(self, step_limit: int) -> list[dict]:
        results: list[dict] = []
        processed_steps = 0
        parallelism = max(1, self.settings.backfill_parallelism)
        while processed_steps < step_limit:
            # Check for shutdown request before processing new batch
            if self.shutdown_event is not None and self.shutdown_event.is_set():
                logger.info("Shutdown requested during backfill. Stopping...")
                break
            
            candidates = db.get_following_backfill_candidates(self.conn)
            if not candidates:
                break
            batch_size = min(parallelism, step_limit - processed_steps, len(candidates))
            batch = candidates[:batch_size]
            if batch_size == 1:
                result = self._backfill_athlete(int(batch[0]["athlete_id"]), max_steps=1)
                results.append(result)
                processed_steps += result["steps_used"]
                continue
            
            executor = ThreadPoolExecutor(max_workers=batch_size)
            shutdown_detected = False
            try:
                futures = {
                    executor.submit(self._backfill_athlete_in_worker, int(athlete["athlete_id"])): int(athlete["athlete_id"])
                    for athlete in batch
                }
                for future in as_completed(futures):
                    # Check for shutdown request while processing futures
                    if self.shutdown_event is not None and self.shutdown_event.is_set() and not shutdown_detected:
                        logger.info("Waiting for in-flight operations to complete...")
                        shutdown_detected = True
                        # Don't break yet - continue processing already-completed futures
                    
                    result = future.result()
                    results.append(result)
                    processed_steps += result["steps_used"]
            finally:
                # Gracefully shutdown the executor
                # wait=True ensures in-flight operations complete
                # cancel_futures=False ensures submitted futures are not cancelled
                executor.shutdown(wait=True, cancel_futures=False)
            
            # If shutdown was detected, stop processing new batches
            if shutdown_detected:
                break
            
            if processed_steps >= step_limit:
                logger.info("Backfill step budget reached for this run.")
                break
        return results

    def _backfill_athlete_in_worker(self, athlete_id: int) -> dict:
        worker_conn = db.connect(self.settings.db_path)
        worker_session = self.session.clone()
        try:
            worker = Crawler(worker_conn, worker_session, self.settings, self.shutdown_event)
            return worker._backfill_athlete(athlete_id, max_steps=1)
        finally:
            worker_conn.close()

    def _backfill_athlete(self, athlete_id: int, *, max_steps: int) -> dict:
        athlete = self.conn.execute(
            "SELECT * FROM athletes WHERE athlete_id = ?",
            (athlete_id,),
        ).fetchone()
        athlete_name = athlete["name"] if athlete else f"Athlete {athlete_id}"

        # Phase 1: Recent catch-up (day-level gap fill from today backwards)
        total_steps = 0
        recent_steps, total_new_activity_count = self._backfill_athlete_recent(athlete_id, athlete, max_steps - total_steps)
        total_steps += recent_steps

        if total_steps < max_steps:
            # Phase 2: Deep history resume (month-cursor backfill)
            deep_steps, deep_new_activity_count = self._backfill_athlete_deep(athlete_id, athlete, max_steps - total_steps)
            total_steps += deep_steps
            total_new_activity_count += deep_new_activity_count

        athlete = self.conn.execute(
            "SELECT * FROM athletes WHERE athlete_id = ?",
            (athlete_id,),
        ).fetchone()
        backfill_status = athlete["backfill_status"] if athlete else "pending"
        deep_complete = athlete["backfill_completed_at"] is not None
        recent_complete = athlete["backfill_recent_completed_at"] is not None
        final_completed = deep_complete and recent_complete

        return {
            "athlete_id": athlete_id,
            "new_activity_count": total_new_activity_count,
            "completed": final_completed,
            "degraded": backfill_status == "degraded",
            "steps_used": total_steps,
        }

    def _ingest_activity_batch(self, activities: list[dict]) -> int:
        new_activity_count = 0
        for activity in activities:
            print(
                f"[ingest] Activity {activity['activity_id']} - {activity['athlete_name']} - "
                f"{activity.get('activity_name') or 'Unnamed activity'}"
            )
            db.save_activity_photos(self.conn, activity)
            if not activity.get("is_renderable", True):
                print("[ingest]   skipped: no renderable map data")
                continue
            if db.activity_exists_with_terminal_stream(self.conn, int(activity["activity_id"])):
                print("[ingest]   skipped: already stored")
                continue
            streams = self._fetch_streams(int(activity["activity_id"]))
            transformed = transform_streams(activity, streams.get("latlng", []), streams.get("time", []))
            db.save_activity(self.conn, activity, transformed, streams_raw=json.dumps(streams))
            new_activity_count += 1
            print(f"[ingest]   saved: {len(transformed.get('path', []))} points")
        return new_activity_count

    def _fetch_streams(self, activity_id: int) -> dict:
        # Add random delay before stream requests
        random_delay(self._stream_delay_range, debug=self._debug_delays, shutdown_event=self.shutdown_event)
        
        response, payload = self.session.get_json(
            f"/activities/{activity_id}/streams",
            **{"stream_types[]": ["latlng", "time"]},
        )
        if response.status_code != 200:
            return {"latlng": [], "time": []}

        if isinstance(payload, dict):
            return {
                "latlng": payload.get("latlng") or [],
                "time": payload.get("time") or [],
            }

        if not isinstance(payload, list):
            return {"latlng": [], "time": []}

        parsed = {"latlng": [], "time": []}
        for entry in payload:
            stream_type = entry.get("type")
            data = entry.get("data") or []
            if stream_type in parsed:
                parsed[stream_type] = data
        return parsed

    def _backfill_athlete_recent(self, athlete_id: int, athlete: sqlite3.Row | None, max_steps: int) -> tuple[int, int]:
        """
        Phase 1: Recent catch-up — day-level gap fill from today backwards.
        Uses month-cursor pages but focuses on recent months first.
        """
        athlete_name = athlete["name"] if athlete else f"Athlete {athlete_id}"
        recent_cursor = athlete["backfill_recent_cursor_before"] if athlete and athlete["backfill_recent_cursor_before"] else None
        backfill_status = athlete["backfill_status"] if athlete else "pending"
        steps_used = 0
        total_new_activity_count = 0
        is_following = bool(athlete["is_following"]) if athlete else True

        if max_steps <= 0:
            return 0, 0

        # For fresh-start athletes (active/pending status without recent cursor), skip recent phase
        # and let deep phase handle full backfill. This maintains backward compatibility
        # for typical "start from today and go backward" backfill behavior.
        if (athlete and not recent_cursor and backfill_status in ("active", "pending")):
            logger.debug(f"{athlete_name} is a fresh start, skipping recent phase.")
            # Mark recent complete so we don't attempt it again in future runs
            db.update_backfill_progress(
                self.conn,
                athlete_id,
                cursor_before=None,
                oldest_seen_utc=None,
                status=backfill_status,
                completed=False,
                phase="recent",
                recent_cursor_before=None,
                recent_completed=True,
            )
            return 0, 0

        # Skip recent phase if already completed
        if athlete and athlete["backfill_recent_completed_at"]:
            logger.debug(f"{athlete_name} recent phase already completed, skipping.")
            return 0, 0

        current_month = datetime.now(self.settings.timezone).strftime("%Y%m")
        if recent_cursor is None:
            # Start from current month for recent catch-up
            recent_cursor = None
        elif recent_cursor >= current_month:
            recent_cursor = None

        while steps_used < max_steps:
            if self.shutdown_event is not None and self.shutdown_event.is_set():
                logger.info(f"Shutdown requested. Saving recent progress for {athlete_name}.")
                db.update_backfill_progress(
                    self.conn,
                    athlete_id,
                    cursor_before=None,
                    oldest_seen_utc=None,
                    status="active",
                    completed=False,
                    phase="recent",
                    recent_cursor_before=recent_cursor,
                    recent_completed=False,
                    coverage_checked_at=None,
                )
                return steps_used, total_new_activity_count

            logger.info(f"[recent] {athlete_name} checking from month cursor {recent_cursor or 'latest'}.")
            activities, next_cursor, status, issue = self.history_scraper.fetch_batch(
                athlete_id, recent_cursor, None, is_following=is_following
            )
            steps_used += 1

            if status == "forbidden":
                db.update_backfill_progress(
                    self.conn,
                    athlete_id,
                    cursor_before=None,
                    oldest_seen_utc=None,
                    status="forbidden",
                    completed=False,
                    phase="recent",
                    recent_cursor_before=recent_cursor,
                    recent_completed=False,
                )
                logger.info(f"{athlete_name} forbidden recent.")
                return steps_used, total_new_activity_count

            if status == "degraded":
                logger.warning(f"{athlete_name} degraded at month {recent_cursor}, advancing cursor to skip problematic month.")
                db.update_backfill_progress(
                    self.conn,
                    athlete_id,
                    cursor_before=None,
                    oldest_seen_utc=None,
                    status="degraded",
                    completed=False,
                    phase="recent",
                    recent_cursor_before=next_cursor,
                    recent_completed=False,
                    issue_code=issue.code if issue is not None else "unknown_issue",
                    issue_message=issue.message if issue is not None else "unknown issue",
                )
                logger.warning(f"{athlete_name} degraded recent, advanced cursor to {next_cursor} to avoid infinite loop.")
                return steps_used, total_new_activity_count

            new_count = self._ingest_activity_batch(activities) if activities else 0
            total_new_activity_count += new_count

            if status == "complete" or next_cursor is None:
                db.update_backfill_progress(
                    self.conn,
                    athlete_id,
                    cursor_before=None,
                    oldest_seen_utc=None,
                    status="complete",
                    completed=False,
                    phase="recent",
                    recent_cursor_before=next_cursor,
                    recent_completed=True,
                )
                logger.info(f"{athlete_name} recent phase complete. Steps: {steps_used}, new activities: {total_new_activity_count}.")
                return steps_used, total_new_activity_count

            recent_cursor = next_cursor
            db.update_backfill_progress(
                self.conn,
                athlete_id,
                cursor_before=None,
                oldest_seen_utc=None,
                status="active",
                completed=False,
                phase="recent",
                recent_cursor_before=recent_cursor,
                recent_completed=False,
                coverage_checked_at=None,
            )
            logger.info(f"[recent] {athlete_name} advanced to {next_cursor}. Added {new_count} new activities.")

        return steps_used, total_new_activity_count

    def _backfill_athlete_deep(self, athlete_id: int, athlete: sqlite3.Row | None, max_steps: int) -> tuple[int, int]:
        """
        Phase 2: Deep history resume — standard month-cursor backfill.
        """
        import sqlite3

        athlete_name = athlete["name"] if athlete else f"Athlete {athlete_id}"
        cursor_before = athlete["backfill_deep_cursor_before"] if athlete else None
        oldest_seen = athlete["backfill_oldest_seen_utc"] if athlete else None
        backfill_status = athlete["backfill_status"] if athlete else "pending"
        is_following = bool(athlete["is_following"]) if athlete else True

        if max_steps <= 0:
            return 0, 0

        current_month = datetime.now(self.settings.timezone).strftime("%Y%m")
        if cursor_before is None or backfill_status in ("pending", "needs_endpoint"):
            cursor_before = None
        elif cursor_before >= current_month:
            cursor_before = None

        total_new_activity_count = 0
        steps_used = 0

        while steps_used < max_steps:
            if self.shutdown_event is not None and self.shutdown_event.is_set():
                logger.info(f"Shutdown requested. Saving deep progress for {athlete_name}.")
                db.update_backfill_progress(
                    self.conn,
                    athlete_id,
                    cursor_before=cursor_before,
                    oldest_seen_utc=oldest_seen,
                    status="active",
                    completed=False,
                    phase="deep",
                )
                return steps_used, total_new_activity_count

            logger.info(f"[deep] {athlete_name} checking from month cursor {cursor_before or 'latest'}.")
            activities, next_cursor, status, issue = self.history_scraper.fetch_batch(
                athlete_id, cursor_before, oldest_seen, is_following=is_following
            )
            steps_used += 1

            if status == "forbidden":
                db.update_backfill_progress(
                    self.conn,
                    athlete_id,
                    cursor_before=cursor_before,
                    oldest_seen_utc=oldest_seen,
                    status="forbidden",
                    completed=True,
                    phase="deep",
                )
                logger.info(f"{athlete_name} forbidden deep.")
                return steps_used, total_new_activity_count

            if status == "degraded":
                logger.warning(f"{athlete_name} degraded deep at month {cursor_before or 'latest'}, advancing cursor to skip problematic month.")
                db.update_backfill_progress(
                    self.conn,
                    athlete_id,
                    cursor_before=next_cursor,
                    oldest_seen_utc=oldest_seen,
                    status="degraded",
                    completed=False,
                    phase="deep",
                    issue_code=issue.code if issue is not None else "unknown_issue",
                    issue_message=issue.message if issue is not None else "unknown issue",
                )
                detail = issue.message if issue is not None else "unknown issue"
                logger.warning(f"{athlete_name} degraded deep: {detail}. Advanced cursor to {next_cursor} to avoid infinite loop.")
                return steps_used, total_new_activity_count

            new_activity_count = self._ingest_activity_batch(activities) if activities else 0
            total_new_activity_count += new_activity_count
            oldest_seen = min((activity["start_date_utc"] for activity in activities), default=oldest_seen)

            if status == "complete":
                db.update_backfill_progress(
                    self.conn,
                    athlete_id,
                    cursor_before=next_cursor,
                    oldest_seen_utc=oldest_seen,
                    status="complete",
                    completed=True,
                    phase="deep",
                )
                logger.info(f"{athlete_name} deep phase complete. Steps: {steps_used}, new activities: {total_new_activity_count}.")
                return steps_used, total_new_activity_count

            cursor_before = next_cursor
            db.update_backfill_progress(
                self.conn,
                athlete_id,
                cursor_before=cursor_before,
                oldest_seen_utc=oldest_seen,
                status="gap" if status == "gap" else "active",
                completed=False,
                phase="deep",
            )
            logger.info(f"[deep] {athlete_name} advanced to {next_cursor}. Added {new_activity_count} new activities.")

        print(f"[backfill] {athlete_name} deep paused after {steps_used} step(s); will resume from {cursor_before}.")
        return steps_used, total_new_activity_count

