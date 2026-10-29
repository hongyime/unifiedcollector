from __future__ import annotations

import copy

from scripts.verify_credential_rotation_plan import load_matrix, verify_matrix


def test_task_40_matrix_covers_every_value_free_credential() -> None:
    assert verify_matrix(load_matrix()) == []


def test_task_40_matrix_rejects_missing_or_duplicate_classification() -> None:
    matrix = copy.deepcopy(load_matrix())
    matrix["env_only"].remove("DASHBOARD_JWT_SECRET")
    assert "matrix must classify every mapped credential exactly once" in verify_matrix(matrix)

    matrix = copy.deepcopy(load_matrix())
    matrix["env_only"].append("DASHBOARD_JWT_SECRET")
    assert "env_only credentials must not be duplicated" in verify_matrix(matrix)


def test_task_40_matrix_rejects_restart_only_or_unsafe_rollback() -> None:
    matrix = copy.deepcopy(load_matrix())
    matrix["env_only_rule"] = "A restart is sufficient."
    matrix["repair_policy"]["exposed_or_revoked_rollback"] = "eligible"
    errors = verify_matrix(matrix)
    assert "env-only rule must reject restart-only and require recreate" in errors
    assert "exposed/revoked credentials must not be rollback eligible" in errors
