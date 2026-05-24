from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

COLLECTOR_ROOT = REPO_ROOT / "services" / "collector"
if str(COLLECTOR_ROOT) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_ROOT))


from collector.database import Database


def _mock_pool_with_connection_and_transaction(conn: AsyncMock) -> MagicMock:
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=conn)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)

    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


def test_start_bootstrap_wizard_transitions_from_uninitialized():
    db = Database()
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(
        side_effect=[
            {"state": "uninitialized"},
            {
                "state": "wizard_in_progress",
                "wizard_version": "wizard-v1",
                "generated_defaults": {"LANGUAGE_WHITELIST": "en"},
                "initialized_by": None,
                "initialized_at": None,
                "updated_at": "2026-04-21T00:00:00Z",
            },
        ]
    )
    db.pool = _mock_pool_with_connection_and_transaction(mock_conn)

    result = asyncio.run(
        db.start_bootstrap_wizard(
            wizard_version="wizard-v1",
            actor_id="operator@example.com",
            actor_role="operator",
            generated_defaults={"LANGUAGE_WHITELIST": "en"},
        )
    )

    assert result["state"] == "wizard_in_progress"
    assert result["wizard_version"] == "wizard-v1"

    executed_sql = "\n".join(call.args[0] for call in mock_conn.execute.call_args_list)
    assert "UPDATE collector.control_bootstrap_state" in executed_sql
    assert "bootstrap_wizard_started" in executed_sql


def test_start_bootstrap_wizard_rejects_invalid_transition_from_initialized():
    db = Database()
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value={"state": "initialized"})
    db.pool = _mock_pool_with_connection_and_transaction(mock_conn)

    with pytest.raises(ValueError, match="Invalid bootstrap transition"):
        asyncio.run(
            db.start_bootstrap_wizard(
                wizard_version="wizard-v1",
                actor_id="operator@example.com",
                actor_role="operator",
            )
        )


def test_commit_bootstrap_baseline_requires_wizard_in_progress_state():
    db = Database()
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value={"state": "uninitialized"})
    db.pool = _mock_pool_with_connection_and_transaction(mock_conn)

    with pytest.raises(ValueError, match="Invalid bootstrap transition"):
        asyncio.run(
            db.commit_bootstrap_baseline(
                wizard_version="wizard-v1",
                generated_defaults={"COLLECTOR_BACKFILL_REQ_PER_MIN": 5},
                config_values=[
                    {
                        "service_name": "collector",
                        "config_key": "COLLECTOR_BACKFILL_REQ_PER_MIN",
                        "value": 5,
                        "scope": "bootstrap",
                        "requires_restart": False,
                    }
                ],
                actor_id="operator@example.com",
                actor_role="operator",
            )
        )


def test_commit_bootstrap_baseline_applies_config_and_initializes_state_transactionally():
    db = Database()
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(
        side_effect=[
            {"state": "wizard_in_progress"},
            None,
            {
                "state": "initialized",
                "wizard_version": "wizard-v1",
                "generated_defaults": {"COLLECTOR_BACKFILL_REQ_PER_MIN": 5},
                "initialized_by": "operator@example.com",
                "initialized_at": "2026-04-21T00:00:00Z",
                "updated_at": "2026-04-21T00:00:00Z",
            },
        ]
    )
    db.pool = _mock_pool_with_connection_and_transaction(mock_conn)

    result = asyncio.run(
        db.commit_bootstrap_baseline(
            wizard_version="wizard-v1",
            generated_defaults={"COLLECTOR_BACKFILL_REQ_PER_MIN": 5},
            config_values=[
                {
                    "service_name": "collector",
                    "config_key": "COLLECTOR_BACKFILL_REQ_PER_MIN",
                    "value": 5,
                    "scope": "bootstrap",
                    "requires_restart": False,
                }
            ],
            actor_id="operator@example.com",
            actor_role="operator",
            request_id="req-1",
            reason="first boot",
        )
    )

    assert result["state"] == "initialized"
    assert result["wizard_version"] == "wizard-v1"

    sql_calls = [call.args[0] for call in mock_conn.execute.call_args_list]
    idx_cfg = next(i for i, sql in enumerate(sql_calls) if "INSERT INTO collector.control_config_values" in sql)
    idx_state = next(i for i, sql in enumerate(sql_calls) if "UPDATE collector.control_bootstrap_state" in sql)
    assert idx_cfg < idx_state

    assert any("INSERT INTO collector.control_config_versions" in sql for sql in sql_calls)
    assert any("bootstrap_config_applied" in sql for sql in sql_calls)

    update_state_call = next(
        call for call in mock_conn.execute.call_args_list if "UPDATE collector.control_bootstrap_state" in call.args[0]
    )
    # SQL args: (sql, wizard_version, generated_defaults_json, initialized_by)
    assert update_state_call.args[1] == "wizard-v1"
    assert json.loads(update_state_call.args[2]) == {"COLLECTOR_BACKFILL_REQ_PER_MIN": 5}


def test_commit_bootstrap_baseline_rejects_secret_entries():
    db = Database()
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value={"state": "wizard_in_progress"})
    db.pool = _mock_pool_with_connection_and_transaction(mock_conn)

    with pytest.raises(ValueError, match="non-secret values"):
        asyncio.run(
            db.commit_bootstrap_baseline(
                wizard_version="wizard-v1",
                generated_defaults={"MEDIA_BRIDGE_SECRET": "<generated>"},
                config_values=[
                    {
                        "service_name": "collector",
                        "config_key": "MEDIA_BRIDGE_SECRET",
                        "value": "super-secret",
                        "scope": "bootstrap",
                        "is_secret": True,
                    }
                ],
                actor_id="operator@example.com",
                actor_role="operator",
            )
        )
