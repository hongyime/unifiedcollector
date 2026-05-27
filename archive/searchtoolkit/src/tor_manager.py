"""
tor_manager.py — Tor daemon management for SearchToolkit.

Provides a standalone manager for the Tor Expert Bundle routing daemon.
Ensures the local SOCKS5 proxy (127.0.0.1:9050) is available for outbound
requests without requiring persistent background services.

Adapted from FORGE's forge/opsec/tor.py for use in SearchToolkit.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Optional


_LOG = logging.getLogger(__name__)


class TorManager:
    """Manages the lifecycle of the portable Tor daemon for SearchToolkit."""

    def __init__(self, tor_exe: Optional[Path] = None):
        self._tor_exe = tor_exe or self._ensure_tor_available()
        self._process: Optional[subprocess.Popen[str]] = None

    @staticmethod
    def _ensure_tor_available() -> Path:
        """Ensure tor.exe is available, unzipping it if necessary."""
        try:
            return TorManager._find_tor_exe()
        except FileNotFoundError:
            _LOG.info("tor.exe not found. Attempting to extract from archive...")
            if TorManager._extract_tor_archive():
                return TorManager._find_tor_exe()
            raise

    @staticmethod
    def _find_tor_exe() -> Path:
        """Locate tor.exe in the project root or expert bundle directories."""
        # Search in the searchtoolkit directory (where this script lives)
        toolkit_root = Path(__file__).parent
        candidates: list[Path] = []
        
        for path in toolkit_root.rglob("tor.exe"):
            lower = str(path).lower().replace("\\", "/")
            if "/tor/" in lower or "tor-expert-bundle" in lower:
                candidates.append(path)
        
        if candidates:
            candidates.sort(key=lambda p: len(str(p)))
            return candidates[0]

        raise FileNotFoundError(
            "Tor Expert Bundle (tor.exe) not found. "
            "Please ensure it is present or a valid archive is available in the searchtoolkit directory."
        )

    @staticmethod
    def _extract_tor_archive() -> bool:
        """Search for and extract a Tor archive (zip or tar.gz)."""
        toolkit_root = Path(__file__).parent
        archive_patterns = (
            "tor-expert-bundle-*.tar.gz",
            "tor-expert-bundle-*.tgz",
            "tor-expert-bundle-*.zip",
            "tor*.zip",
            "tor*.tar.gz",
            "tor*.tgz",
        )
        archives: list[Path] = []
        for pattern in archive_patterns:
            archives.extend(toolkit_root.glob(pattern))
        seen: set[Path] = set()
        deduped_archives: list[Path] = []
        for path in archives:
            if path in seen:
                continue
            seen.add(path)
            deduped_archives.append(path)
        deduped_archives.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        for archive_path in deduped_archives:
            _LOG.info("Extracting %s...", archive_path.name)
            try:
                if archive_path.suffix.lower() == ".zip":
                    with zipfile.ZipFile(archive_path, "r") as zip_ref:
                        zip_ref.extractall(toolkit_root)
                    if TorManager._find_tor_exe():
                        return True
                elif archive_path.name.lower().endswith(".tar.gz") or archive_path.suffix.lower() == ".tgz":
                    with tarfile.open(archive_path, "r:gz") as tar_ref:
                        tar_ref.extractall(toolkit_root)
                    if TorManager._find_tor_exe():
                        return True
            except FileNotFoundError:
                continue
            except Exception as e:
                _LOG.error("Failed to extract %s: %s", archive_path.name, e)
        
        _LOG.error("No usable Tor archive found in searchtoolkit directory.")
        return False

    @staticmethod
    def _is_port_open(host: str, port: int) -> bool:
        """Check if a port is currently open and listening."""
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            try:
                s.connect((host, port))
                return True
            except (ConnectionRefusedError, TimeoutError, socket.timeout):
                return False

    def start(self, wait_for_bootstrap: bool = True) -> bool:
        """Start the Tor daemon.

        Args:
            wait_for_bootstrap: If True, block until Tor is 100% bootstrapped.

        Returns:
            True if started successfully, False otherwise.
        """
        if self.is_running:
            _LOG.debug("Tor daemon is already running (process tracked).")
            return True

        if self._is_port_open("127.0.0.1", 9050):
            _LOG.info("Tor (or another SOCKS5 proxy) is already listening on 127.0.0.1:9050.")
            return True

        _LOG.info("Starting Tor daemon from %s...", self._tor_exe)
        
        # Start Tor with stdout/stderr piped so we can monitor bootstrap
        try:
            self._process = subprocess.Popen(
                [str(self._tor_exe)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
        except Exception as e:
            _LOG.error("Failed to start Tor: %s", e)
            return False

        if wait_for_bootstrap:
            return self._wait_for_bootstrap()
        
        return True

    def _wait_for_bootstrap(self, timeout: int = 60) -> bool:
        """Monitor Tor output for the 100% bootstrap message."""
        if not self._process or not self._process.stdout:
            return False

        start_time = time.time()
        _LOG.info("Waiting for Tor to bootstrap...")

        # Progress feedback: print dot every 5 seconds
        def _print_dots():
            for _ in range(12):  # 12 × 5s = 60s
                time.sleep(5)
                print(".", end="", flush=True)
        
        dot_thread = threading.Thread(target=_print_dots, daemon=True)
        dot_thread.start()

        while time.time() - start_time < timeout:
            line = self._process.stdout.readline()
            if not line:
                break
            
            # Tor log format: [notice] Bootstrapped 100% (done): Done
            if "Bootstrapped 100% (done)" in line:
                _LOG.info("Tor is ready (100% bootstrapped).")
                return True
            
            if "ERROR" in line.upper():
                _LOG.error("Tor error: %s", line.strip())
                return False

        print()  # newline after dots
        _LOG.error("Tor failed to bootstrap within %d seconds.", timeout)
        self.stop()
        return False

    def stop(self) -> None:
        """Terminate the Tor daemon process."""
        if self._process:
            _LOG.info("Stopping Tor daemon...")
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
            _LOG.info("Tor daemon stopped.")

    def rotate_circuit(self) -> bool:
        """Request Tor to rotate to a new circuit via SIGHUP (Linux/Mac only).

        On Windows, CTRL_BREAK_EVENT terminates the process rather than
        rotating the circuit. Proper rotation on Windows requires the Tor
        control port protocol; until that is implemented we log a warning
        and skip the rotation rather than killing the daemon.

        Returns:
            True if rotation signal was sent successfully.
        """
        if not self.is_running or not self._process:
            return False

        if os.name == "nt":
            _LOG.warning(
                "Tor circuit rotation via signal is not supported on Windows. "
                "Install the 'stem' library and configure ControlPort 9051 for rotation support."
            )
            return False

        try:
            import signal
            self._process.send_signal(signal.SIGHUP)
            _LOG.debug("Tor circuit rotation requested.")
            return True
        except Exception as e:
            _LOG.error("Failed to rotate Tor circuit: %s", e)
            return False

    @property
    def is_running(self) -> bool:
        """Check if the Tor process is currently running."""
        return self._process is not None and self._process.poll() is None

    @property
    def proxy_url(self) -> str:
        """Get the SOCKS5 proxy URL."""
        return "socks5://127.0.0.1:9050"

    def __enter__(self):
        """Context manager entry - start Tor."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - stop Tor."""
        self.stop()
        return False

