from __future__ import annotations

import platform
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from ingestion.config import BASE_DIR, load_settings


class SyncRunner:
    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None
        self.started_at: str | None = None
        self.log_path: Path | None = None
        self.command: list[str] | None = None
        self._log_handle = None

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def recommended_command(self, target_date: str | None = None, refresh_following_roster: bool = False) -> list[str]:
        settings = load_settings()
        resolved_date = target_date or datetime.now(settings.timezone).date().isoformat()
        command = [sys.executable, "-m", "ingestion.main", "--date", resolved_date, "--sync-only"]
        if refresh_following_roster:
            command.append("--refresh-following-roster")
        cookies_file = BASE_DIR / "cookies.txt"
        if cookies_file.exists():
            command.extend(["--auth-mode", "cookiestxt", "--cookies-file", str(cookies_file)])
        else:
            command.extend(["--auth-mode", "cookiestxt"])
        return command

    def start(self, target_date: str | None = None, refresh_following_roster: bool = False) -> dict:
        if self.is_running():
            return self.status()

        settings = load_settings()
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(settings.timezone).strftime("%Y%m%d_%H%M%S")
        self.log_path = settings.log_dir / f"daily_sync_{timestamp}.log"
        self.command = self.recommended_command(target_date, refresh_following_roster)

        self._log_handle = self.log_path.open("w", encoding="utf-8")
        
        if platform.system() == "Windows":
            extra_kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            extra_kwargs = {"start_new_session": True}
        
        self.process = subprocess.Popen(
            self.command,
            cwd=BASE_DIR,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            **extra_kwargs,
        )
        self.started_at = datetime.now().isoformat(timespec="seconds")
        return self.status()

    def stop(self) -> dict:
        if not self.is_running():
            return self.status()

        try:
            self.process.send_signal(signal.CTRL_BREAK_EVENT)
        except Exception:
            self.process.terminate()

        deadline = time.time() + 15
        while time.time() < deadline and self.is_running():
            time.sleep(0.25)

        if self.is_running():
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

        if self._log_handle and not self._log_handle.closed:
            self._log_handle.close()
            self._log_handle = None

        return self.status()

    def status(self) -> dict:
        return {
            "running": self.is_running(),
            "pid": self.process.pid if self.process else None,
            "started_at": self.started_at,
            "log_path": str(self.log_path) if self.log_path else None,
            "command": self.command or self.recommended_command(),
        }


runner = SyncRunner()
