"""
Regression checks for T-007 unified operations dashboard shell/navigation.

Verifies:
1) Single dashboard route orchestrates module rendering via unified navigation.
2) Legacy functionality mapping matrix is linked in UI dev notes/tests.
"""

import os


def _dashboard_source() -> str:
    app_py = os.path.join(
        os.path.dirname(__file__), "..", "..",
        "services", "collector", "collector", "dashboard", "app.py"
    )
    with open(app_py, encoding="utf-8") as f:
        return f.read()


def test_unified_module_registry_exists_with_expected_modules():
    source = _dashboard_source()

    assert "_UNIFIED_OPERATION_MODULES" in source
    expected_module_ids = [
        "system_health",
        "service_hub",
        "session_controls",
        "findings_hub",
        "backfill_jobs",
        "dlq",
        "pruning_cursors",
        "danger_zone_wipe",
        "live_config",
        "legacy_mapping",
    ]
    for module_id in expected_module_ids:
        assert f'"{module_id}"' in source, f"Missing unified module id: {module_id}"


def test_sidebar_navigation_uses_unified_module_selector():
    source = _dashboard_source()

    assert "def _render_operations_navigation_sidebar" in source, (
        "Unified module sidebar renderer is required"
    )
    assert "Unified operations modules" in source, (
        "Sidebar must expose unified operations module selector"
    )
    assert "_ops_modules_selected" in source, (
        "Sidebar must persist selected unified modules"
    )


def test_render_async_uses_selected_modules_to_gate_panel_rendering():
    source = _dashboard_source()
    render_start = source.find("async def _render_async()")
    assert render_start != -1

    render_body = source[render_start:render_start + 2200]

    assert "selected_modules = _selected_operations_modules()" in render_body
    assert 'if "session_controls" in selected_modules' in render_body
    assert 'if "findings_hub" in selected_modules' in render_body
    assert 'if "backfill_jobs" in selected_modules or "dlq" in selected_modules' in render_body
    assert 'if "pruning_cursors" in selected_modules or "danger_zone_wipe" in selected_modules' in render_body
    assert 'if "live_config" in selected_modules' in render_body


def test_legacy_mapping_matrix_is_present_and_linked_in_ui_dev_notes():
    source = _dashboard_source()

    assert "_LEGACY_FUNCTIONALITY_MATRIX" in source, (
        "Legacy functionality mapping matrix constant must be defined"
    )
    assert "def _render_legacy_mapping_matrix" in source, (
        "Matrix renderer must be defined"
    )
    assert "Legacy functionality mapping (UI dev notes)" in source, (
        "UI must expose matrix in a dedicated dev-notes link/expander"
    )


def test_service_dashboard_hub_is_linked_for_legacy_surfaces():
    source = _dashboard_source()

    assert "_SERVICE_DASHBOARD_LINKS" in source
    for service in [
        "Dashboard Index",
        "Media Archival",
        "Face Recognition",
        "User Intelligence",
        "Link Discovery",
        "Bulk Sender",
    ]:
        assert f'"{service}"' in source, f"Missing service hub link for: {service}"
