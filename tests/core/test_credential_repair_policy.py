from __future__ import annotations

import pytest

from src.core.credential_repair_policy import HUMAN_BLOCKS, classify_credential


def test_valid_owned_session_is_preserved_not_rotated() -> None:
    decision = classify_credential(present=True, valid=True, owned=True)
    assert decision.status == "ok"
    assert decision.action == "preserve"
    assert decision.rollback_eligible is True


@pytest.mark.parametrize("field,reason", [
    ("present", "missing"), ("valid", "expired"), ("owned", "misowned"),
    ("exposed", "exposed"), ("deployment_required", "deployment_required"),
])
def test_repair_reasons_are_explicit(field: str, reason: str) -> None:
    values = dict(present=True, valid=True, owned=True, exposed=False, deployment_required=False)
    values[field] = False if field in {"present", "valid", "owned"} else True
    decision = classify_credential(**values)
    assert decision.status == "repair"
    assert decision.action == "repair"
    assert reason in decision.reasons


def test_exposed_or_revoked_credential_is_not_rollback_eligible() -> None:
    assert classify_credential(present=True, valid=True, owned=True, exposed=True).rollback_eligible is False
    assert classify_credential(present=True, valid=True, owned=True, revoked=True).rollback_eligible is False


@pytest.mark.parametrize("block", sorted(HUMAN_BLOCKS))
def test_human_blocks_never_report_ok(block: str) -> None:
    decision = classify_credential(present=True, valid=True, owned=True, human_block=block)  # type: ignore[arg-type]
    assert decision.status == "blocked"
    assert decision.action == "human_action"


def test_unknown_human_block_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown human block"):
        classify_credential(present=True, valid=True, owned=True, human_block="captcha")  # type: ignore[arg-type]
