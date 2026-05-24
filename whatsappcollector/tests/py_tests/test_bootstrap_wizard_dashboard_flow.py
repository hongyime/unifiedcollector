"""
Dashboard wizard flow regression checks for T-006.

Ensures:
- setup wizard panel exists,
- operational panels are blocked until bootstrap is initialized,
- generated defaults are visible/reviewable before commit,
- commit path uses bootstrap baseline backend API.
"""

import os


def _dashboard_source() -> str:
    app_py = os.path.join(
        os.path.dirname(__file__), "..", "..",
        "services", "collector", "collector", "dashboard", "app.py"
    )
    with open(app_py, encoding="utf-8") as f:
        return f.read()


def test_bootstrap_wizard_panel_function_exists():
    source = _dashboard_source()
    assert "async def _bootstrap_wizard_panel" in source, (
        "Collector dashboard must define _bootstrap_wizard_panel()"
    )


def test_render_async_blocks_operations_until_initialized():
    source = _dashboard_source()
    render_start = source.find("async def _render_async()")
    assert render_start != -1

    render_body = source[render_start:render_start + 1200]

    assert "get_control_bootstrap_state" in render_body, (
        "_render_async must check bootstrap state before rendering operations"
    )
    assert "_bootstrap_wizard_panel" in render_body, (
        "_render_async must render setup wizard when bootstrap is not initialized"
    )
    assert "await database.close()" in render_body and "return" in render_body, (
        "_render_async must early-return after wizard gate to block normal operations"
    )

    wizard_idx = render_body.find("_bootstrap_wizard_panel")
    ops_idx = render_body.find("_qr_and_logout_panel()")
    assert wizard_idx != -1 and ops_idx != -1 and wizard_idx < ops_idx, (
        "Wizard gate must run before operational panels"
    )


def test_wizard_start_uses_bootstrap_start_api():
    source = _dashboard_source()
    assert "start_bootstrap_wizard" in source, (
        "Wizard start action must call database.start_bootstrap_wizard()"
    )


def test_wizard_commit_requires_initialize_confirmation_and_uses_commit_api():
    source = _dashboard_source()
    assert "Type INITIALIZE to commit baseline" in source, (
        "Wizard must require typed confirmation before committing baseline"
    )
    assert "commit_bootstrap_baseline" in source, (
        "Wizard commit action must call database.commit_bootstrap_baseline()"
    )


def test_generated_defaults_are_visible_before_commit():
    source = _dashboard_source()
    assert "Generated defaults (reviewable before commit)" in source, (
        "Wizard must display generated defaults before commit"
    )
    assert "Final generated defaults preview" in source, (
        "Wizard must show final defaults preview before commit"
    )
