"""Tests that every main.py CLI subcommand exits cleanly with --help.

These tests mirror what the .bat menu options invoke, ensuring no command
crashes on startup or produces an unhandled exception.
"""
import subprocess
import sys
import os
import pytest

# Path to main.py (one level up from tests/)
_MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
_PYTHON = sys.executable


def _run(*args, timeout=30):
    """Run main.py with the given args and return the CompletedProcess."""
    return subprocess.run(
        [_PYTHON, _MAIN] + list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ── Top-level --help ──────────────────────────────────────────────────────────

def test_top_level_help():
    result = _run("--help")
    assert result.returncode == 0, result.stderr


# ── Each subcommand --help ────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "list",
    "test-all",
    "access-stats",
    "analyze",
    "analyze-profiles",
    "db-migrate",
    "cleanup-bak",
    "db-reset",
    "list-usernames",
])
def test_subcommand_help(cmd):
    """Commands that accept --help should exit 0."""
    result = _run(cmd, "--help")
    assert result.returncode == 0, f"{cmd} --help failed:\n{result.stderr}"


@pytest.mark.parametrize("cmd,extra", [
    ("login", ["--help"]),
    ("priority-analysis", ["--help"]),
    ("spider", ["--help"]),
    ("download", ["--help"]),
    ("following-download", ["--help"]),
    ("selective-download", ["--help"]),
    ("progress", ["--help"]),
    ("add-username", ["--help"]),
])
def test_subcommand_with_args_help(cmd, extra):
    result = _run(cmd, *extra)
    assert result.returncode == 0, f"{cmd} {' '.join(extra)} failed:\n{result.stderr}"


# ── Safe read-only commands ───────────────────────────────────────────────────

def test_list_command():
    """list should always succeed (reads .env only)."""
    result = _run("list")
    assert result.returncode == 0, result.stderr


def test_cleanup_bak_no_files():
    """cleanup-bak with no .bak files should exit 0 cleanly."""
    result = _run("cleanup-bak")
    assert result.returncode == 0, result.stderr
    # Should report nothing to clean or confirm deletions
    assert "bak" in result.stdout.lower() or "clean" in result.stdout.lower()


def test_progress_subcommand_help():
    result = _run("progress", "show", "--help")
    assert result.returncode == 0, result.stderr


def test_progress_resume_help():
    result = _run("progress", "resume", "--help")
    assert result.returncode == 0, result.stderr


def test_progress_clear_help():
    result = _run("progress", "clear", "--help")
    assert result.returncode == 0, result.stderr


# ── Verify no raw tracebacks on bad input ─────────────────────────────────────

def test_no_traceback_on_unknown_command():
    """Unknown commands should produce argparse error, not a Python traceback."""
    result = _run("nonexistent-command-xyz")
    # argparse exits with code 2 for unknown commands — that's fine
    assert "Traceback" not in result.stderr, "Raw traceback leaked to stderr"


def test_no_traceback_on_missing_required_arg():
    """login without a name should produce argparse error, not a traceback."""
    result = _run("login")
    assert "Traceback" not in result.stderr, "Raw traceback leaked to stderr"


# ── New DB-backed commands ────────────────────────────────────────────────────

def test_db_reset_command():
    """db-reset should exit 0 and report tables cleared."""
    result = _run("db-reset")
    assert result.returncode == 0, result.stderr
    assert "cleared" in result.stdout.lower() or "reset" in result.stdout.lower()


def test_list_usernames_empty():
    """list-usernames with empty DB should exit 0 and report no usernames."""
    # db-reset first to ensure clean state
    _run("db-reset")
    result = _run("list-usernames")
    assert result.returncode == 0, result.stderr


def test_add_username_command():
    """add-username should add a username to the DB and exit 0."""
    _run("db-reset")
    result = _run("add-username", "testuser_bat_audit")
    assert result.returncode == 0, result.stderr
    assert "added" in result.stdout.lower() or "testuser_bat_audit" in result.stdout.lower()


def test_add_username_duplicate():
    """add-username with duplicate should exit 0 and report already exists."""
    _run("db-reset")
    _run("add-username", "testuser_dup")
    result = _run("add-username", "testuser_dup")
    assert result.returncode == 0, result.stderr
    assert "already" in result.stdout.lower()


def test_list_usernames_after_add():
    """list-usernames should show username added via add-username."""
    _run("db-reset")
    _run("add-username", "testuser_list_check")
    result = _run("list-usernames")
    assert result.returncode == 0, result.stderr
    assert "testuser_list_check" in result.stdout


def test_analyze_command_with_empty_db():
    """analyze should exit 0 even with no data in DB."""
    _run("db-reset")
    result = _run("analyze")
    assert result.returncode == 0, result.stderr
