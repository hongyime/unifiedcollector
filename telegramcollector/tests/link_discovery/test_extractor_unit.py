"""
Unit tests for services/link_discovery/extractor.py

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.4
"""
import pytest
from services.link_discovery.extractor import Extractor, ExtractedLink


@pytest.fixture
def extractor():
    return Extractor()


# ---------------------------------------------------------------------------
# extract_links — empty / None input
# ---------------------------------------------------------------------------

def test_extract_links_empty_string_returns_empty(extractor):
    assert extractor.extract_links('') == []


def test_extract_links_none_returns_empty(extractor):
    assert extractor.extract_links(None) == []


# ---------------------------------------------------------------------------
# Invite links
# ---------------------------------------------------------------------------

def test_invite_link_has_group_type(extractor):
    links = extractor.extract_links('Join us: t.me/+AbCdEf')
    assert len(links) == 1
    assert links[0].link == 't.me/+abcdef'
    assert links[0].link_type == 'group'
    assert links[0].is_bot_link is False


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def test_telegram_me_normalised_to_t_me(extractor):
    links = extractor.extract_links('telegram.me/group')
    assert len(links) == 1
    assert links[0].link == 't.me/group'


def test_mixed_case_normalised_to_lowercase(extractor):
    links = extractor.extract_links('T.ME/MyGroup')
    assert len(links) == 1
    assert links[0].link == 't.me/mygroup'


def test_trailing_slash_stripped(extractor):
    links = extractor.extract_links('t.me/group/')
    assert len(links) == 1
    assert links[0].link == 't.me/group'


# ---------------------------------------------------------------------------
# Multiple links / deduplication
# ---------------------------------------------------------------------------

def test_multiple_links_all_returned(extractor):
    text = 't.me/groupA and t.me/groupB'
    links = extractor.extract_links(text)
    link_strs = [l.link for l in links]
    assert 't.me/groupa' in link_strs
    assert 't.me/groupb' in link_strs
    assert len(links) == 2


def test_duplicate_links_deduplicated(extractor):
    text = 't.me/mygroup and t.me/mygroup and T.ME/MyGroup'
    links = extractor.extract_links(text)
    assert len(links) == 1
    assert links[0].link == 't.me/mygroup'


# ---------------------------------------------------------------------------
# Bot detection
# ---------------------------------------------------------------------------

def test_bot_link_flagged(extractor):
    links = extractor.extract_links('t.me/mybot')
    assert len(links) == 1
    assert links[0].is_bot_link is True


def test_non_bot_link_not_flagged(extractor):
    links = extractor.extract_links('t.me/mygroup')
    assert len(links) == 1
    assert links[0].is_bot_link is False


# ---------------------------------------------------------------------------
# raw_message_id default
# ---------------------------------------------------------------------------

def test_raw_message_id_defaults_to_zero(extractor):
    links = extractor.extract_links('t.me/somegroup')
    assert links[0].raw_message_id == 0
