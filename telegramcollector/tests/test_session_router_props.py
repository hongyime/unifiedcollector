"""Property-based tests for SessionRouter (task 1.3, 1.4).

Tests Properties 5 and 8 from the design document.
All tests use hypothesis with @given and @settings(max_examples=100).
"""

# Feature: login-bot-session-manager, Property 5: SessionRouter distributes to all non-source directories

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.login_bot.session_router import SessionRouter  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Characters that are valid as directory names on all platforms
_SAFE_CHARS = st.characters(
    whitelist_categories=("Ll", "Lu", "Nd"),
    whitelist_characters="_-",
)

_SAFE_TEXT = st.text(_SAFE_CHARS, min_size=1, max_size=20).filter(
    # Filter out Windows reserved device names (NUL, CON, PRN, AUX, COM1-9, LPT1-9)
    lambda s: s.upper() not in {
        "NUL", "CON", "PRN", "AUX",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }
)


# ---------------------------------------------------------------------------
# Property 5: SessionRouter distributes to all non-source directories
# Validates: Requirements 8.1, 8.2
#
# For any set of subdirectory names that includes "collector" plus at least
# one other directory, distribute(stem) must:
#   - return exactly N-1 directory names (all except "collector")
#   - never include "collector" in the returned list
# ---------------------------------------------------------------------------

@given(
    extra_dirs=st.sets(_SAFE_TEXT, min_size=1, max_size=8).filter(
        lambda s: "collector" not in s
    ),
    stem=_SAFE_TEXT,
)
@h_settings(max_examples=100)
def test_property_5_distributes_to_all_non_source_dirs(
    extra_dirs: set[str],
    stem: str,
) -> None:
    """**Validates: Requirements 8.1, 8.2**

    SessionRouter.distribute(stem) copies the session file to every immediate
    subdirectory of base_path except the source directory ("collector").
    It returns exactly the names of the directories that received a copy,
    which must be all non-source dirs and must never include "collector".
    """
    with tempfile.TemporaryDirectory() as base_path:
        # Build the full set of subdirs: collector + all extra dirs
        all_subdirs = {"collector"} | extra_dirs

        # Create each subdirectory under base_path
        for subdir in all_subdirs:
            os.makedirs(os.path.join(base_path, subdir), exist_ok=True)

        # Write a non-zero-byte fake session file in the source dir
        session_filename = f"{stem}.session"
        src_path = os.path.join(base_path, "collector", session_filename)
        with open(src_path, "wb") as f:
            f.write(b"fake session data - non zero bytes")

        # Run distribute via asyncio.run (distribute is async)
        router = SessionRouter(base_path=base_path, source_subdir="collector")
        result = asyncio.run(router.distribute(stem))

        # The result must contain exactly the non-source directories
        expected = extra_dirs  # all dirs except "collector"
        assert set(result) == expected, (
            f"Expected distribute to return {expected!r}, got {set(result)!r}. "
            f"all_subdirs={all_subdirs!r}, stem={stem!r}"
        )

        # "collector" must never appear in the result
        assert "collector" not in result, (
            f'"collector" (source dir) must not appear in distribute result, '
            f"but got result={result!r}"
        )

        # The count must be exactly N-1
        assert len(result) == len(all_subdirs) - 1, (
            f"Expected {len(all_subdirs) - 1} entries in result "
            f"(N-1 = {len(all_subdirs)}-1), got {len(result)}: {result!r}"
        )


# ---------------------------------------------------------------------------
# Feature: login-bot-session-manager, Property 8: Session copy size invariant
# Validates: Requirements 12.1, 12.3
#
# For any non-empty byte content written to a source session file,
# _copy_session must return True and the destination file size must equal
# the length of the original content.
# ---------------------------------------------------------------------------

@given(content=st.binary(min_size=1))
@h_settings(max_examples=100)
def test_property_8_session_copy_size_invariant(content: bytes) -> None:
    """**Validates: Requirements 12.1, 12.3**

    _copy_session(src, dst_dir) must return True and produce a destination
    file whose byte size equals the source file's byte size for any non-empty
    content.
    """
    with tempfile.TemporaryDirectory() as src_dir_str, \
         tempfile.TemporaryDirectory() as dst_dir_str:
        src_dir = Path(src_dir_str)
        dst_dir = Path(dst_dir_str)

        # Write the generated bytes to a source session file
        src = src_dir / "phone.session"
        src.write_bytes(content)

        # Call _copy_session directly (it is synchronous)
        router = SessionRouter(base_path=src_dir_str, source_subdir="collector")
        result = router._copy_session(src, dst_dir)

        # Must report success
        assert result is True, (
            f"_copy_session returned {result!r} for content of length {len(content)}"
        )

        # Destination file size must equal source content length
        dst = dst_dir / src.name
        assert dst.stat().st_size == len(content), (
            f"Expected dst size {len(content)}, got {dst.stat().st_size}"
        )
