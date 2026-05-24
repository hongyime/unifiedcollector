"""
Property-based tests for services/link_discovery/queue_rules.py

Feature: link-discovery-service

Properties tested:
  5. First-match-wins          — Validates: Requirements 7.2
  6. Rule condition AND semantics — Validates: Requirements 7.3, 7.4, 7.5, 7.6, 7.7, 7.8
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from services.link_discovery.queue_rules import QueueRules


# ---------------------------------------------------------------------------
# Property 5: First-match-wins
# Validates: Requirements 7.2
# ---------------------------------------------------------------------------

@given(
    rules=st.lists(
        st.fixed_dictionaries({
            'id': st.integers(min_value=1, max_value=1000),
            'language_whitelist': st.just([]),
            'language_blacklist': st.just([]),
            'keyword_whitelist': st.just([]),
            'keyword_blacklist': st.just([]),
            'min_member_count': st.none(),
            'max_member_count': st.none(),
            'auto_queue': st.booleans(),
        }),
        min_size=2, max_size=10,
    ).map(lambda rs: sorted({r['id']: r for r in rs}.values(), key=lambda r: r['id'])),
)
@settings(max_examples=200)
def test_first_match_wins(rules):
    # All rules have no conditions → all match. First rule must win.
    qr = QueueRules.__new__(QueueRules)
    link_row = {'language': 'en', 'chat_title': 'test', 'member_count': 100}
    decision = qr._evaluate_rules(rules, link_row)
    assert decision.rule_id == rules[0]['id']


# ---------------------------------------------------------------------------
# Property 6: Rule condition AND semantics
# Validates: Requirements 7.3, 7.4, 7.5, 7.6, 7.7, 7.8
# ---------------------------------------------------------------------------

@given(
    lang=st.one_of(st.none(), st.just('en'), st.just('ru')),
    title=st.one_of(st.none(), st.just(''), st.just('English Group'), st.just('Русская группа')),
    count=st.one_of(st.none(), st.integers(min_value=0, max_value=100000)),
    whitelist=st.lists(st.sampled_from(['en', 'ms', 'de']), min_size=1, max_size=3),
)
@settings(max_examples=300)
def test_language_whitelist_and_semantics(lang, title, count, whitelist):
    rule = {
        'id': 1, 'language_whitelist': whitelist, 'language_blacklist': [],
        'keyword_whitelist': [], 'keyword_blacklist': [],
        'min_member_count': None, 'max_member_count': None, 'auto_queue': True,
    }
    qr = QueueRules.__new__(QueueRules)
    link_row = {'language': lang, 'chat_title': title, 'member_count': count}
    result = qr._rule_matches(rule, link_row)
    assert result == (lang in whitelist)
