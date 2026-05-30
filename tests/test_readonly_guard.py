"""Read-only enforcement guard (P2-5).

The system is read-only ingestion by policy, but that was convention/README only
— nothing stopped a bad import from archive/ (which still contains outbound
code) adding a send/react/edit/delete call to a collector.

This test statically scans the collector sources for outbound-shaped telethon /
client calls and fails if any appear OUTSIDE the sanctioned allowlist. It is a
cheap CI tripwire, not a runtime sandbox — but it catches the realistic failure
mode (someone copies outbound code in) at PR time.

Sanctioned outbound paths (operator notifications, not data manipulation):
  - src/core/hub_notifier.py  — collection-status pings to the hub group
  - src/bots/                 — onboarding bots (/startcollector flow)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# Outbound-shaped call patterns that must NOT appear in read-only collectors.
_FORBIDDEN = re.compile(
    r"\.(send_message|send_file|send_reaction|send|react|"
    r"edit_message|delete_messages|forward_messages|forward)\s*\("
)

# Files allowed to perform outbound actions (operator notifications / onboarding).
_ALLOWLIST = {
    "hub_notifier.py",
}
_ALLOWLIST_DIRS = {"bots"}

_SRC = Path(__file__).resolve().parent.parent / "src"


def _collector_files():
    for p in (_SRC / "collectors").rglob("*.py"):
        if p.name == "__init__.py":
            continue
        yield p


def test_collectors_have_no_outbound_calls():
    violations = []
    for path in _collector_files():
        if path.name in _ALLOWLIST:
            continue
        if any(part in _ALLOWLIST_DIRS for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _FORBIDDEN.search(line):
                # Allow the sanctioned hub notifier reference passing through.
                if "hub_notifier" in line or "_hub_notifier" in line:
                    continue
                violations.append(f"{path.name}:{i}: {stripped[:100]}")
    assert not violations, (
        "Outbound (write) calls found in read-only collectors — the system is "
        "read-only ingestion only:\n" + "\n".join(violations)
    )
