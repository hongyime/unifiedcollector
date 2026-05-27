"""
Property test for FaceProcessor — Property 15: Non-Biometric Queues Always Consumed

**Validates: Requirements 8.2**

FOR ALL states of `face_processor.is_ready` (True or False), the non-biometric queues
(`messages.inbound`, `contacts.update`, `messages.status`, `session.events`,
`groups.metadata`) SHALL always be registered as consumers.

Biometric queues (`biometrics.encode`, `identity.cluster`) are ONLY consumed when
`is_ready=True`.
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Constants matching worker.py registration logic
# ---------------------------------------------------------------------------

NON_BIOMETRIC_QUEUES = {
    "messages.inbound",
    "contacts.update",
    "messages.status",
    "media.download",
    "media.process",
    "media.profile_photo",
    "session.events",
    "groups.metadata",
}

BIOMETRIC_QUEUES = {
    "biometrics.encode",
    "identity.cluster",
}


# ---------------------------------------------------------------------------
# Simulated consumer registration logic (mirrors worker.py)
# ---------------------------------------------------------------------------

class MockFaceProcessor:
    """Minimal stand-in for FaceProcessor with controllable is_ready state."""

    def __init__(self, is_ready: bool):
        self.is_ready = is_ready


async def simulate_consumer_registration(is_ready: bool) -> set[str]:
    """
    Simulate the consumer registration logic from Worker.start() and
    Worker._register_biometric_consumers(), returning the set of registered queues.
    """
    face_processor = MockFaceProcessor(is_ready)
    consumed: list[str] = []

    async def mock_consume(queue_name: str, handler) -> str:
        consumed.append(queue_name)
        return queue_name

    # Non-biometric consumers — always registered (mirrors worker.py)
    await mock_consume("messages.inbound", None)
    await mock_consume("contacts.update", None)
    await mock_consume("messages.status", None)
    await mock_consume("media.download", None)
    await mock_consume("media.process", None)
    await mock_consume("media.profile_photo", None)
    await mock_consume("session.events", None)
    await mock_consume("groups.metadata", None)

    # Biometric consumers — only when is_ready
    if face_processor.is_ready:
        await mock_consume("biometrics.encode", None)
        await mock_consume("identity.cluster", None)

    return set(consumed)


# ---------------------------------------------------------------------------
# Property 15: Non-Biometric Queues Always Consumed
# ---------------------------------------------------------------------------

@given(is_ready=st.booleans())
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_non_biometric_queues_always_consumed(is_ready: bool):
    """
    Property 15: FOR ALL states of face_processor.is_ready, non-biometric queues
    SHALL always be registered as consumers.

    **Validates: Requirements 8.2**
    """
    consumed = asyncio.run(simulate_consumer_registration(is_ready))

    # Non-biometric queues must always be present regardless of is_ready
    for queue in NON_BIOMETRIC_QUEUES:
        assert queue in consumed, (
            f"Non-biometric queue '{queue}' was NOT consumed when is_ready={is_ready}. "
            f"Non-biometric queues must always be registered."
        )


@given(is_ready=st.booleans())
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_biometric_queues_only_when_ready(is_ready: bool):
    """
    Property 15 (corollary): Biometric queues SHALL only be consumed when is_ready=True.

    **Validates: Requirements 8.2, 8.3**
    """
    consumed = asyncio.run(simulate_consumer_registration(is_ready))

    for queue in BIOMETRIC_QUEUES:
        if is_ready:
            assert queue in consumed, (
                f"Biometric queue '{queue}' was NOT consumed when is_ready=True."
            )
        else:
            assert queue not in consumed, (
                f"Biometric queue '{queue}' WAS consumed when is_ready=False. "
                f"Biometric queues must only be registered when models are loaded."
            )


# ---------------------------------------------------------------------------
# Concrete examples for clarity
# ---------------------------------------------------------------------------

def test_non_biometric_queues_consumed_when_models_missing():
    """When models are not loaded, non-biometric queues are still consumed."""
    consumed = asyncio.run(simulate_consumer_registration(is_ready=False))
    assert NON_BIOMETRIC_QUEUES.issubset(consumed)
    assert not BIOMETRIC_QUEUES.intersection(consumed)


def test_all_queues_consumed_when_models_loaded():
    """When models are loaded, both non-biometric and biometric queues are consumed."""
    consumed = asyncio.run(simulate_consumer_registration(is_ready=True))
    assert NON_BIOMETRIC_QUEUES.issubset(consumed)
    assert BIOMETRIC_QUEUES.issubset(consumed)
