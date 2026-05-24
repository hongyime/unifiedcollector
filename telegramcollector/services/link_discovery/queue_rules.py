"""
QueueRules — evaluates active filter rules against a discovered link and
determines whether to auto-queue it.

Rules are loaded fresh from the database on each evaluate() call so that
operator changes take effect without a service restart.

First-match-wins: rules are evaluated in ascending id order and evaluation
stops at the first rule whose conditions are all satisfied.
"""
from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class QueueDecision:
    should_queue: bool      # True if a matching rule with auto_queue=True was found
    rule_id: int | None     # id of the first matching rule, or None if no rule matched


# ---------------------------------------------------------------------------
# QueueRules
# ---------------------------------------------------------------------------

class QueueRules:
    def __init__(self, db_pool) -> None:
        self._pool = db_pool

    async def _load_active_rules(self) -> list[dict]:
        rows = await self._pool.fetch(
            """
            SELECT id, name, language_whitelist, language_blacklist,
                   keyword_whitelist, keyword_blacklist,
                   min_member_count, max_member_count, auto_queue
              FROM link_discovery.queue_rules
             WHERE is_active = TRUE
             ORDER BY id ASC;
            """
        )
        return [dict(r) for r in rows]

    def _rule_matches(self, rule: dict, link_row: dict) -> bool:
        """
        Returns True only if ALL specified conditions in the rule are satisfied.
        AND semantics: every non-empty/non-null condition must pass.
        """
        lang = link_row.get('language')
        title = link_row.get('chat_title') or ''
        count = link_row.get('member_count')

        # 1. language_whitelist
        wl = rule.get('language_whitelist') or []
        if wl and lang not in wl:
            return False

        # 2. language_blacklist
        bl = rule.get('language_blacklist') or []
        if bl and lang in bl:
            return False

        # 3. keyword_whitelist
        kwl = rule.get('keyword_whitelist') or []
        if kwl:
            title_lower = title.lower()
            if not any(kw.lower() in title_lower for kw in kwl):
                return False

        # 4. keyword_blacklist
        kbl = rule.get('keyword_blacklist') or []
        if kbl:
            title_lower = title.lower()
            if any(kw.lower() in title_lower for kw in kbl):
                return False

        # 5. min_member_count
        min_mc = rule.get('min_member_count')
        if min_mc is not None:
            if count is None or count < min_mc:
                return False

        # 6. max_member_count
        max_mc = rule.get('max_member_count')
        if max_mc is not None:
            if count is None or count > max_mc:
                return False

        return True

    def _evaluate_rules(self, rules: list[dict], link_row: dict) -> QueueDecision:
        """
        Evaluate pre-loaded rules against link_row (used by tests to avoid DB calls).
        First-match-wins: returns on the first matching rule.
        """
        for rule in rules:
            if self._rule_matches(rule, link_row):
                return QueueDecision(
                    should_queue=rule['auto_queue'],
                    rule_id=rule['id'],
                )
        return QueueDecision(should_queue=False, rule_id=None)

    async def evaluate(self, link_row: dict) -> QueueDecision:
        """
        Loads active rules from DB and evaluates them against link_row.
        Returns QueueDecision with the first matching rule's outcome, or
        QueueDecision(should_queue=False, rule_id=None) if no rule matches.
        """
        rules = await self._load_active_rules()
        return self._evaluate_rules(rules, link_row)
