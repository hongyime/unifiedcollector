"""Interactive UI for invalid username cleanup."""

from __future__ import annotations

import datetime
from io import StringIO
from typing import List, Optional, Set

from .models import InvalidReason, InvalidUsernameRecord
from .invalid_username_tracker import InvalidUsernameTracker
from .username_file_manager import UsernameFileManager


def _format_reason(reason: InvalidReason) -> str:
    """Return a human-readable label for an InvalidReason."""
    labels = {
        InvalidReason.NOT_FOUND: "Not Found",
        InvalidReason.ACCOUNT_DELETED: "Account Deleted",
        InvalidReason.USERNAME_CHANGED: "Username Changed",
        InvalidReason.PRIVATE_BANNED: "Private / Banned",
        InvalidReason.UNKNOWN: "Unknown",
    }
    return labels.get(reason, reason.value)


def _format_timestamp(ts: float) -> str:
    """Return a human-readable UTC timestamp string."""
    try:
        dt = datetime.datetime.utcfromtimestamp(ts)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (OSError, OverflowError, ValueError):
        return str(ts)


class CleanupUI:
    """Interactive UI for reviewing and removing invalid usernames.

    All output goes to the provided stream (defaults to sys.stdout) so
    that tests can capture it without monkey-patching builtins.
    """

    def __init__(
        self,
        tracker: InvalidUsernameTracker,
        file_manager: UsernameFileManager,
        output=None,
        input_fn=None,
    ):
        """Initialize UI with tracker and file manager.

        Args:
            tracker: InvalidUsernameTracker instance
            file_manager: UsernameFileManager instance
            output: Output stream (defaults to sys.stdout)
            input_fn: Callable for reading user input (defaults to input())
        """
        import sys
        self._tracker = tracker
        self._file_manager = file_manager
        self._out = output or sys.stdout
        self._input = input_fn or input

    def _print(self, *args, **kwargs) -> None:
        """Write to the configured output stream."""
        print(*args, file=self._out, **kwargs)

    # ── Display helpers ───────────────────────────────────────────────────────

    def display_invalid_usernames(self, records: List[InvalidUsernameRecord]) -> None:
        """Display a formatted list of invalid usernames.

        Format per entry:
        - Username
        - Reason
        - First detected timestamp
        - Number of detection attempts

        Args:
            records: List of InvalidUsernameRecord objects to display
        """
        if not records:
            self._print("  (no invalid usernames)")
            return

        # Group by username to count detections
        from collections import defaultdict
        counts: dict = defaultdict(int)
        first_seen: dict = {}
        latest_reason: dict = {}
        latest_msg: dict = {}

        for r in records:
            counts[r.username] += 1
            if r.username not in first_seen or r.detected_at < first_seen[r.username]:
                first_seen[r.username] = r.detected_at
            latest_reason[r.username] = r.reason
            if r.error_message:
                latest_msg[r.username] = r.error_message

        self._print(f"\n  {'#':<4} {'Username':<30} {'Reason':<20} {'First Seen':<22} {'Detections'}")
        self._print("  " + "-" * 90)

        for i, username in enumerate(sorted(counts.keys()), start=1):
            reason_str = _format_reason(latest_reason[username])
            ts_str = _format_timestamp(first_seen[username])
            det_count = counts[username]
            self._print(f"  {i:<4} {username:<30} {reason_str:<20} {ts_str:<22} {det_count}")

    def confirm_removal(self, usernames: Set[str], original_count: int) -> bool:
        """Confirm removal operation with the user.

        Displays:
        - Number of usernames to remove
        - Number of usernames remaining
        - Warning if removing > 50% of the file

        Args:
            usernames: Set of usernames to remove
            original_count: Total number of usernames currently in the file

        Returns:
            True if user confirms, False otherwise
        """
        remove_count = len(usernames)
        remaining_count = original_count - remove_count
        pct = (remove_count / original_count * 100) if original_count > 0 else 0

        self._print(f"\n  Usernames to remove : {remove_count}")
        self._print(f"  Usernames remaining : {remaining_count}")

        if pct > 50:
            self._print(
                f"\n  ⚠  WARNING: You are about to remove {pct:.0f}% of your username list!"
            )

        self._print()
        answer = self._input("  Confirm removal? [y/N] ").strip().lower()
        return answer in ("y", "yes")

    def display_removal_result(
        self,
        removed_count: int,
        remaining_count: int,
        backup_path=None,
    ) -> None:
        """Display the result of a removal operation.

        Shows:
        - Number of usernames removed
        - Number of usernames remaining
        - Backup file location (if provided)
        - Success message

        Args:
            removed_count: Number of usernames that were removed
            remaining_count: Number of usernames still in the file
            backup_path: Optional Path to the backup file
        """
        self._print(f"\n  ✓ Removed {removed_count} username(s).")
        self._print(f"  ✓ {remaining_count} username(s) remain in the file.")
        if backup_path is not None:
            self._print(f"  ✓ Backup saved to: {backup_path}")
        self._print("  ✓ Cleanup complete.")

    # ── Main prompt ───────────────────────────────────────────────────────────

    def present_cleanup_prompt(self) -> Optional[Set[str]]:
        """Present invalid usernames and get user decision.

        Displays:
        - Count of invalid usernames detected
        - List of invalid usernames with reasons
        - Options: Remove all, Remove selected, Keep all, View details

        Returns:
            Set of usernames to remove, or None if user chose to keep all
        """
        records = self._tracker.get_invalid_records()
        invalid_usernames = {r.username for r in records}

        if not invalid_usernames:
            self._print("\n  No invalid usernames detected.")
            return None

        self._print(f"\n{'='*60}")
        self._print(f"  Invalid Username Cleanup")
        self._print(f"{'='*60}")
        self._print(f"\n  {len(invalid_usernames)} invalid username(s) detected during this session.")

        while True:
            self._print("\n  Options:")
            self._print("    [1] Remove all invalid usernames")
            self._print("    [2] Remove selected usernames")
            self._print("    [3] Keep all (skip cleanup)")
            self._print("    [4] View details")
            self._print()

            choice = self._input("  Enter choice [1-4]: ").strip()

            if choice == "1":
                return set(invalid_usernames)

            elif choice == "2":
                self.display_invalid_usernames(records)
                self._print()
                raw = self._input(
                    "  Enter numbers to remove (comma-separated, e.g. 1,3): "
                ).strip()
                if not raw:
                    self._print("  No selection made.")
                    continue

                # Build ordered list of unique usernames for index lookup
                ordered = sorted(invalid_usernames)
                selected: Set[str] = set()
                for part in raw.split(","):
                    part = part.strip()
                    if not part.isdigit():
                        self._print(f"  Skipping invalid entry: '{part}'")
                        continue
                    idx = int(part) - 1
                    if 0 <= idx < len(ordered):
                        selected.add(ordered[idx])
                    else:
                        self._print(f"  Index {part} out of range, skipping.")

                if selected:
                    return selected
                self._print("  No valid usernames selected.")

            elif choice == "3":
                self._print("\n  Skipping cleanup. No changes made.")
                return None

            elif choice == "4":
                self.display_invalid_usernames(records)

            else:
                self._print(f"  Invalid choice '{choice}'. Please enter 1, 2, 3, or 4.")
