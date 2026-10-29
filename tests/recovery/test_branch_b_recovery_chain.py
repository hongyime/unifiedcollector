from pathlib import Path


def test_chain_is_receipt_gated_and_never_starts_consumers() -> None:
    text = Path("scripts/recovery/run_branch_b_recovery_chain.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert text.index("finalize_task44_artifacts.sh") < text.index("promote_branch_b.py --check")
    assert text.index("promote_branch_b.py --check") < text.index("promote_branch_b.py --run")
    assert text.index("promote_branch_b.py --run") < text.index("create_task49_activation_manifest.py")
    assert text.index("create_task49_activation_manifest.py") < text.index("record_task50_fail_closed.py")
    assert "docker compose up" not in text
    assert "consumer" not in text
