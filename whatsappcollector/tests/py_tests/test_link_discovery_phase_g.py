from __future__ import annotations

import importlib
import sys
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[2] / "services" / "link_discovery"
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))


def load_module(name: str):
    return importlib.import_module(f"link_discovery.{name}")


def test_extractor_finds_whatsapp_links_and_deduplicates():
    extractor = load_module("extractor")
    text = "join chat.whatsapp.com/AbCd1234 and again chat.whatsapp.com/AbCd1234 plus wa.me/1234567890"
    links = extractor.extract_links(text)

    assert ("https://chat.whatsapp.com/AbCd1234", "group_invite") in links
    assert ("https://wa.me/1234567890", "contact_link") in links
    assert len(links) == 2


def test_rule_selection_first_match_wins_and_inactive_skips():
    rules_mod = load_module("queue_rules")
    QueueRule = rules_mod.QueueRule

    rules = [
        QueueRule(id=2, name="inactive", keyword_whitelist=["foo"], keyword_blacklist=[], auto_queue=True, is_active=False),
        QueueRule(id=3, name="later", keyword_whitelist=["chat.whatsapp.com"], keyword_blacklist=[], auto_queue=True, is_active=True),
        QueueRule(id=1, name="first", keyword_whitelist=["chat.whatsapp.com"], keyword_blacklist=[], auto_queue=True, is_active=True),
    ]

    selected = rules_mod.select_matching_rule(rules, "https://chat.whatsapp.com/AbCd1234")
    assert selected is not None
    assert selected.id == 1
