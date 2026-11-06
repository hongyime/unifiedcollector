"""The browser egress proxy admits only explicit HTTPS CONNECT hosts."""
from __future__ import annotations

from scripts.uc_browser_egress_proxy import allowed_host, load_policy, valid_host_rule


def test_allowed_host_requires_an_exact_host_or_real_subdomain() -> None:
    policy = ["snapchat.com", "*.sc-cdn.net"]

    assert allowed_host("snapchat.com", policy)
    assert allowed_host("edge.sc-cdn.net", policy)
    assert not allowed_host("evil-snapchat.com", policy)
    assert not allowed_host("sc-cdn.net", policy)
    assert not allowed_host("snapchat.com.evil.test", policy)


def test_policy_rejects_an_empty_host_list(tmp_path) -> None:
    policy = tmp_path / "policy.json"
    policy.write_text('{"allowed_hosts": []}', encoding="utf-8")

    try:
        load_policy(policy)
    except ValueError as exc:
        assert "allowed_hosts" in str(exc)
    else:
        raise AssertionError("empty policy unexpectedly accepted")


def test_wildcard_rules_are_valid_but_do_not_match_their_apex() -> None:
    assert valid_host_rule("*.sc-cdn.net")
    assert not valid_host_rule("*.not a hostname")
