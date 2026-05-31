"""Unit tests for github pure parse helpers (STAGE 2 safety net)."""
from src.collectors.github.parse import get_pat_display, validate_pat_format


def test_get_pat_display_masks():
    assert get_pat_display("ghp_abcdefgh12345678") == "ghp_****...****5678"


def test_get_pat_display_short():
    assert get_pat_display("x") == "****"
    assert get_pat_display("") == "****"


def test_validate_pat_format_valid():
    assert validate_pat_format("ghp_" + "a" * 30)
    assert validate_pat_format("gho_" + "b" * 30)


def test_validate_pat_format_invalid():
    assert not validate_pat_format("short")
    assert not validate_pat_format("")
    assert not validate_pat_format("xxx_" + "a" * 30)
