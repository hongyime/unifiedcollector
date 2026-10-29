"""Scraped values rendered into the server HTML pages must be inert.

WhatsApp session identity (phone number, push name), the QR data URL, bridge
errors/status, and the social registry (platform, users, content counters,
profile photo URLs, usernames, display names, times_seen, contexts) are all
attacker-influenced. They are interpolated into the inline templates in
``whatsapp.py`` and ``social.py``, so every one must pass through ``esc()``
before it reaches ``innerHTML``.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-only-for-pytest-do-not-use")
os.environ.setdefault("DASHBOARD_ADMIN_PASSWORD", "x")

import pytest  # noqa: E402

from src.dashboard.api import social_page, whatsapp_link_page  # noqa: E402


def _html(handler) -> str:
    return asyncio.run(handler(_user={})).body.decode("utf-8")


_PAGES = {
    "whatsapp_link": lambda: _html(whatsapp_link_page),
    "social": lambda: _html(social_page),
}

_ESC_FN = (
    "function esc(s){return String(s||'').replace(/[&<>\"']/g,"
    "m=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[m]));}"
)

# Template-literal expressions that legitimately are not a bare esc(...) call.
# Both only concatenate *static* markup around an esc()-wrapped value.
_SAFE_NON_ESC_INTERPOLATIONS = {
    "u.profile_photo_url?'<img src=\"'+esc(u.profile_photo_url)+'\">':''",
    "u.username?esc(u.username):'<span class=muted>—</span>'",
}


def test_both_pages_define_the_same_escape_helper():
    for name, get_html in _PAGES.items():
        assert _ESC_FN in get_html(), name


def test_escape_helper_neutralizes_html_payload():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable to execute the template's esc()")
    html = _PAGES["whatsapp_link"]()
    match = re.search(r"function esc\(s\)\{.*?\}\n", html)
    assert match, "esc() definition not found"
    script = match.group(0)
    script += "\nconsole.log(esc('<img src=x onerror=alert(1)>'));\n"
    script += "console.log(esc('\\\"quoted\\\" & \\'single\\''));\n"
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout.splitlines()
    assert out[0] == "&lt;img src=x onerror=alert(1)&gt;"
    assert "&quot;" in out[1] and "&#39;" in out[1] and "&amp;" in out[1]


def test_no_unescaped_template_interpolation_in_innerhtml():
    for name, get_html in _PAGES.items():
        html = get_html()
        for line in html.splitlines():
            if "innerHTML" not in line or "${" not in line:
                continue
            for expr in re.findall(r"\$\{([^{}]*)\}", line):
                expr = expr.strip()
                if expr.startswith("esc("):
                    continue
                assert expr in _SAFE_NON_ESC_INTERPOLATIONS, (
                    f"{name}: unescaped interpolation ${{{expr}}}"
                )


def test_whatsapp_dynamic_concat_sites_are_escaped():
    html = _PAGES["whatsapp_link"]()
    required = [
        "esc(s.phone_number)",
        "esc(s.push_name)",
        "esc(d.qr)",
        "esc(d.error||'')",
        "esc(d.status||'starting')",
        "esc(e&&e.message?e.message:e)",
    ]
    for expr in required:
        assert expr in html, f"missing escaped {expr}"
    unsafe = [
        "'+s.phone_number+'",
        "'+s.push_name+'",
        "'+d.qr+'",
        "+(d.error||'')",
        "+(d.status||'starting')",
        "+(e&&e.message?e.message:e)",
    ]
    for expr in unsafe:
        assert expr not in html, f"unescaped dynamic value {expr}"
