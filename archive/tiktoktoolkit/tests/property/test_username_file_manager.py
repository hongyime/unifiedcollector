"""Property tests for UsernameFileManager.

Property 9:  Atomic File Write Pattern
Property 10: Backup Creation
Property 11: Empty File Prevention
Property 15: File Parsing Normalization
Property 16: Removal Return Value

Note: @given tests use tempfile.mkdtemp() to avoid the function-scoped
fixture health check from Hypothesis.
"""

import shutil
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.username_file_manager import UsernameFileManager


# ── Strategies ────────────────────────────────────────────────────────────────

_username_st = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
    min_size=1,
    max_size=30,
)

_usernames_list_st = st.lists(_username_st, min_size=2, max_size=20, unique=True)
_usernames_list_min1_st = st.lists(_username_st, min_size=1, max_size=20, unique=True)


def _make_file(usernames: list) -> tuple:
    """Create a temp dir + usernames file. Returns (dir_path, file_path, mgr)."""
    tmp_dir = tempfile.mkdtemp()
    file_path = Path(tmp_dir) / "usernames.txt"
    file_path.write_text("\n".join(usernames) + "\n", encoding="utf-8")
    mgr = UsernameFileManager(file_path=file_path)
    return tmp_dir, file_path, mgr


# ── Property 15: File Parsing Normalization ───────────────────────────────────

class TestFileParsing:
    """Property 15: Whitespace is stripped and empty lines are excluded.

    Validates: Requirements 9.2
    """

    @given(usernames=_usernames_list_min1_st)
    @settings(max_examples=30)
    def test_stripped_usernames_returned(self, usernames):
        """Usernames with surrounding whitespace are returned stripped."""
        tmp_dir = tempfile.mkdtemp()
        try:
            file_path = Path(tmp_dir) / "usernames.txt"
            file_path.write_text(
                "\n".join(f"  {u}  " for u in usernames) + "\n",
                encoding="utf-8",
            )
            mgr = UsernameFileManager(file_path=file_path)
            result = mgr.read_usernames()
            assert result == usernames
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @given(usernames=_usernames_list_min1_st)
    @settings(max_examples=30)
    def test_empty_lines_excluded(self, usernames):
        """Empty lines interspersed in the file are excluded from results."""
        tmp_dir = tempfile.mkdtemp()
        try:
            file_path = Path(tmp_dir) / "usernames.txt"
            lines = []
            for u in usernames:
                lines.append(u)
                lines.append("")
            file_path.write_text("\n".join(lines), encoding="utf-8")
            mgr = UsernameFileManager(file_path=file_path)
            result = mgr.read_usernames()
            assert result == usernames
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_whitespace_only_lines_excluded(self, tmp_path):
        file_path = tmp_path / "usernames.txt"
        file_path.write_text("alice\n   \nbob\n\t\ncharlie\n", encoding="utf-8")
        mgr = UsernameFileManager(file_path=file_path)
        assert mgr.read_usernames() == ["alice", "bob", "charlie"]

    def test_single_username_no_newline(self, tmp_path):
        file_path = tmp_path / "usernames.txt"
        file_path.write_text("alice", encoding="utf-8")
        mgr = UsernameFileManager(file_path=file_path)
        assert mgr.read_usernames() == ["alice"]


# ── Property 9: Atomic File Write Pattern ────────────────────────────────────

class TestAtomicFileWrite:
    """Property 9: Temp file is created and only replaces original after successful write.

    Validates: Requirements 5.1, 5.2, 5.3
    """

    @given(usernames=_usernames_list_st)
    @settings(max_examples=20)
    def test_original_file_updated_after_removal(self, usernames):
        """After atomic removal, the original file contains only remaining usernames."""
        tmp_dir, file_path, mgr = _make_file(usernames)
        try:
            to_remove = {usernames[0]}
            mgr.remove_usernames_atomic(to_remove, create_backup=False)
            result = mgr.read_usernames()
            assert usernames[0] not in result
            for u in usernames[1:]:
                assert u in result
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_no_temp_file_left_after_success(self, tmp_path):
        file_path = tmp_path / "usernames.txt"
        file_path.write_text("alice\nbob\ncharlie\n", encoding="utf-8")
        mgr = UsernameFileManager(file_path=file_path)
        mgr.remove_usernames_atomic({"alice"}, create_backup=False)
        tmp_file = file_path.with_suffix(".tmp")
        assert not tmp_file.exists()

    def test_original_content_preserved_on_empty_removal_set(self, tmp_path):
        file_path = tmp_path / "usernames.txt"
        file_path.write_text("alice\nbob\ncharlie\n", encoding="utf-8")
        mgr = UsernameFileManager(file_path=file_path)
        mgr.remove_usernames_atomic(set(), create_backup=False)
        assert mgr.read_usernames() == ["alice", "bob", "charlie"]


