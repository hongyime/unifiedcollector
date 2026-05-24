# Feature: bulk-sender-service, Property 3: Hash Determinism
"""
Property test for Sender._compute_hash determinism.

Validates: Requirements 4.1
"""

import re
import tempfile

from hypothesis import given, settings
import hypothesis.strategies as st

from services.bulk_sender.sender import Sender


@given(st.binary())
@settings(max_examples=100)
def test_hash_determinism(file_bytes: bytes) -> None:
    """For any byte sequence, _compute_hash returns the same result on repeated calls.

    **Validates: Requirements 4.1**
    """
    sender = Sender.__new__(Sender)

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    hash1 = sender._compute_hash(tmp_path)
    hash2 = sender._compute_hash(tmp_path)

    # Both calls must agree
    assert hash1 == hash2

    # Must be exactly 64 characters
    assert len(hash1) == 64

    # Must be lowercase hex
    assert re.fullmatch(r"[0-9a-f]{64}", hash1) is not None
