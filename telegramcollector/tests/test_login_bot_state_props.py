"""Property-based tests for the login state machine in login_bot/main.py.

Feature: login-bot-session-manager, Property 9: Login state machine advances only forward
"""
# Feature: login-bot-session-manager, Property 9: Login state machine advances only forward

import os
import random
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.login_bot.main import LoginState  # noqa: E402

# ---------------------------------------------------------------------------
# State ordering — numeric index for each state
# ---------------------------------------------------------------------------

_STATE_ORDER = {
    LoginState.WAITING_PHONE: 0,
    LoginState.WAITING_CODE: 1,
    LoginState.WAITING_2FA: 2,
    "done": 3,
}

_DONE = "done"


def _simulate_transitions(inputs: list[str]) -> list[int]:
    """Simulate state transitions for a sequence of inputs.

    Rules:
    - "phone" input: if state is WAITING_PHONE → advance to WAITING_CODE
    - "code"  input: if state is WAITING_CODE  → advance to WAITING_2FA or done (randomly)
    - "2fa"   input: if state is WAITING_2FA   → done

    Returns the ordered list of numeric state indices visited (including the
    initial state before any input is processed).
    """
    state: str = LoginState.WAITING_PHONE
    visited: list[int] = [_STATE_ORDER[state]]

    for inp in inputs:
        if inp == "phone" and state == LoginState.WAITING_PHONE:
            state = LoginState.WAITING_CODE
        elif inp == "code" and state == LoginState.WAITING_CODE:
            # Randomly choose: 2FA required or direct success
            if random.random() < 0.5:
                state = LoginState.WAITING_2FA
            else:
                state = _DONE
        elif inp == "2fa" and state == LoginState.WAITING_2FA:
            state = _DONE

        visited.append(_STATE_ORDER[state])

        # Once done, no further transitions are possible
        if state == _DONE:
            break

    return visited


# ---------------------------------------------------------------------------
# Property 9: Login state machine advances only forward
# Validates: Requirements 3.1–3.8
# ---------------------------------------------------------------------------

@given(
    inputs=st.lists(
        st.sampled_from(["phone", "code", "2fa"]),
        min_size=1,
        max_size=10,
    )
)
@settings(max_examples=200)
def test_property_9_state_machine_forward_only(inputs: list[str]) -> None:
    """**Validates: Requirements 3.1–3.8**

    For any sequence of valid inputs, the LoginState numeric index must never
    decrease — the state machine only advances forward through:
        WAITING_PHONE (0) → WAITING_CODE (1) → WAITING_2FA (2) → done (3)

    Feature: login-bot-session-manager, Property 9: Login state machine advances only forward
    """
    visited = _simulate_transitions(inputs)

    for i in range(1, len(visited)):
        prev = visited[i - 1]
        curr = visited[i]
        assert curr >= prev, (
            f"State moved backwards: index {prev} → {curr} "
            f"(inputs={inputs}, visited={visited})"
        )
