"""
Regression checks for T-008 unified configuration center.

Ensures:
1) Configuration center module exists in unified shell.
2) Cross-service metadata uses PARAMETER_REGISTRY.
3) Secret-aware save path is wired to control-plane encrypted secret API.
4) Validation messaging is present for missing secret key/value.
"""

import os


def _dashboard_source() -> str:
    app_py = os.path.join(
        os.path.dirname(__file__), "..", "..",
        "services", "collector", "collector", "dashboard", "app.py"
    )
    with open(app_py, encoding="utf-8") as f:
        return f.read()


def test_config_center_module_registered_in_unified_modules():
    source = _dashboard_source()
    assert '"config_center"' in source, "config_center module must be in unified module registry"


def test_configuration_center_panel_function_exists():
    source = _dashboard_source()
    assert "async def _configuration_center_panel" in source
    assert "Configuration Center" in source


def test_configuration_center_uses_parameter_registry_for_cross_service_metadata():
    source = _dashboard_source()
    assert "PARAMETER_REGISTRY" in source
    assert "available_services = sorted(PARAMETER_REGISTRY.keys())" in source
    assert "for meta in PARAMETER_REGISTRY.get(selected_service, [])" in source


def test_configuration_center_secret_save_uses_encrypted_control_plane_api():
    source = _dashboard_source()
    assert "Save secret" in source
    assert "database.upsert_control_secret(" in source
    assert "plaintext_value=secret_value" in source


def test_configuration_center_secret_validation_messages_present():
    source = _dashboard_source()
    assert "Secret key is required." in source
    assert "Secret value is required." in source
