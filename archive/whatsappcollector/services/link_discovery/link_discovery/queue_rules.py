from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class QueueRule:
    id: int
    name: str
    keyword_whitelist: list[str] | None
    keyword_blacklist: list[str] | None
    auto_queue: bool
    is_active: bool
    preferred_session: str | None
    session_allowlist: list[str] | None


def _contains_any(text: str, needles: list[str]) -> bool:
    lowered = text.lower()
    return any(n.lower() in lowered for n in needles)


def matches_rule(rule: QueueRule, text: str) -> bool:
    if not rule.is_active:
        return False

    whitelist = rule.keyword_whitelist or []
    blacklist = rule.keyword_blacklist or []

    if blacklist and _contains_any(text, blacklist):
        return False

    if whitelist:
        return _contains_any(text, whitelist)

    return True


def select_matching_rule(rules: list[QueueRule], text: str) -> QueueRule | None:
    for rule in sorted(rules, key=lambda r: r.id):
        if matches_rule(rule, text):
            return rule
    return None
