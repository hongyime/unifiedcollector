"""
Property-based tests for services/link_discovery/extractor.py

Feature: link-discovery-service

Properties tested:
  1. Link normalisation idempotence          — Validates: Requirements 2.3
  2. Bot detection completeness              — Validates: Requirements 3.1
  3. Regex coverage — patterns extracted     — Validates: Requirements 2.2, 2.4
  3. Regex coverage — non-link text empty    — Validates: Requirements 2.4
"""
import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from services.link_discovery.extractor import Extractor


# ---------------------------------------------------------------------------
# Property 1: Link normalisation idempotence
# Validates: Requirements 2.3
# ---------------------------------------------------------------------------

@given(st.text())
@settings(max_examples=200)
def test_normalise_idempotent(raw):
    extractor = Extractor()
    once = extractor._normalise_link(raw)
    twice = extractor._normalise_link(once)
    assert once == twice


# ---------------------------------------------------------------------------
# Property 2: Bot detection completeness
# Validates: Requirements 3.1
# ---------------------------------------------------------------------------

@given(
    prefix=st.text(alphabet=st.characters(blacklist_categories=('Cs',))),
    suffix=st.text(alphabet=st.characters(blacklist_categories=('Cs',))),
    bot_variant=st.sampled_from(['bot', 'Bot', 'BOT', 'bOt', 'BoT']),
)
@settings(max_examples=200)
def test_bot_detection_completeness(prefix, suffix, bot_variant):
    username = prefix + bot_variant + suffix
    extractor = Extractor()
    assert extractor._is_bot_link(username) is True


# ---------------------------------------------------------------------------
# Property 3: Regex coverage — patterns extracted
# Validates: Requirements 2.2, 2.4
# ---------------------------------------------------------------------------

@given(
    username=st.from_regex(r'[A-Za-z0-9_]{3,32}', fullmatch=True),
    surrounding=st.text(max_size=50),
)
@settings(max_examples=200)
def test_public_link_extracted(username, surrounding):
    extractor = Extractor()
    text = f"{surrounding} t.me/{username} {surrounding}"
    links = extractor.extract_links(text)
    normalised = [l.link for l in links]
    assert f"t.me/{username.lower()}" in normalised


# ---------------------------------------------------------------------------
# Property 3: Regex coverage — non-link text returns empty list
# Validates: Requirements 2.4
# ---------------------------------------------------------------------------

@given(st.text().filter(lambda s: not re.search(r't\.me/|telegram\.me/', s, re.I)))
@settings(max_examples=200)
def test_no_links_in_plain_text(text):
    extractor = Extractor()
    assert extractor.extract_links(text) == []
