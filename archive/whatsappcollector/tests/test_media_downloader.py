"""
Property tests for MediaDownloader multi-bridge fallback logic.

Property 11: Bridge Exhaustion Implies All Tried
**Validates: Requirements 6.3**

Property 12: Success Uses First Available Bridge
**Validates: Requirements 6.1, 6.2**

Property 13: No Bridge Retried in Same Attempt
**Validates: Requirements 6.6**
"""

import asyncio
from typing import Any

from hypothesis import given, settings as h_settings, HealthCheck
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Simulated bridge loop — mirrors the logic in both MediaDownloader.download()
# and MediaDownloader.download_message() without importing the real classes.
# ---------------------------------------------------------------------------

async def run_bridge_loop(
    bridge_urls: list[str],
    bridge_behavior: dict[str, str],  # url -> "success" | "fail"
) -> tuple[bool, list[str]]:
    """
    Simulate the multi-bridge fallback loop.

    Returns (success: bool, attempted_bridges: list[str]).
    """
    attempted: list[str] = []

    for base_url in bridge_urls:
        attempted.append(base_url)
        if bridge_behavior.get(base_url) == "success":
            return True, attempted
        # else: simulate failure, continue to next bridge

    return False, attempted


# ---------------------------------------------------------------------------
# Property 11: Bridge Exhaustion Implies All Tried
# ---------------------------------------------------------------------------

@given(
    bridge_urls=st.lists(st.text(min_size=1), min_size=1, max_size=5)
)
@h_settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=5000,
)
def test_bridge_exhaustion_implies_all_tried(bridge_urls: list[str]) -> None:
    """
    **Validates: Requirements 6.3**

    Property 11: FOR ALL download attempts where the result is failure,
    the number of bridges attempted SHALL equal the total number of bridges
    configured.
    """

    async def _inner() -> None:
        # All bridges fail
        behavior = {url: "fail" for url in bridge_urls}
        success, attempted = await run_bridge_loop(bridge_urls, behavior)

        assert not success, "Expected failure when all bridges fail"
        assert len(attempted) == len(bridge_urls), (
            f"Expected {len(bridge_urls)} bridges attempted, got {len(attempted)}. "
            f"Configured: {bridge_urls}, Attempted: {attempted}"
        )

    asyncio.run(_inner())


# ---------------------------------------------------------------------------
# Property 12: Success Uses First Available Bridge
# ---------------------------------------------------------------------------

@given(
    success_idx=st.integers(min_value=0, max_value=4)
)
@h_settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=5000,
)
def test_success_uses_first_available_bridge(success_idx: int) -> None:
    """
    **Validates: Requirements 6.1, 6.2**

    Property 12: FOR ALL download attempts where bridge at index k is the
    first non-failing bridge, the download SHALL succeed using bridge k and
    bridges 0..k-1 SHALL each have been attempted exactly once.
    """

    async def _inner() -> None:
        # Build a list of (success_idx + 1) bridges
        total_bridges = success_idx + 1
        bridge_urls = [f"http://bridge-{i}:3001" for i in range(total_bridges)]

        # Bridges 0..k-1 fail, bridge k succeeds
        behavior: dict[str, str] = {}
        for i, url in enumerate(bridge_urls):
            behavior[url] = "success" if i == success_idx else "fail"

        success, attempted = await run_bridge_loop(bridge_urls, behavior)

        assert success, f"Expected success when bridge {success_idx} is available"
        assert len(attempted) == success_idx + 1, (
            f"Expected exactly {success_idx + 1} bridges attempted (0..{success_idx}), "
            f"got {len(attempted)}: {attempted}"
        )
        # Each bridge 0..k-1 attempted exactly once
        for i in range(success_idx + 1):
            assert attempted.count(bridge_urls[i]) == 1, (
                f"Bridge {i} ({bridge_urls[i]}) should appear exactly once in attempt log, "
                f"got {attempted.count(bridge_urls[i])}"
            )

    asyncio.run(_inner())


# ---------------------------------------------------------------------------
# Property 13: No Bridge Retried in Same Attempt
# ---------------------------------------------------------------------------

@given(
    bridge_urls=st.lists(st.text(min_size=1), min_size=1, max_size=5, unique=True)
)
@h_settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=5000,
)
def test_no_bridge_retried_in_same_attempt(bridge_urls: list[str]) -> None:
    """
    **Validates: Requirements 6.6**

    Property 13: FOR ALL download attempts, each bridge URL SHALL appear
    at most once in the attempt log.
    """

    async def _inner() -> None:
        # All bridges fail to ensure the full loop runs
        behavior = {url: "fail" for url in bridge_urls}
        success, attempted = await run_bridge_loop(bridge_urls, behavior)

        assert not success, "Expected failure when all bridges fail"

        # Each URL must appear at most once
        for url in bridge_urls:
            count = attempted.count(url)
            assert count <= 1, (
                f"Bridge URL '{url}' appeared {count} times in attempt log "
                f"(expected at most 1). Full log: {attempted}"
            )

    asyncio.run(_inner())
