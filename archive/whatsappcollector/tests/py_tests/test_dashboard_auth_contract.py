"""
Regression checks for T-010 dashboard authN/authZ contract.

Validates:
1) Auth sidebar + credential settings are present.
2) Unauthenticated mutation requests are explicitly rejected.
3) Role boundaries are enforced for operator/admin actions.
"""

import os


def _dashboard_source() -> str:
    app_py = os.path.join(
        os.path.dirname(__file__), "..", "..",
        "services", "collector", "collector", "dashboard", "app.py"
    )
    with open(app_py, encoding="utf-8") as f:
        return f.read()


def _config_source() -> str:
    cfg_py = os.path.join(
        os.path.dirname(__file__), "..", "..",
        "services", "collector", "collector", "config.py"
    )
    with open(cfg_py, encoding="utf-8") as f:
        return f.read()


def _database_source() -> str:
    db_py = os.path.join(
        os.path.dirname(__file__), "..", "..",
        "services", "collector", "collector", "database.py"
    )
    with open(db_py, encoding="utf-8") as f:
        return f.read()


def test_config_exposes_dashboard_auth_settings():
    source = _config_source()
    required_keys = [
        "DASHBOARD_AUTH_REQUIRED",
        "DASHBOARD_VIEWER_USERNAME",
        "DASHBOARD_VIEWER_PASSWORD",
        "DASHBOARD_OPERATOR_USERNAME",
        "DASHBOARD_OPERATOR_PASSWORD",
        "DASHBOARD_ADMIN_USERNAME",
        "DASHBOARD_ADMIN_PASSWORD",
    ]
    for key in required_keys:
        assert key in source, f"Missing dashboard auth setting: {key}"


def test_dashboard_has_auth_sidebar_and_signin_flow():
    source = _dashboard_source()
    assert "def _render_auth_sidebar" in source
    assert "Sign in" in source
    assert "Sign out" in source
    assert "Dashboard auth required but no credentials are configured." in source


def test_dashboard_has_explicit_unauthenticated_mutation_rejection_message():
    source = _dashboard_source()
    assert "Unauthenticated mutation request rejected. Sign in first." in source


def test_dashboard_enforces_role_boundaries_for_mutations():
    source = _dashboard_source()
    assert '_has_role("operator")' in source
    assert '_has_role("admin")' in source
    assert '_deny_mutation_message("operator")' in source
    assert '_deny_mutation_message("admin")' in source


def test_database_has_hard_authorization_check_for_mutations():
    source = _database_source()
    assert "def _require_mutation_authorization" in source
    assert "Unauthenticated mutation request rejected" in source
    assert "Insufficient role" in source
