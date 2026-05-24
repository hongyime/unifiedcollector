"""
Unit tests for services/link_discovery/queue_rules.py

Requirements: 7.1–7.12
"""
import pytest

from services.link_discovery.queue_rules import QueueDecision, QueueRules


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_rule(
    id=1,
    language_whitelist=None,
    language_blacklist=None,
    keyword_whitelist=None,
    keyword_blacklist=None,
    min_member_count=None,
    max_member_count=None,
    auto_queue=True,
):
    return {
        'id': id,
        'language_whitelist': language_whitelist or [],
        'language_blacklist': language_blacklist or [],
        'keyword_whitelist': keyword_whitelist or [],
        'keyword_blacklist': keyword_blacklist or [],
        'min_member_count': min_member_count,
        'max_member_count': max_member_count,
        'auto_queue': auto_queue,
    }


def make_qr():
    """Instantiate QueueRules without a DB pool for unit tests."""
    return QueueRules.__new__(QueueRules)


# ---------------------------------------------------------------------------
# _evaluate_rules tests
# ---------------------------------------------------------------------------

def test_empty_rule_list_returns_no_match():
    qr = make_qr()
    decision = qr._evaluate_rules([], {'language': 'en', 'chat_title': 'test', 'member_count': 100})
    assert decision == QueueDecision(should_queue=False, rule_id=None)


def test_auto_queue_false_returns_correct_decision():
    qr = make_qr()
    rule = make_rule(id=5, auto_queue=False)
    decision = qr._evaluate_rules([rule], {'language': 'en', 'chat_title': 'test', 'member_count': 100})
    assert decision == QueueDecision(should_queue=False, rule_id=5)


def test_catch_all_rule_matches_any_link():
    qr = make_qr()
    rule = make_rule(id=1, auto_queue=True)
    link_row = {'language': None, 'chat_title': None, 'member_count': None}
    decision = qr._evaluate_rules([rule], link_row)
    assert decision == QueueDecision(should_queue=True, rule_id=1)


def test_metadata_absent_link_only_matches_no_condition_rules():
    qr = make_qr()
    rule_with_conditions = make_rule(id=1, language_whitelist=['en'])
    catch_all = make_rule(id=2, auto_queue=True)
    link_row = {'language': None, 'chat_title': None, 'member_count': None}
    decision = qr._evaluate_rules([rule_with_conditions, catch_all], link_row)
    # rule_with_conditions should not match (language is None, not in whitelist)
    # catch_all should match
    assert decision == QueueDecision(should_queue=True, rule_id=2)


# ---------------------------------------------------------------------------
# _rule_matches tests
# ---------------------------------------------------------------------------

def test_language_blacklist_hit_returns_false():
    qr = make_qr()
    rule = make_rule(language_blacklist=['ru'])
    link_row = {'language': 'ru', 'chat_title': 'test', 'member_count': 100}
    assert qr._rule_matches(rule, link_row) is False


def test_language_whitelist_miss_returns_false():
    qr = make_qr()
    rule = make_rule(language_whitelist=['en', 'de'])
    link_row = {'language': 'ru', 'chat_title': 'test', 'member_count': 100}
    assert qr._rule_matches(rule, link_row) is False


def test_language_whitelist_hit_returns_true():
    qr = make_qr()
    rule = make_rule(language_whitelist=['en', 'de'])
    link_row = {'language': 'en', 'chat_title': 'test', 'member_count': 100}
    assert qr._rule_matches(rule, link_row) is True


def test_keyword_whitelist_no_title_match_returns_false():
    qr = make_qr()
    rule = make_rule(keyword_whitelist=['crypto', 'bitcoin'])
    link_row = {'language': 'en', 'chat_title': 'General Chat', 'member_count': 100}
    assert qr._rule_matches(rule, link_row) is False


def test_keyword_whitelist_with_matching_title_returns_true():
    qr = make_qr()
    rule = make_rule(keyword_whitelist=['crypto', 'bitcoin'])
    link_row = {'language': 'en', 'chat_title': 'Bitcoin Traders', 'member_count': 100}
    assert qr._rule_matches(rule, link_row) is True


