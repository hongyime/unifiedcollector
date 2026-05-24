"""
Bug condition exploration tests for BUG-3 and BUG-4.

These tests assert the DESIRED (fixed) behavior — that the README contains
the required sections. Running on UNFIXED README: tests FAIL → confirms bugs
exist. Running on FIXED README: tests PASS → confirms bugs are fixed.

Validates: Requirements 3.1, 3.2, 4.1
"""

import os

README_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "README.md")


def _read_readme() -> str:
    with open(README_PATH, "r", encoding="utf-8") as f:
        return f.read()


def test_bug_condition_b3_storage_requirements_present():
    """BUG-3: README must contain a Storage Requirements section.

    FAILS on unfixed README (confirms bug). PASSES after fix.
    isBugCondition_B3 holds when this section is absent.
    """
    content = _read_readme()
    assert "## Storage Requirements" in content, (
        "BUG-3 CONFIRMED: README does not contain '## Storage Requirements' section. "
        "This section must be added before Quick Start."
    )


def test_bug_condition_b4_first_run_checklist_present():
    """BUG-4: README must contain a First Run Checklist section.

    FAILS on unfixed README (confirms bug). PASSES after fix.
    isBugCondition_B4 holds when this section is absent.
    """
    content = _read_readme()
    assert "## First Run Checklist" in content, (
        "BUG-4 CONFIRMED: README does not contain '## First Run Checklist' section. "
        "This section must be added before Quick Start."
    )


# ---------------------------------------------------------------------------
# Preservation tests — these should PASS on both unfixed and fixed README
# ---------------------------------------------------------------------------

EXPECTED_EXISTING_SECTIONS = [
    "## Prerequisites",
    "## Quick Start",
    "## Runtime Ports",
    "## Environment Variables",
]


def test_preservation_existing_sections_present():
    """All existing README sections must remain after fix."""
    content = _read_readme()
    for section in EXPECTED_EXISTING_SECTIONS:
        assert section in content, f"Preservation FAILED: '{section}' was removed from README"


def test_preservation_quick_start_unchanged():
    """## Quick Start section must still be present."""
    content = _read_readme()
    assert "## Quick Start" in content


# Property-based test using hypothesis
from hypothesis import given, settings
import hypothesis.strategies as st


def _apply_fix_if_missing(readme_content: str) -> str:
    """Simulate the fix: add sections only if absent."""
    result = readme_content
    if "## Storage Requirements" not in result:
        # Would insert before ## Prerequisites
        result = result.replace(
            "## Prerequisites",
            "## Storage Requirements\n\nPlaceholder.\n\n## Prerequisites",
            1,
        )
    if "## First Run Checklist" not in result:
        result = result.replace(
            "## Prerequisites",
            "## First Run Checklist\n\nPlaceholder.\n\n## Prerequisites",
            1,
        )
    return result


@given(st.text(min_size=10))
@settings(max_examples=50)
def test_readme_idempotent(extra_content):
    """Fix logic must not insert duplicate sections when they already exist.

    **Validates: Requirements 3.6**
    """
    # Build a README that already has both sections
    base = (
        "# Title\n\n"
        "## Storage Requirements\n\nSome content.\n\n"
        "## First Run Checklist\n\nSome checklist.\n\n"
        "## Prerequisites\n\nSome prereqs.\n\n"
        "## Quick Start\n\nSome steps.\n\n"
        + extra_content
    )
    result = _apply_fix_if_missing(base)
    assert result.count("## Storage Requirements") == 1, (
        "Duplicate Storage Requirements section inserted!"
    )
    assert result.count("## First Run Checklist") == 1, (
        "Duplicate First Run Checklist section inserted!"
    )


# ---------------------------------------------------------------------------
# Unit tests for BUG-3 & BUG-4 — section presence, position, and content
# ---------------------------------------------------------------------------

def test_storage_requirements_section_present():
    """## Storage Requirements section must be present in README."""
    content = _read_readme()
    assert "## Storage Requirements" in content


def test_storage_requirements_precedes_quick_start():
    """## Storage Requirements must appear before ## Quick Start."""
    content = _read_readme()
    assert "## Storage Requirements" in content
    assert "## Quick Start" in content
    sr_idx = content.index("## Storage Requirements")
    qs_idx = content.index("## Quick Start")
    assert sr_idx < qs_idx, (
        f"Storage Requirements (pos {sr_idx}) must precede Quick Start (pos {qs_idx})"
    )


def test_storage_requirements_has_windows_example():
    """Storage Requirements section must include a Windows path example."""
    content = _read_readme()
    sr_start = content.index("## Storage Requirements")
    next_section = content.find("\n## ", sr_start + 1)
    section_content = content[sr_start:next_section] if next_section != -1 else content[sr_start:]
    assert "D:\\" in section_content or "D:/" in section_content or "EXTERNAL_STORAGE_ROOT=D:" in section_content, (
        "Storage Requirements section must include a Windows path example (e.g., D:\\whatsapp_data)"
    )


def test_storage_requirements_has_linux_example():
    """Storage Requirements section must include a Linux/macOS path example."""
    content = _read_readme()
    sr_start = content.index("## Storage Requirements")
    next_section = content.find("\n## ", sr_start + 1)
    section_content = content[sr_start:next_section] if next_section != -1 else content[sr_start:]
    assert "/mnt/" in section_content, (
        "Storage Requirements section must include a Linux/macOS path example (e.g., /mnt/external/...)"
    )


def test_first_run_checklist_section_present():
    """## First Run Checklist section must be present in README."""
    content = _read_readme()
    assert "## First Run Checklist" in content


def test_first_run_checklist_precedes_quick_start():
    """## First Run Checklist must appear before ## Quick Start."""
    content = _read_readme()
    assert "## First Run Checklist" in content
    assert "## Quick Start" in content
    frc_idx = content.index("## First Run Checklist")
    qs_idx = content.index("## Quick Start")
    assert frc_idx < qs_idx, (
        f"First Run Checklist (pos {frc_idx}) must precede Quick Start (pos {qs_idx})"
    )


def test_first_run_checklist_covers_required_topics():
    """First Run Checklist must cover all required topics."""
    content = _read_readme()
    frc_start = content.index("## First Run Checklist")
    next_section = content.find("\n## ", frc_start + 1)
    section_content = content[frc_start:next_section] if next_section != -1 else content[frc_start:]

    required_topics = [
        "EXTERNAL_STORAGE_ROOT",
        "secrets",
        "FINDINGS_HUB_GROUP_NAME",
        "QR",
        "health",
        "degraded",
    ]
    for topic in required_topics:
        assert topic.lower() in section_content.lower(), (
            f"First Run Checklist must cover topic: '{topic}'"
        )
