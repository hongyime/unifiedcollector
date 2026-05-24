"""Atomic file operations for usernames.txt management."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import List, Set, Tuple


class UsernameFileManager:
    """Manages atomic, corruption-safe operations on the usernames file.

    All write operations use the temp-file + fsync + atomic-rename pattern
    to guarantee that the original file is never left in a partial state.
    """

    def __init__(self, file_path: Path = Path("data/usernames.txt")):
        """Initialize file manager with target file path.

        Args:
            file_path: Path to the usernames file
        """
        self.file_path = Path(file_path)

    # ── Read operations ───────────────────────────────────────────────────────

    def validate_file_integrity(self) -> bool:
        """Validate that the file exists and is readable.

        Returns:
            True if file is valid and readable
        """
        return self.file_path.exists() and self.file_path.is_file()

    def read_usernames(self) -> List[str]:
        """Read all usernames from file.

        Strips whitespace from each line and skips empty lines.

        Returns:
            List of usernames (stripped, non-empty)

        Raises:
            IOError: If file cannot be read
        """
        if not self.validate_file_integrity():
            raise IOError(
                f"Username file not found or not readable: {self.file_path}"
            )
        try:
            text = self.file_path.read_text(encoding="utf-8")
        except PermissionError as exc:
            raise IOError(
                f"Permission denied reading username file: {self.file_path}"
            ) from exc
        except OSError as exc:
            raise IOError(
                f"Error reading username file: {self.file_path}: {exc}"
            ) from exc

        return [line.strip() for line in text.splitlines() if line.strip()]

    # ── Write operations ──────────────────────────────────────────────────────

    def create_backup(self, suffix: str = ".bak") -> Path:
        """Create a timestamped backup of the usernames file.

        Args:
            suffix: Suffix to append to backup filename (default: ".bak")

        Returns:
            Path to the created backup file

        Raises:
            IOError: If backup cannot be created
        """
        timestamp = int(time.time())
        backup_path = self.file_path.with_suffix(f".{timestamp}{suffix}")
        try:
            content = self.file_path.read_bytes()
            backup_path.write_bytes(content)
        except OSError as exc:
            raise IOError(
                f"Failed to create backup of {self.file_path}: {exc}"
            ) from exc
        return backup_path

    def _write_atomic(self, lines: List[str]) -> None:
        """Write lines to the file atomically using temp + fsync + rename.

        Args:
            lines: Lines to write (without trailing newlines)
        """
        parent = self.file_path.parent
        parent.mkdir(parents=True, exist_ok=True)

        # Write to a temp file in the same directory (same filesystem)
        tmp_path = self.file_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))
                if lines:
                    fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            # Clean up temp file if write failed
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise IOError(f"Failed to write temp file {tmp_path}: {exc}") from exc

        # Atomic rename
        try:
            os.replace(str(tmp_path), str(self.file_path))
        except OSError as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise IOError(
                f"Failed to atomically replace {self.file_path}: {exc}"
            ) from exc

    def remove_usernames_atomic(
        self,
        usernames_to_remove: Set[str],
        create_backup: bool = True,
    ) -> Tuple[int, int]:
        """Atomically remove usernames from the file.

        Process:
        1. Read original file
        2. Filter out usernames to remove
        3. Validate file won't be empty
        4. Write to temp file with fsync
        5. Create backup of original (if create_backup=True)
        6. Atomic rename temp → original

        Args:
            usernames_to_remove: Set of usernames to remove
            create_backup: Whether to create a .bak backup before replacing

        Returns:
            Tuple of (original_count, remaining_count)

        Raises:
            IOError: If file operations fail
            ValueError: If all usernames would be removed (empty file prevention)
        """
        original_usernames = self.read_usernames()
        original_count = len(original_usernames)

        remaining = [u for u in original_usernames if u not in usernames_to_remove]
        remaining_count = len(remaining)

        if remaining_count == 0:
            raise ValueError(
                f"Removing {len(usernames_to_remove)} usernames would leave the file empty. "
                "Operation aborted to prevent data loss."
            )

        if create_backup:
            self.create_backup()

        self._write_atomic(remaining)

        return (original_count, remaining_count)
