"""
Property tests for BrokerManager consumer re-registration after reconnect.

Property 5: Consumer Completeness After Reconnect
**Validates: Requirements 3.2**

Property 6: Topology-Before-Consumers Invariant
**Validates: Requirements 3.1**
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services", "processor-py"))

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Mock broker that records calls
# ---------------------------------------------------------------------------

class MockBroker:
    """Minimal broker mock that records consume() and declare_topology() calls."""

    def __init__(self):
        self._is_connected = True
        self.call_log: list[str] = []  # ordered record of method names called
        self.consumed_queues: list[str] = []
        self._on_reconnect_callback = None

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    async def connect(self) -> None:
        self._is_connected = True

    async def declare_topology(self) -> None:
        self.call_log.append("declare_topology")

    async def set_qos(self, prefetch_count: int) -> None:
        pass

    async def consume(self, queue_name: str, handler) -> None:
        self.call_log.append(f"consume:{queue_name}")
        self.consumed_queues.append(queue_name)

    async def publish(self, routing_key: str, payload: dict) -> None:
        pass

    async def get_queue_depth(self, queue_name: str) -> int:
        return 0

    async def close(self) -> None:
        self._is_connected = False

    def simulate_reconnect(self) -> None:
        """Reset call log to simulate a fresh reconnect cycle."""
        self.call_log.clear()
        self.consumed_queues.clear()


# ---------------------------------------------------------------------------
# BrokerManager factory using mock broker
# ---------------------------------------------------------------------------

def make_broker_manager_with_mock(queue_names: list[str]) -> tuple:
    """
    Build a BrokerManager backed by a MockBroker, pre-register consumers
    for each queue_name, and return (manager, mock_broker).
    """
    # Import here so sys.path manipulation above takes effect
    from processing_queue import BrokerManager

    manager = BrokerManager.__new__(BrokerManager)
    manager.broker_type = "redis"
    mock = MockBroker()
    manager._broker = mock
    manager._consumer_registry = []
    return manager, mock


async def _register_consumers(manager, queue_names: list[str]) -> None:
    """Register a no-op handler for each queue name via BrokerManager.consume()."""
    async def noop_handler(msg):
        pass

    for q in queue_names:
        await manager.consume(q, noop_handler)


# ---------------------------------------------------------------------------
# Property 5: Consumer Completeness After Reconnect
# ---------------------------------------------------------------------------

@given(
    queue_names=st.lists(
        st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="._-")),
        min_size=1,
        max_size=5,
        unique=True,
    )
)
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=5000,
)
def test_consumer_completeness_after_reconnect(queue_names: list[str]) -> None:
    """
    **Validates: Requirements 3.2**

    Property 5: FOR ANY sequence of connect, disconnect, and reconnect events,
    the set of active consumers after the final reconnect SHALL equal the set
    of consumers registered during the initial start() call.
    """

    async def _inner() -> None:
        manager, mock = make_broker_manager_with_mock(queue_names)

        # Initial registration — simulates startup
        await _register_consumers(manager, queue_names)
        initial_registered = set(queue_names)

        # Simulate reconnect: reset the mock's tracking, then call _reregister_consumers
        mock.simulate_reconnect()
        await manager._reregister_consumers()

        # After reconnect, all originally registered queues must be re-consumed
        reregistered = set(mock.consumed_queues)
        assert reregistered == initial_registered, (
            f"After reconnect, consumed queues {reregistered} != "
            f"originally registered queues {initial_registered}"
        )

    asyncio.run(_inner())


@given(
    queue_names=st.lists(
        st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="._-")),
        min_size=1,
        max_size=5,
        unique=True,
    ),
    reconnect_count=st.integers(min_value=1, max_value=3),
)
@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=10000,
)
def test_consumer_completeness_after_multiple_reconnects(
    queue_names: list[str], reconnect_count: int
) -> None:
    """
    **Validates: Requirements 3.2**

    Extension of Property 5: completeness holds across multiple reconnect cycles.
    """

    async def _inner() -> None:
        manager, mock = make_broker_manager_with_mock(queue_names)
        await _register_consumers(manager, queue_names)
        initial_registered = set(queue_names)

        for _ in range(reconnect_count):
            mock.simulate_reconnect()
            await manager._reregister_consumers()

            reregistered = set(mock.consumed_queues)
            assert reregistered == initial_registered, (
                f"After reconnect, consumed queues {reregistered} != "
                f"originally registered queues {initial_registered}"
            )

    asyncio.run(_inner())


# ---------------------------------------------------------------------------
# Property 6: Topology-Before-Consumers Invariant
# ---------------------------------------------------------------------------

@given(
    queue_names=st.lists(
        st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="._-")),
        min_size=1,
        max_size=5,
        unique=True,
    )
)
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=5000,
)
def test_topology_declared_before_consumers(queue_names: list[str]) -> None:
    """
    **Validates: Requirements 3.1**

    Property 6: FOR ALL reconnect events, declare_topology() SHALL be called
    before any consume() call in the same reconnect cycle.
    """

    async def _inner() -> None:
        manager, mock = make_broker_manager_with_mock(queue_names)
        await _register_consumers(manager, queue_names)

        # Simulate reconnect
        mock.simulate_reconnect()
        await manager._reregister_consumers()

        # Verify declare_topology appears in the call log
        assert "declare_topology" in mock.call_log, (
            "declare_topology() was never called during reconnect"
        )

        # Find the index of declare_topology
        topo_index = mock.call_log.index("declare_topology")

        # All consume calls must appear AFTER declare_topology
        consume_indices = [
            i for i, entry in enumerate(mock.call_log)
            if entry.startswith("consume:")
        ]

        assert len(consume_indices) == len(queue_names), (
            f"Expected {len(queue_names)} consume calls, got {len(consume_indices)}"
        )

        for idx in consume_indices:
            assert idx > topo_index, (
                f"consume() at position {idx} was called before "
                f"declare_topology() at position {topo_index}. "
                f"Full call log: {mock.call_log}"
            )

    asyncio.run(_inner())


@given(
    queue_names=st.lists(
        st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="._-")),
        min_size=1,
        max_size=5,
        unique=True,
    ),
    reconnect_count=st.integers(min_value=1, max_value=3),
)
@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=10000,
)
def test_topology_before_consumers_across_multiple_reconnects(
    queue_names: list[str], reconnect_count: int
) -> None:
    """
    **Validates: Requirements 3.1**

    Extension of Property 6: topology-before-consumers invariant holds
    across multiple reconnect cycles.
    """

    async def _inner() -> None:
        manager, mock = make_broker_manager_with_mock(queue_names)
        await _register_consumers(manager, queue_names)

        for cycle in range(reconnect_count):
            mock.simulate_reconnect()
            await manager._reregister_consumers()

            topo_index = mock.call_log.index("declare_topology")
            consume_indices = [
                i for i, entry in enumerate(mock.call_log)
                if entry.startswith("consume:")
            ]

            for idx in consume_indices:
                assert idx > topo_index, (
                    f"Reconnect cycle {cycle}: consume() at position {idx} was called "
                    f"before declare_topology() at position {topo_index}. "
                    f"Full call log: {mock.call_log}"
                )

    asyncio.run(_inner())
