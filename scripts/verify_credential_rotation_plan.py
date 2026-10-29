"""Validate the value-free Task 40 credential/recreate contract."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.verify_credential_consumer_map import MAP_PATH, load_map

REPO_ROOT = Path(__file__).resolve().parent.parent
MATRIX_PATH = REPO_ROOT / "config" / "task-40-credential-matrix.json"
SERVER_SIDE = {"POSTGRES_PASSWORD", "RABBITMQ_PASSWORD", "REDIS_PASSWORD"}


def load_matrix(path: Path = MATRIX_PATH) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read credential rotation matrix: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("unsupported credential rotation matrix schema")
    return document


def verify_matrix(matrix: dict[str, Any], credential_map: dict[str, Any] | None = None) -> list[str]:
    """Return value-free contract violations without reading a real env file."""
    credential_map = load_map() if credential_map is None else credential_map
    expected = {row["name"] for row in credential_map["credentials"]}
    server_side = matrix.get("server_side")
    env_only = matrix.get("env_only")
    if not isinstance(server_side, dict) or not isinstance(env_only, list):
        return ["matrix must contain server_side and env_only classifications"]
    if set(server_side) != SERVER_SIDE:
        return ["server-side classification must contain exactly PostgreSQL, RabbitMQ, and Redis passwords"]
    classified = set(server_side) | set(env_only)
    errors: list[str] = []
    if classified != expected:
        errors.append("matrix must classify every mapped credential exactly once")
    if len(env_only) != len(set(env_only)):
        errors.append("env_only credentials must not be duplicated")
    for name, row in server_side.items():
        if not isinstance(row, dict):
            errors.append(f"invalid server-side row: {name}")
            continue
        if row.get("new_auth") != "required" or row.get("old_auth") != "must_fail_or_be_explicitly_disabled":
            errors.append(f"server-side auth proof is incomplete: {name}")
        if row.get("rollback") != "only_if_old_credential_remains_valid":
            errors.append(f"server-side rollback rule is incomplete: {name}")
    if "restart" not in str(matrix.get("env_only_rule", "")).lower() or "recreate" not in str(matrix.get("env_only_rule", "")).lower():
        errors.append("env-only rule must reject restart-only and require recreate")
    if matrix.get("repair_policy", {}).get("exposed_or_revoked_rollback") != "not_rollback_eligible":
        errors.append("exposed/revoked credentials must not be rollback eligible")
    if not isinstance(matrix.get("service_update_order"), list) or not matrix["service_update_order"]:
        errors.append("service update order is required")
    return errors
