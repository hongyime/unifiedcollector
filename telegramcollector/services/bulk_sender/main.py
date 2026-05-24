"""
BulkSenderService — top-level process for the Bulk Sender Service.

Starts the job runner loop and the Streamlit dashboard subprocess,
handles OS signals, and performs orphan recovery on startup.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import subprocess
import sys
from typing import TYPE_CHECKING

from shared.config import settings
from shared.config_manager import SETTING_GROUPS, _bool_flag, config_manager

if TYPE_CHECKING:
    from services.bulk_sender.job_manager import JobManager
    from services.bulk_sender.sender import Sender

logger = logging.getLogger(__name__)

_MIN_SEND_DELAY = 1.0

# ---------------------------------------------------------------------------
# Task 11.1 — CLI argument parser
# ---------------------------------------------------------------------------

# Maps argparse attribute names → environment variable keys.
# Covers groups "bulk_sender" and "shared" (non-secret settings only).
ARG_TO_ENV_MAP: dict[str, str] = {
    defn.cli_flag.lstrip("-").replace("-", "_"): defn.key
    for group in ("bulk_sender", "shared")
    for defn in SETTING_GROUPS[group]
    if not defn.sensitive
}


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for bulk_sender and shared settings."""
    parser = argparse.ArgumentParser(
        description="Bulk Sender Service",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    for group_name in ("bulk_sender", "shared"):
        for defn in SETTING_GROUPS[group_name]:
            if defn.sensitive:
                continue  # never expose secrets via CLI
            if defn.python_type is bool:
                parser.add_argument(
                    defn.cli_flag,
                    dest=defn.cli_flag.lstrip("-").replace("-", "_"),
                    type=_bool_flag,
                    default=None,
                    metavar="BOOL",
                    help=defn.description,
                )
            else:
                parser.add_argument(
                    defn.cli_flag,
                    dest=defn.cli_flag.lstrip("-").replace("-", "_"),
                    type=defn.python_type,
                    default=None,
                    help=defn.description,
                )
    return parser


class BulkSenderService:
    """Top-level orchestrator for the Bulk Sender Service."""

    def __init__(
        self,
        job_manager: JobManager,
        sender: Sender,
        poll_interval: float = 5.0,
    ) -> None:
        """Initialise the service.

        Reads ``settings.BULK_SENDER_SEND_DELAY``, clamps it to >= 1.0, and
        logs a WARNING if the configured value was below the minimum.

        Args:
            job_manager: Persistence layer for job state.
            sender: Handles file enumeration, dedup, validation, and dispatch.
            poll_interval: Seconds between pending-job poll cycles.
        """
        configured_delay: float = settings.BULK_SENDER_SEND_DELAY
        effective_delay = max(configured_delay, _MIN_SEND_DELAY)

        if configured_delay < _MIN_SEND_DELAY:
            logger.warning(
                "BULK_SENDER_SEND_DELAY=%.3f is below the minimum of %.1f; "
                "clamping to %.1f",
                configured_delay,
                _MIN_SEND_DELAY,
                effective_delay,
            )

        self._effective_delay: float = effective_delay
        self.job_manager: JobManager = job_manager
        self.sender: Sender = sender
        self.poll_interval: float = poll_interval

        self._active_tasks: dict[int, asyncio.Task] = {}
        self._dashboard_process: subprocess.Popen | None = None
        self._running: bool = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Entry point called by __main__.

        1. Calls job_manager.recover_orphaned_jobs().
        2. Spawns the Streamlit dashboard subprocess.
        3. Registers SIGTERM / SIGINT handlers.
        4. Enters the job runner loop: polls for pending jobs every
           poll_interval seconds and dispatches each as an asyncio Task.
        """
        self._running = True

        # 1. Recover orphaned jobs
        recovered = self.job_manager.recover_orphaned_jobs()
        if recovered:
            logger.info("Recovered %d orphaned job(s) on startup", recovered)

        # 2. Spawn Streamlit dashboard subprocess
        self._dashboard_process = self._spawn_dashboard()

        # 3. Register signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        # 4. Job runner poll loop
        logger.info("BulkSenderService started, polling every %.1fs", self.poll_interval)
        while self._running:
            try:
                pending_jobs = self.job_manager.get_pending_jobs()
                for job in pending_jobs:
                    job_id = job["id"]
                    if job_id not in self._active_tasks:
                        task = asyncio.create_task(self._run_job(job_id))
                        self._active_tasks[job_id] = task
                        task.add_done_callback(lambda t, jid=job_id: self._active_tasks.pop(jid, None))
            except Exception as e:
                logger.error("Error in job runner poll loop: %s", e)
            await asyncio.sleep(self.poll_interval)

    async def stop(self) -> None:
        """Graceful shutdown.

        Cancels all active job tasks, waits for them to finish the current
        file, terminates the Streamlit subprocess, and closes DB connections.
        """
        logger.info("BulkSenderService stopping...")
        self._running = False

        # Cancel all active tasks
        for job_id, task in list(self._active_tasks.items()):
            task.cancel()
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks.values(), return_exceptions=True)
        self._active_tasks.clear()

        # Terminate dashboard subprocess
        if self._dashboard_process and self._dashboard_process.poll() is None:
            self._dashboard_process.terminate()
            try:
                self._dashboard_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._dashboard_process.kill()

        logger.info("BulkSenderService stopped")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_job(self, job_id: int) -> None:
        """Drive a single job from start to completion (or pause/cancel).

        Sets status='running', delegates to Sender.send_job(), then sets
        status='complete' or handles exceptions by setting status='failed'.

        Args:
            job_id: Primary key of the job to run.
        """
        job = self.job_manager.get_job(job_id)
        if job is None:
            logger.error("_run_job: job_id=%d not found", job_id)
            return

        stop_event = asyncio.Event()
        self.job_manager.set_status(job_id, "running")

        try:
            await self.sender.send_job(job, stop_event)
            # Check if job was paused or cancelled during execution
            current_job = self.job_manager.get_job(job_id)
            if current_job and current_job["status"] == "running":
                self.job_manager.set_status(job_id, "complete")
        except Exception as e:
            logger.error("_run_job: job_id=%d failed: %s", job_id, e, exc_info=True)
            self.job_manager.set_status(job_id, "failed")

    def _spawn_dashboard(self) -> "subprocess.Popen | None":
        """Spawn the Streamlit dashboard subprocess."""
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "streamlit", "run",
                 "services/bulk_sender/dashboard/app.py",
                 "--server.port", "8505",
                 "--server.headless", "true"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("Dashboard subprocess started (pid=%d)", proc.pid)
            return proc
        except Exception as e:
            logger.error("Failed to spawn dashboard: %s", e)
            return None


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    # Task 11.2 — apply CLI overrides before settings are consumed
    _parser = build_arg_parser()
    _args = _parser.parse_args()
    config_manager.apply_cli_overrides(_args, ARG_TO_ENV_MAP)

    from services.bulk_sender.job_manager import JobManager
    from services.bulk_sender.sender import Sender
    from shared.config import settings

    dsn = (
        f"postgresql://{settings.DB_USER}:{settings.DB_PASSWORD}"
        f"@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
    )

    job_manager = JobManager(dsn=dsn)
    sender = Sender(
        job_manager=job_manager,
        send_delay=settings.BULK_SENDER_SEND_DELAY,
        max_retries=settings.BULK_SENDER_MAX_RETRIES,
        sessions_path=settings.BULK_SENDER_SESSIONS_PATH,
        bot_tokens=[t for t in settings.BULK_SENDER_BOT_TOKENS.split(";") if t.strip()],
    )
    svc = BulkSenderService(job_manager=job_manager, sender=sender)
    asyncio.run(svc.start())
