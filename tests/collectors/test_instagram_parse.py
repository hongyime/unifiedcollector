"""Unit tests for instagram pure parse helpers (STAGE 2 safety net).

Covers the SAFE extracted helpers only. The auth/2FA/challenge flows are NOT
extracted (require a watched live login cycle).
"""
import os
import tempfile

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
