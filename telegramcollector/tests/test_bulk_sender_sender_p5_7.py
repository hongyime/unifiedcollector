# Feature: bulk-sender-service, Property 9: File List Ordering
"""
Property test for Sender._get_file_list lexicographic ordering.

Validates: Requirements 6.4
"""

import os
import tempfile

from hypothesis import given, settings
import hypothesis.strategies as st

from services.bulk_sender.sender import Sender


@given(
    st.lists(
        st.text(alphabet=st.characters(whitelist_categories=('Ll', 'Lu', 'Nd')), min_size=1, max_size=15),
        min_size=0,
        max_size=20,
        unique=True
    )
)
@settings(max_examples=100)
def test_file_list_ordering(stems: list) -> None:
    """_get_file_list returns files sorted in lexicographic order by full absolute path.

    **Validates: Requirements 6.4**
    """
    sender = Sender.__new__(Sender)

    tmp_dir = tempfile.mkdtemp()
    try:
        for stem in stems:
            filename = stem + ".jpg"
            path = os.path.join(tmp_dir, filename)
            open(path, 'w').close()

        result = sender._get_file_list(tmp_dir)

        # Assert the result is already sorted lexicographically
        assert result == sorted(result), (
            f"Result is not sorted. Got: {result}"
        )

        # Assert sorting the result again produces the same list (idempotent)
        assert sorted(result) == result, (
            f"Re-sorting changed the order. Got: {result}"
        )

    finally:
        for entry in os.listdir(tmp_dir):
            os.remove(os.path.join(tmp_dir, entry))
        os.rmdir(tmp_dir)
