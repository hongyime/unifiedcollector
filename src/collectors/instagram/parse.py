"""Pure parsing helpers for the instagram collector.

Extracted from the collector class (STAGE 2 of the per-package refactor). These
are side-effect-light: ``parse_browser_cookies`` reads a Netscape cookie file
(I/O but no ``self``); ``extract_post_edges_from_payload`` is a pure dict walk.
Both are testable in isolation. The collector keeps thin staticmethod shims.

NOTE: the risky instagram auth/2FA/challenge flows are deliberately NOT extracted
here -- they require a watched live login cycle (see skill collector-package-refactor).
"""
from __future__ import annotations


def parse_browser_cookies(filepath: str) -> dict[str, str]:
    cookies = {}
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies[parts[5]] = parts[6]
    return cookies


def extract_post_edges_from_payload(payload: dict) -> list:
    """Best-effort traversal of IG's nested JSON shapes to find post edges."""
    edges: list = []
    if not isinstance(payload, dict):
        return edges

    def walk(obj):
        if isinstance(obj, dict):
            # Common IG shape
            etmm = obj.get("edge_owner_to_timeline_media")
            if isinstance(etmm, dict):
                e = etmm.get("edges")
                if isinstance(e, list):
                    edges.extend(e)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(payload)
    return edges
