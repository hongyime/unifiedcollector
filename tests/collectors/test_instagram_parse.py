"""Unit tests for instagram pure parse helpers (STAGE 2 safety net).

Covers the SAFE extracted helpers only. The auth/2FA/challenge flows are NOT
extracted (require a watched live login cycle).
"""
import os
import tempfile
import json
from pathlib import Path

from src.collectors.instagram import InstagramCollector

from src.collectors.instagram.parse import (
    parse_browser_cookies,
    extract_post_edges_from_payload,
)


def _write_cookie_file(content: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    f.write(content)
    f.close()
    return f.name


def test_parse_browser_cookies_netscape():
    path = _write_cookie_file(
        "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tABC123\n"
        ".instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tXYZ789\n"
        "\n"
        "malformed line without tabs\n"
    )
    try:
        cookies = parse_browser_cookies(path)
    finally:
        os.unlink(path)
    assert cookies == {"sessionid": "ABC123", "csrftoken": "XYZ789"}


def test_parse_browser_cookie_vault_snapshot_scopes_to_instagram():
    payload = {
        "cookies": [
            {"domain": ".instagram.com", "name": "sessionid", "value": "ig-session"},
            {"domain": ".instagram.com", "name": "csrftoken", "value": "ig-csrf"},
            {"domain": ".threads.com", "name": "sessionid", "value": "threads-session"},
        ]
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(payload, handle)
        path = handle.name
    try:
        cookies = parse_browser_cookies(path)
    finally:
        os.unlink(path)
    assert cookies == {"sessionid": "ig-session", "csrftoken": "ig-csrf"}


def test_instagram_cookie_discovery_reads_vault_latest_snapshot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for index in range(1, 20):
        monkeypatch.delenv(f"INSTA_ACCOUNT_{index}_NAME", raising=False)
        monkeypatch.delenv(f"INSTA_ACCOUNT_{index}_USER", raising=False)
    vault_dir = tmp_path / "credentials" / "browser_cookies"
    vault_dir.mkdir(parents=True)
    (vault_dir / "latest.json").write_text(
        json.dumps(
            {
                "cookies": [
                    {"domain": ".instagram.com", "name": "sessionid", "value": "ig-session"},
                    {"domain": ".instagram.com", "name": "csrftoken", "value": "ig-csrf"},
                ]
            }
        ),
        encoding="utf-8",
    )
    collector = object.__new__(InstagramCollector)
    discovered = collector._auto_discover_cookies()
    assert discovered == {"browser-vault-cookie": "credentials/browser_cookies/latest.json"}


def test_extract_post_edges_nested():
    payload = {
        "data": {"user": {"edge_owner_to_timeline_media": {
            "edges": [{"node": {"id": "1"}}, {"node": {"id": "2"}}]
        }}}
    }
    edges = extract_post_edges_from_payload(payload)
    assert len(edges) == 2


def test_extract_post_edges_multiple_locations():
    payload = {
        "a": {"edge_owner_to_timeline_media": {"edges": [{"n": 1}]}},
        "b": {"c": {"edge_owner_to_timeline_media": {"edges": [{"n": 2}, {"n": 3}]}}},
    }
    assert len(extract_post_edges_from_payload(payload)) == 3


def test_extract_post_edges_guards():
    assert extract_post_edges_from_payload("notadict") == []
    assert extract_post_edges_from_payload({}) == []
    assert extract_post_edges_from_payload({"edge_owner_to_timeline_media": "wrongtype"}) == []
