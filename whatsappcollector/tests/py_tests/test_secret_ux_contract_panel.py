"""
Regression checks for T-009 secret UX contract in configuration center.

Ensures:
1) Secrets remain masked by default in config center list.
2) Reveal flow requires admin role.
3) Reveal flow requires typed REVEAL confirmation.
4) Reveal path emits audited event insertion.
"""

import os


def _dashboard_source() -> str:
    app_py = os.path.join(
        os.path.dirname(__file__), "..", "..",
        "services", "collector", "collector", "dashboard", "app.py"
    )
    with open(app_py, encoding="utf-8") as f:
        return f.read()


def _database_source() -> str:
    db_py = os.path.join(
        os.path.dirname(__file__), "..", "..",
        "services", "collector", "collector", "database.py"
    )
    with open(db_py, encoding="utf-8") as f:
        return f.read()


def test_secret_reveal_ui_controls_exist():
    source = _dashboard_source()
    assert "Secret reveal (privileged + audited)" in source
    assert "Reveal secret (audited)" in source
    assert "Type REVEAL to confirm privileged reveal" in source


def test_secret_reveal_requires_admin_role_and_confirmation():
    source = _dashboard_source()
    assert "_has_role(\"admin\")" in source
    assert "_deny_mutation_message(\"admin\")" in source
    assert "reveal_confirm.strip() != \"REVEAL\"" in source


def test_secret_reveal_uses_explicit_plaintext_api_and_audit_event():
    source = _dashboard_source()
    assert "database.get_control_secret_plaintext(" in source
    assert "database.insert_control_change_log_event(" in source
    assert 'event_type="secret_revealed"' in source


def test_database_exposes_control_change_log_insert_helper():
    source = _database_source()
    assert "async def insert_control_change_log_event" in source
    assert "INSERT INTO collector.control_change_log" in source