# ── Property 10: Backup Creation ─────────────────────────────────────────────

class TestBackupCreation:
    """Property 10: Backup file exists and contains original content.

    Validates: Requirements 5.4, 9.5
    """

    @given(usernames=_usernames_list_st)
    @settings(max_examples=20)
    def test_backup_file_exists_after_removal(self, usernames):
        tmp_dir, file_path, mgr = _make_file(usernames)
        try:
            mgr.remove_usernames_atomic({usernames[0]}, create_backup=True)
            bak_files = list(Path(tmp_dir).glob("*.bak"))
            assert len(bak_files) >= 1
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @given(usernames=_usernames_list_st)
    @settings(max_examples=20)
    def test_backup_contains_original_content(self, usernames):
        tmp_dir, file_path, mgr = _make_file(usernames)
        try:
            mgr.remove_usernames_atomic({usernames[0]}, create_backup=True)
            bak_files = list(Path(tmp_dir).glob("*.bak"))
            assert len(bak_files) >= 1
            backup_content = bak_files[0].read_text(encoding="utf-8")
            for u in usernames:
                assert u in backup_content
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_create_backup_returns_path(self, tmp_path):
        file_path = tmp_path / "usernames.txt"
        file_path.write_text("alice\nbob\n", encoding="utf-8")
        mgr = UsernameFileManager(file_path=file_path)
        backup_path = mgr.create_backup()
        assert backup_path.exists()
        assert backup_path.read_text(encoding="utf-8") == "alice\nbob\n"

    def test_no_backup_when_create_backup_false(self, tmp_path):
        file_path = tmp_path / "usernames.txt"
        file_path.write_text("alice\nbob\ncharlie\n", encoding="utf-8")
        mgr = UsernameFileManager(file_path=file_path)
        mgr.remove_usernames_atomic({"alice"}, create_backup=False)
        bak_files = list(tmp_path.glob("*.bak"))
        assert len(bak_files) == 0


# ── Property 11: Empty File Prevention ───────────────────────────────────────

class TestEmptyFilePrevention:
    """Property 11: ValueError is raised when all usernames would be removed.

    Validates: Requirements 5.5
    """

    @given(usernames=_usernames_list_min1_st)
    @settings(max_examples=20)
    def test_removing_all_raises_value_error(self, usernames):
        tmp_dir, file_path, mgr = _make_file(usernames)
        try:
            with pytest.raises(ValueError):
                mgr.remove_usernames_atomic(set(usernames), create_backup=False)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @given(usernames=_usernames_list_min1_st)
    @settings(max_examples=20)
    def test_original_file_unchanged_after_failed_removal(self, usernames):
        tmp_dir, file_path, mgr = _make_file(usernames)
        try:
            try:
                mgr.remove_usernames_atomic(set(usernames), create_backup=False)
            except ValueError:
                pass
            result = mgr.read_usernames()
            assert set(result) == set(usernames)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_removing_subset_does_not_raise(self, tmp_path):
        file_path = tmp_path / "usernames.txt"
        file_path.write_text("alice\nbob\ncharlie\n", encoding="utf-8")
        mgr = UsernameFileManager(file_path=file_path)
        mgr.remove_usernames_atomic({"alice", "bob"}, create_backup=False)


# ── Property 16: Removal Return Value ────────────────────────────────────────

class TestRemovalReturnValue:
    """Property 16: Returned tuple has original_count > remaining_count.

    Validates: Requirements 9.4
    """

    @given(usernames=_usernames_list_st)
    @settings(max_examples=20)
    def test_return_value_original_greater_than_remaining(self, usernames):
        tmp_dir, file_path, mgr = _make_file(usernames)
        try:
            original_count, remaining_count = mgr.remove_usernames_atomic(
                {usernames[0]}, create_backup=False
            )
            assert original_count > remaining_count
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @given(usernames=_usernames_list_st)
    @settings(max_examples=20)
    def test_return_value_counts_are_accurate(self, usernames):
        tmp_dir, file_path, mgr = _make_file(usernames)
        try:
            original_count, remaining_count = mgr.remove_usernames_atomic(
                {usernames[0]}, create_backup=False
            )
            assert original_count == len(usernames)
            assert remaining_count == len(usernames) - 1
            actual_remaining = mgr.read_usernames()
            assert len(actual_remaining) == remaining_count
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
