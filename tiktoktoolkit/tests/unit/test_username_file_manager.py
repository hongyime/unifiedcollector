"""Unit tests for UsernameFileManager — edge cases."""

import pytest
from pathlib import Path

from src.username_file_manager import UsernameFileManager


# ── validate_file_integrity ───────────────────────────────────────────────────

class TestValidateFileIntegrity:
    def test_existing_file_is_valid(self, tmp_path):
        f = tmp_path / "usernames.txt"
        f.write_text("alice\n")
        mgr = UsernameFileManager(file_path=f)
        assert mgr.validate_file_integrity() is True

    def test_missing_file_is_invalid(self, tmp_path):
        mgr = UsernameFileManager(file_path=tmp_path / "nonexistent.txt")
        assert mgr.validate_file_integrity() is False

    def test_directory_is_invalid(self, tmp_path):
        mgr = UsernameFileManager(file_path=tmp_path)
        assert mgr.validate_file_integrity() is False


# ── read_usernames ────────────────────────────────────────────────────────────

class TestReadUsernames:
    def test_reads_simple_file(self, tmp_path):
        f = tmp_path / "usernames.txt"
        f.write_text("alice\nbob\ncharlie\n")
        mgr = UsernameFileManager(file_path=f)
        assert mgr.read_usernames() == ["alice", "bob", "charlie"]

    def test_strips_whitespace(self, tmp_path):
        f = tmp_path / "usernames.txt"
        f.write_text("  alice  \n  bob\t\n")
        mgr = UsernameFileManager(file_path=f)
        assert mgr.read_usernames() == ["alice", "bob"]

    def test_skips_empty_lines(self, tmp_path):
        f = tmp_path / "usernames.txt"
        f.write_text("alice\n\nbob\n\n\ncharlie\n")
        mgr = UsernameFileManager(file_path=f)
        assert mgr.read_usernames() == ["alice", "bob", "charlie"]

    def test_file_not_found_raises_ioerror(self, tmp_path):
        mgr = UsernameFileManager(file_path=tmp_path / "missing.txt")
        with pytest.raises(IOError):
            mgr.read_usernames()

    def test_empty_file_returns_empty_list(self, tmp_path):
        f = tmp_path / "usernames.txt"
        f.write_text("")
        mgr = UsernameFileManager(file_path=f)
        assert mgr.read_usernames() == []

    def test_file_with_only_whitespace_returns_empty_list(self, tmp_path):
        f = tmp_path / "usernames.txt"
        f.write_text("   \n\t\n  \n")
        mgr = UsernameFileManager(file_path=f)
        assert mgr.read_usernames() == []


# ── create_backup ─────────────────────────────────────────────────────────────

class TestCreateBackup:
    def test_backup_file_created(self, tmp_path):
        f = tmp_path / "usernames.txt"
        f.write_text("alice\nbob\n")
        mgr = UsernameFileManager(file_path=f)
        backup = mgr.create_backup()
        assert backup.exists()

    def test_backup_has_same_content(self, tmp_path):
        f = tmp_path / "usernames.txt"
        content = "alice\nbob\ncharlie\n"
        f.write_text(content)
        mgr = UsernameFileManager(file_path=f)
        backup = mgr.create_backup()
        assert backup.read_text(encoding="utf-8") == content

    def test_backup_path_returned(self, tmp_path):
        f = tmp_path / "usernames.txt"
        f.write_text("alice\n")
        mgr = UsernameFileManager(file_path=f)
        backup = mgr.create_backup()
        assert isinstance(backup, Path)

    def test_backup_missing_file_raises_ioerror(self, tmp_path):
        mgr = UsernameFileManager(file_path=tmp_path / "missing.txt")
        with pytest.raises(IOError):
            mgr.create_backup()


# ── remove_usernames_atomic ───────────────────────────────────────────────────

class TestRemoveUsernamesAtomic:
    def test_removes_specified_usernames(self, tmp_path):
        f = tmp_path / "usernames.txt"
        f.write_text("alice\nbob\ncharlie\n")
        mgr = UsernameFileManager(file_path=f)
        mgr.remove_usernames_atomic({"alice", "bob"}, create_backup=False)
        assert mgr.read_usernames() == ["charlie"]

    def test_returns_correct_counts(self, tmp_path):
        f = tmp_path / "usernames.txt"
        f.write_text("alice\nbob\ncharlie\n")
        mgr = UsernameFileManager(file_path=f)
        orig, remaining = mgr.remove_usernames_atomic({"alice"}, create_backup=False)
        assert orig == 3
        assert remaining == 2

    def test_removing_all_raises_value_error(self, tmp_path):
        f = tmp_path / "usernames.txt"
        f.write_text("alice\nbob\n")
        mgr = UsernameFileManager(file_path=f)
        with pytest.raises(ValueError, match="empty"):
            mgr.remove_usernames_atomic({"alice", "bob"}, create_backup=False)

    def test_file_unchanged_after_value_error(self, tmp_path):
        f = tmp_path / "usernames.txt"
        f.write_text("alice\nbob\n")
        mgr = UsernameFileManager(file_path=f)
        try:
            mgr.remove_usernames_atomic({"alice", "bob"}, create_backup=False)
        except ValueError:
            pass
        assert set(mgr.read_usernames()) == {"alice", "bob"}

    def test_removing_nonexistent_username_is_noop(self, tmp_path):
        f = tmp_path / "usernames.txt"
        f.write_text("alice\nbob\n")
        mgr = UsernameFileManager(file_path=f)
        orig, remaining = mgr.remove_usernames_atomic({"nonexistent"}, create_backup=False)
        assert orig == 2
        assert remaining == 2
        assert mgr.read_usernames() == ["alice", "bob"]

    def test_empty_removal_set_is_noop(self, tmp_path):
        f = tmp_path / "usernames.txt"
        f.write_text("alice\nbob\n")
        mgr = UsernameFileManager(file_path=f)
        orig, remaining = mgr.remove_usernames_atomic(set(), create_backup=False)
        assert orig == 2
        assert remaining == 2

    def test_creates_backup_when_requested(self, tmp_path):
        f = tmp_path / "usernames.txt"
        f.write_text("alice\nbob\ncharlie\n")
        mgr = UsernameFileManager(file_path=f)
        mgr.remove_usernames_atomic({"alice"}, create_backup=True)
        bak_files = list(tmp_path.glob("*.bak"))
        assert len(bak_files) == 1

    def test_no_backup_when_not_requested(self, tmp_path):
        f = tmp_path / "usernames.txt"
        f.write_text("alice\nbob\ncharlie\n")
        mgr = UsernameFileManager(file_path=f)
        mgr.remove_usernames_atomic({"alice"}, create_backup=False)
        bak_files = list(tmp_path.glob("*.bak"))
        assert len(bak_files) == 0

    def test_file_not_found_raises_ioerror(self, tmp_path):
        mgr = UsernameFileManager(file_path=tmp_path / "missing.txt")
        with pytest.raises(IOError):
            mgr.remove_usernames_atomic({"alice"}, create_backup=False)

    def test_order_preserved_after_removal(self, tmp_path):
        """Remaining usernames must preserve original file order."""
        f = tmp_path / "usernames.txt"
        f.write_text("alice\nbob\ncharlie\ndave\n")
        mgr = UsernameFileManager(file_path=f)
        mgr.remove_usernames_atomic({"bob"}, create_backup=False)
        assert mgr.read_usernames() == ["alice", "charlie", "dave"]
