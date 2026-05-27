"""
Username Database CLI Commands - Manage the username database from the command line.

Provides commands for:
- Viewing usernames by source account
- Migrating from flat file
- Exporting to flat file
- Viewing database statistics
"""
from __future__ import annotations

import argparse
import os
import sys

from src.commands.base import BaseCommand
from src.username_database import UsernameDatabase
from src.config import INSTAGRAM_ACCOUNTS, DATA_DIR


def _get_account_names() -> list[str]:
    """Return list of configured account names."""
    return [acc["name"] for acc in INSTAGRAM_ACCOUNTS]


def _get_db(db_path: str | None = None) -> UsernameDatabase:
    """Create a UsernameDatabase instance, optionally with a custom path."""
    if db_path:
        return UsernameDatabase(db_path=db_path)
    return UsernameDatabase()


# ---------------------------------------------------------------------------
# Command: username-db list
# ---------------------------------------------------------------------------

class UsernameDbListCommand(BaseCommand):
    """View usernames stored in the database, optionally filtered by source account."""

    name = "username-db-list"
    description = "View usernames by source account"
    help_text = "List usernames in the database, optionally filtered by source account"

    def _add_arguments(self):
        self.parser.add_argument(
            "--source",
            type=str,
            default=None,
            help="Filter by source account name",
        )
        self.parser.add_argument(
            "--db",
            type=str,
            default=None,
            help="Path to database file (default: data/username_database.json)",
        )
        self.parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of usernames to display",
        )

    def execute(self, args: argparse.Namespace) -> int:
        try:
            db = _get_db(args.db)

            if args.source:
                records = db.get_usernames_by_source(args.source)
                if not records:
                    self.print_info(f"No usernames found for source account: {args.source}")
                    return 0
                self.print_info(f"Usernames scraped by '{args.source}' ({len(records)} total):")
            else:
                records = db.get_all_usernames()
                if not records:
                    self.print_info("Database is empty.")
                    return 0
                self.print_info(f"All usernames ({len(records)} total):")

            if args.limit:
                records = records[: args.limit]

            for record in records:
                last = record.last_accessed
                last_str = f", last accessed: {record.added_datetime[:10]}" if last else ""
                print(f"  {record.username} (source: {record.source_account}{last_str})")

            return 0

        except Exception as e:
            self.print_error(f"Failed to list usernames: {e}")
            return 1


# ---------------------------------------------------------------------------
# Command: username-db migrate
# ---------------------------------------------------------------------------

class UsernameDbMigrateCommand(BaseCommand):
    """Migrate usernames from a flat file into the structured database."""

    name = "username-db-migrate"
    description = "Migrate usernames from flat file to database"
    help_text = "Import usernames from a flat text file (one per line) into the database"

    def _add_arguments(self):
        self.parser.add_argument(
            "filepath",
            type=str,
            help="Path to flat file with usernames (one per line)",
        )
        self.parser.add_argument(
            "--source",
            type=str,
            required=True,
            help="Source account name to attribute all migrated usernames to",
        )
        self.parser.add_argument(
            "--db",
            type=str,
            default=None,
            help="Path to database file (default: data/username_database.json)",
        )

    def validate_args(self, args: argparse.Namespace) -> list[str]:
        errors = []
        if not os.path.exists(args.filepath):
            errors.append(f"File not found: {args.filepath}")
        account_names = _get_account_names()
        if account_names and args.source not in account_names:
            errors.append(
                f"Unknown source account '{args.source}'. "
                f"Available: {', '.join(account_names)}"
            )
        return errors

    def execute(self, args: argparse.Namespace) -> int:
        errors = self.validate_args(args)
        if errors:
            for err in errors:
                self.print_error(err)
            return 1

        try:
            db = _get_db(args.db)
            result = db.migrate_from_flat_file(
                filepath=args.filepath,
                default_source=args.source,
            )

            if "error" in result:
                self.print_error(f"Migration failed: {result['error']}")
                return 1

            stats = result["statistics"]
            self.print_success("Migration complete:")
            print(f"  Total lines:  {result['total_lines']}")
            print(f"  Added:        {stats['added']}")
            print(f"  Duplicates:   {stats['duplicates']}")
            print(f"  Invalid:      {stats['invalid']}")
            print(f"  Skipped:      {stats['skipped']}")
            print(f"  Backup:       {result['backup_path']}")
            return 0

        except Exception as e:
            self.print_error(f"Migration failed: {e}")
            return 1