def test_keyword_whitelist_case_insensitive():
    qr = make_qr()
    rule = make_rule(keyword_whitelist=['CRYPTO'])
    link_row = {'language': 'en', 'chat_title': 'crypto news', 'member_count': 100}
    assert qr._rule_matches(rule, link_row) is True


def test_keyword_blacklist_hit_returns_false():
    qr = make_qr()
    rule = make_rule(keyword_blacklist=['spam', 'ads'])
    link_row = {'language': 'en', 'chat_title': 'Free Ads Channel', 'member_count': 100}
    assert qr._rule_matches(rule, link_row) is False


def test_keyword_blacklist_no_hit_returns_true():
    qr = make_qr()
    rule = make_rule(keyword_blacklist=['spam', 'ads'])
    link_row = {'language': 'en', 'chat_title': 'Tech News', 'member_count': 100}
    assert qr._rule_matches(rule, link_row) is True


def test_min_member_count_with_none_member_count_returns_false():
    qr = make_qr()
    rule = make_rule(min_member_count=100)
    link_row = {'language': 'en', 'chat_title': 'test', 'member_count': None}
    assert qr._rule_matches(rule, link_row) is False


def test_min_member_count_satisfied():
    qr = make_qr()
    rule = make_rule(min_member_count=100)
    link_row = {'language': 'en', 'chat_title': 'test', 'member_count': 500}
    assert qr._rule_matches(rule, link_row) is True


def test_min_member_count_not_satisfied():
    qr = make_qr()
    rule = make_rule(min_member_count=1000)
    link_row = {'language': 'en', 'chat_title': 'test', 'member_count': 50}
    assert qr._rule_matches(rule, link_row) is False


def test_max_member_count_with_none_member_count_returns_false():
    qr = make_qr()
    rule = make_rule(max_member_count=10000)
    link_row = {'language': 'en', 'chat_title': 'test', 'member_count': None}
    assert qr._rule_matches(rule, link_row) is False


def test_max_member_count_satisfied():
    qr = make_qr()
    rule = make_rule(max_member_count=10000)
    link_row = {'language': 'en', 'chat_title': 'test', 'member_count': 5000}
    assert qr._rule_matches(rule, link_row) is True


def test_max_member_count_not_satisfied():
    qr = make_qr()
    rule = make_rule(max_member_count=100)
    link_row = {'language': 'en', 'chat_title': 'test', 'member_count': 500}
    assert qr._rule_matches(rule, link_row) is False


def test_keyword_whitelist_with_none_title_returns_false():
    qr = make_qr()
    rule = make_rule(keyword_whitelist=['news'])
    link_row = {'language': 'en', 'chat_title': None, 'member_count': 100}
    assert qr._rule_matches(rule, link_row) is False


def test_keyword_blacklist_with_none_title_returns_true():
    """Blacklist with None title should pass (no title to match against)."""
    qr = make_qr()
    rule = make_rule(keyword_blacklist=['spam'])
    link_row = {'language': 'en', 'chat_title': None, 'member_count': 100}
    assert qr._rule_matches(rule, link_row) is True


def test_language_blacklist_with_none_language_passes():
    """Blacklist with None language should pass (None is not in the blacklist)."""
    qr = make_qr()
    rule = make_rule(language_blacklist=['ru'])
    link_row = {'language': None, 'chat_title': 'test', 'member_count': 100}
    assert qr._rule_matches(rule, link_row) is True


def test_all_conditions_must_pass_and_semantics():
    """If any condition fails, the rule should not match."""
    qr = make_qr()
    rule = make_rule(
        language_whitelist=['en'],
        min_member_count=100,
    )
    # language matches but member_count is None → should fail
    link_row = {'language': 'en', 'chat_title': 'test', 'member_count': None}
    assert qr._rule_matches(rule, link_row) is False


def test_first_rule_wins_when_multiple_match():
    qr = make_qr()
    rule1 = make_rule(id=1, auto_queue=False)
    rule2 = make_rule(id=2, auto_queue=True)
    link_row = {'language': 'en', 'chat_title': 'test', 'member_count': 100}
    decision = qr._evaluate_rules([rule1, rule2], link_row)
    assert decision == QueueDecision(should_queue=False, rule_id=1)
