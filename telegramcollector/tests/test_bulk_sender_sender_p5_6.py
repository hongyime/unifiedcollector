# Feature: bulk-sender-service, Property 8: File List Extension Filter
"""
Property test for Sender._get_file_list extension filtering.

Validates: Requirements 6.1, 6.2
"""

import os
import tempfile

from hypothesis import given, settings
import hypothesis.strategies as st

from services.bulk_sender.sender import Sender, VALID_EXTENSIONS

valid_exts = ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif', '.JPG', '.PNG']
invalid_exts = ['.txt', '.pdf', '.mp4', '.doc', '.zip', '.py', '.csv']
all_exts = valid_exts + invalid_exts


@given(
    st.lists(
        st.tuples(
            st.text(
                alphabet=st.characters(whitelist_categories=('Ll', 'Lu', 'Nd')),
                min_size=1,
                max_size=10,
            ),
            st.sampled_from(all_exts),
        ),
        min_size=0,
        max_size=20,
    )
)
@settings(max_examples=100)
def test_file_list_extension_filter(filenames: list) -> None:
    """_get_file_list returns only files with valid image extensions (case-insensitive).

    **Validates: Requirements 6.1, 6.2**
    """
    sender = Sender.__new__(Sender)

    tmp_dir = tempfile.mkdtemp()
    try:
        created: list[tuple[str, str]] = []
        seen_names: set[str] = set()

        for stem, ext in filenames:
            name = stem + ext
            # Avoid duplicate filenames in the same directory
            if name in seen_names:
                continue
            seen_names.add(name)
            path = os.path.join(tmp_dir, name)
            open(path, 'w').close()
            created.append((name, ext))

        result = sender._get_file_list(tmp_dir)

        # Build expected set: only files whose extension is in VALID_EXTENSIONS
        expected = {
            os.path.realpath(os.path.join(tmp_dir, name))
            for name, ext in created
            if ext.lower() in VALID_EXTENSIONS
        }

        assert set(result) == expected, (
            f"Expected {sorted(expected)}, got {sorted(result)}"
        )

        # Assert no invalid-extension files leaked through
        for path in result:
            _, suffix = os.path.splitext(path)
            assert suffix.lower() in VALID_EXTENSIONS, (
                f"File with invalid extension returned: {path}"
            )

        # Assert all valid-extension files are present
        for name, ext in created:
            if ext.lower() in VALID_EXTENSIONS:
                full = os.path.realpath(os.path.join(tmp_dir, name))
                assert full in result, f"Expected valid file missing from result: {full}"

    finally:
        # Clean up temp files
        for entry in os.listdir(tmp_dir):
            os.remove(os.path.join(tmp_dir, entry))
        os.rmdir(tmp_dir)