# ---------------------------------------------------------------------------
# Command: username-db export
# ---------------------------------------------------------------------------

class UsernameDbExportCommand(BaseCommand):
    """Export all usernames from the database to a flat text file."""

    name = "username-db-export"
    description = "Export usernames to flat file"
    help_text = "Export all usernames from the database to a flat text file (one per line)"

    def _add_arguments(self):
        self.parser.add_argument(
            "filepath",
            type=str,
            help="Output file path",
        )
        self.parser.add_argument(
            "--source",
            type=str,
            default=None,
            help="Export only usernames from this source account",
        )
        self.parser.add_argument(
            "--db",
            type=str,
            default=None,
            help="Path to database file (default: data/username_database.json)",
        )

    def execute(self, args: argparse.Namespace) -> int:
        try:
            db = _get_db(args.db)

            if args.source:
                records = db.get_usernames_by_source(args.source)
                usernames = [r.username for r in records]
                try:
                    with open(args.filepath, "w", encoding="utf-8") as f:
                        for username in usernames:
                            f.write(f"{username}\n")
                    count = len(usernames)
                except Exception as e:
                    self.print_error(f"Failed to write file: {e}")
                    return 1
            else:
                count = db.export_to_flat_file(args.filepath)

            if count == 0:
                self.print_warning("No usernames exported (database may be empty).")
            else:
                self.print_success(f"Exported {count} usernames to {args.filepath}")
            return 0

        except Exception as e:
            self.print_error(f"Export failed: {e}")
            return 1


# ---------------------------------------------------------------------------
# Command: username-db stats
# ---------------------------------------------------------------------------

class UsernameDbStatsCommand(BaseCommand):
    """Display statistics about the username database."""

    name = "username-db-stats"
    description = "View database statistics"
    help_text = "Show statistics about the username database (total counts, per-account breakdown)"

    def _add_arguments(self):
        self.parser.add_argument(
            "--db",
            type=str,
            default=None,
            help="Path to database file (default: data/username_database.json)",
        )

    def execute(self, args: argparse.Namespace) -> int:
        try:
            db = _get_db(args.db)
            all_records = db.get_all_usernames()
            total = len(all_records)

            print("Username Database Statistics")
            print("=" * 40)
            print(f"Total usernames: {total}")

            if total == 0:
                self.print_info("Database is empty.")
                return 0

            # Per-account breakdown
            print("\nBy source account:")
            for account_name, usernames in db.source_account_index.items():
                count = len(usernames)
                pct = (count / total * 100) if total else 0
                print(f"  {account_name}: {count} ({pct:.1f}%)")

            # Accessed vs never accessed
            accessed = sum(1 for r in all_records if r.last_accessed is not None)
            print(f"\nAccessed:        {accessed}")
            print(f"Never accessed:  {total - accessed}")

            # Database file info
            db_path = db.db_path
            if os.path.exists(db_path):
                size_kb = os.path.getsize(db_path) / 1024
                print(f"\nDatabase file:   {db_path}")
                print(f"File size:       {size_kb:.1f} KB")

            return 0

        except Exception as e:
            self.print_error(f"Failed to get statistics: {e}")
            return 1


__all__ = [
    "UsernameDbListCommand",
    "UsernameDbMigrateCommand",
    "UsernameDbExportCommand",
    "UsernameDbStatsCommand",
]


