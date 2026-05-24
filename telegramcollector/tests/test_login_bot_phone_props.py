"""Property-based tests for phone helper functions in login_bot/main.py.

Feature: login-bot-session-manager
Property 2: Phone sanitisation round-trip
Property 3: Session stem contains only digits
"""
import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.login_bot.main import sanitise_phone, session_stem_from_phone  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers / generators
# ---------------------------------------------------------------------------

# Characters that sanitise_phone must strip
_NOISE_CHARS = " -()".replace("", "")  # space, dash, open-paren, close-paren
_NOISE = st.sampled_from(list(" -()"))

# A strategy that builds a phone string with a leading '+', some digits, and
# random noise characters (spaces, dashes, brackets) interspersed.
_digit = st.text(alphabet="0123456789", min_size=1, max_size=3)
_noise = st.lists(_NOISE, min_size=0, max_size=3)


def _interleave(parts):
    """Join a list of strings into one."""
    return "".join(parts)


@st.composite
def noisy_phone(draw):
    """Generate a phone string: '+' followed by digit groups separated by noise."""
    # At least 6 digits after the '+' so the result is >= 7 chars after sanitise
    core_digits = draw(st.text(alphabet="0123456789", min_size=6, max_size=15))
    # Randomly insert noise characters between digit groups
    result = ["+"]
    for ch in core_digits:
        result.append(ch)
        if draw(st.booleans()):
            result.append(draw(_NOISE))
    return "".join(result)


@st.composite
def valid_e164_phone(draw):
    """Generate a clean E.164 phone: '+' followed by 6–15 digits."""
    digits = draw(st.text(alphabet="0123456789", min_size=6, max_size=15))
    return "+" + digits


# ---------------------------------------------------------------------------
# Property 2: Phone sanitisation round-trip
# Validates: Requirements 3.2
# ---------------------------------------------------------------------------

@given(phone=noisy_phone())
@settings(max_examples=500)
def test_property_2_phone_sanitisation_round_trip(phone: str) -> None:
    """**Validates: Requirements 3.2**

    For any phone string that starts with '+' and contains only digits, spaces,
    dashes, and brackets, sanitise_phone must produce a string that starts with
    '+' and whose remaining characters are all ASCII digits.
    """
    # Feature: login-bot-session-manager, Property 2: Phone sanitisation round-trip
    result = sanitise_phone(phone)
    assert result.startswith("+"), (
        f"sanitise_phone({phone!r}) = {result!r} does not start with '+'"
    )
    digits_part = result[1:]
    assert digits_part.isdigit(), (
        f"sanitise_phone({phone!r}) = {result!r}: characters after '+' are not all digits"
    )


# ---------------------------------------------------------------------------
# Property 3: Session stem contains only digits
# Validates: Requirements 7.2
# ---------------------------------------------------------------------------

@given(phone=valid_e164_phone())
@settings(max_examples=500)
def test_property_3_session_stem_digits_only(phone: str) -> None:
    """**Validates: Requirements 7.2**

    For any valid sanitised E.164 phone number, session_stem_from_phone must
    return a string composed entirely of ASCII digits — no '+', spaces, dashes,
    or any other character.
    """
    # Feature: login-bot-session-manager, Property 3: Session stem contains only digits
    stem = session_stem_from_phone(phone)
    assert stem.isdigit(), (
        f"session_stem_from_phone({phone!r}) = {stem!r} contains non-digit characters"
    )
    assert "+" not in stem, (
        f"session_stem_from_phone({phone!r}) = {stem!r} still contains '+'"
    )
