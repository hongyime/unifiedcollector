"""Session distribution logic for the Login Bot.

Copies a newly authenticated session file to every service subdirectory
under SESSIONS_BASE_PATH so that all downstream services can use the
account immediately.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


class SessionRouter:
    """Copies session files from the source directory to all target service dirs."""

    def __init__(self, base_path: str, source_subdir: str = "collector") -> None:
        """
        Args:
            base_path:     Root sessions directory (value of Settings.SESSIONS_BASE_PATH).
            source_subdir: Subdirectory that holds the canonical session files.
        """
        self.base_path: Path = Path(base_path)
        self.source_subdir: str = source_subdir

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def distribute(self, session_stem: str) -> list[str]:
        """Copy <base_path>/<source_subdir>/<session_stem>.session to every
        immediate subdirectory of base_path except source_subdir.

        Returns a list of subdirectory names that received a valid (non-zero)
        copy. Logs and skips any target that fails or produces a zero-byte file.
        """
        src = self.base_path / self.source_subdir / f"{session_stem}.session"
        succeeded: list[str] = []

        for target_dir in self._list_target_dirs():
            if self._copy_session(src, target_dir):
                succeeded.append(target_dir.name)

        return succeeded

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _list_target_dirs(self) -> list[Path]:
        """Return all immediate subdirectories of base_path except source_subdir."""
        if not self.base_path.is_dir():
            return []
        return [
            entry
            for entry in self.base_path.iterdir()
            if entry.is_dir() and entry.name != self.source_subdir
        ]

    def _copy_session(self, src: Path, dst_dir: Path) -> bool:
        """Copy *src* into *dst_dir*, creating the directory if necessary.

        Returns True only when the destination file exists and has a non-zero
        byte size. Logs and returns False on any failure or zero-byte result.
        """
        try:
            os.makedirs(dst_dir, exist_ok=True)
            dst = dst_dir / src.name
            shutil.copy2(src, dst)
            if dst.stat().st_size > 0:
                return True
            logger.error(
                "Session copy produced a zero-byte file: %s → %s", src, dst
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to copy session %s to %s: %s", src, dst_dir, exc
            )
            return False
