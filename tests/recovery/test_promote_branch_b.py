from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.recovery import promote_branch_b as promotion


def _write(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def _ready_prerequisites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    candidate = tmp_path / "candidate.dump"
    candidate.write_bytes(b"validated candidate")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    monkeypatch.setattr(promotion, "EVIDENCE", tmp_path)
    monkeypatch.setattr(promotion, "CANDIDATE", candidate)
    monkeypatch.setattr(promotion, "CANDIDATE_SHA256", digest)
    (tmp_path / "task-12-authority-decision.md").write_text(
        f"Decision: **Branch B**\n{digest}\n", encoding="utf-8"
    )
    _write(tmp_path / "task-14-collector-production-recovery.json", {
        "status": "passed", "branch": "recovered", "candidate_sha256": digest,
        "rehearsal_resources_removed": True,
    })
    _write(tmp_path / "task-47-collector-production-recovery.json", {
        "selected_branch": "recovered",
        "expected_pgdata": promotion.RECOVERED_VOLUME,
        "rendered_bind_root": "/media/sg-ultra/unifiedcollector",
        "mount": {"uuid": "2C30-310F", "fstype": "exfat"},
        "production_postgres_running": False,
    })
    _write(tmp_path / "task-44-artifact-manifest.json", {"schema_version": 1})
    candidate_tsv = tmp_path / "task-12-media-deletion-proposal.tsv"
    candidate_tsv.write_text("media_id\tfile_path\tfile_size\tsha256\treason\n00000000-0000-0000-0000-000000000001\t/media/a.jpg\t1\t" + "a" * 64 + "\tmissing_canonical_path\n")
    _write(tmp_path / "task-11-media-reconciliation-plan.json", {"schema_version": 1})
    _write(tmp_path / "task-12-media-deletion-proposal.json", {
        "mode": "proposal_only_no_database_or_filesystem_mutation",
        "inputs": {"plan_sha256": hashlib.sha256((tmp_path / "task-11-media-reconciliation-plan.json").read_bytes()).hexdigest()},
        "candidate_tsv": {"path": str(candidate_tsv), "sha256": hashlib.sha256(candidate_tsv.read_bytes()).hexdigest(), "count": 1},
    })
    monkeypatch.setattr(promotion, "PLAN", tmp_path / "task-11-media-reconciliation-plan.json", raising=False)
    monkeypatch.setattr(promotion, "PROPOSAL", tmp_path / "task-12-media-deletion-proposal.json", raising=False)
    import scripts.recovery.apply_task12_media_deletion as deletion
    monkeypatch.setattr(deletion, "EVIDENCE", tmp_path)
    monkeypatch.setattr(deletion, "PLAN", tmp_path / "task-11-media-reconciliation-plan.json")
    monkeypatch.setattr(deletion, "PROPOSAL", tmp_path / "task-12-media-deletion-proposal.json")
    return digest


def test_prerequisites_require_a_hash_bound_cleaned_rehearsal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready_prerequisites(tmp_path, monkeypatch)
    promotion.require_prerequisites()
    receipt = json.loads((tmp_path / "task-14-collector-production-recovery.json").read_text())
    receipt["rehearsal_resources_removed"] = False
    _write(tmp_path / "task-14-collector-production-recovery.json", receipt)
    with pytest.raises(promotion.PromotionError, match="Task 14 receipt"):
        promotion.require_prerequisites()


def test_check_refuses_a_running_rejected_original_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready_prerequisites(tmp_path, monkeypatch)

    def inspect(name: str) -> dict | None:
        if name == promotion.ORIGINAL_CONTAINER:
            return {"State": {"Running": True}}
        return None

    monkeypatch.setattr(promotion, "docker_inspect", inspect)
    monkeypatch.setattr(promotion, "render_guard", lambda: pytest.fail("render guard must not run"))
    with pytest.raises(promotion.PromotionError, match="rejected original"):
        promotion.check()


def test_forward_container_rejects_any_original_volume_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(promotion, "docker_inspect", lambda _: {
        "Mounts": [
            {"Destination": "/var/lib/postgresql/data", "Name": promotion.RECOVERED_VOLUME},
            {"Destination": "/forbidden", "Name": promotion.ORIGINAL_VOLUME},
        ]
    })
    with pytest.raises(promotion.PromotionError, match="rejected original"):
        promotion.verify_forward_container()


def test_forward_container_requires_exactly_one_recovered_pgdata_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(promotion, "docker_inspect", lambda _: {
        "Mounts": [{"Destination": "/var/lib/postgresql/data", "Name": "wrong"}]
    })
    with pytest.raises(promotion.PromotionError, match="recovered pgdata"):
        promotion.verify_forward_container()


def test_promotion_bootstraps_only_the_restored_collector_login_without_exposing_its_password() -> None:
    text = Path(promotion.__file__).read_text(encoding="utf-8")
    assert "def bootstrap_collector_login" in text
    assert "ALTER ROLE collector LOGIN" in text
    assert "c.relowner='postgres'::regrole" in text
    assert "GRANT USAGE, CREATE ON SCHEMA public TO collector" in text
    assert "\\password collector" in text
    assert '"$POSTGRES_PASSWORD"' in text
    assert 'collector_login_bootstrap' in text


def test_collector_login_bootstrap_keeps_the_password_out_of_host_command_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(promotion, "run", lambda *command: commands.append(command) or "")
    promotion.bootstrap_collector_login()
    assert len(commands) == 3
    assert all("POSTGRES_PASSWORD=" not in argument for command in commands for argument in command)
    assert "ALTER SCHEMA public OWNER TO collector" in commands[1][-1]
    assert "${POSTGRES_PASSWORD" in commands[2][-1]
    assert commands[2][:6] == ("docker", "exec", "-u", "postgres", promotion.ORIGINAL_CONTAINER, "sh")


def test_promotion_rechecks_the_exact_post_deletion_media_count() -> None:
    text = Path(promotion.__file__).read_text(encoding="utf-8")
    assert "forward_identity(1064772)" in text
    assert 'forward_identity(1064772 - int(deletion["deleted_media_items"]))' in text


def test_forward_identity_binds_counts_to_a_new_logical_restore_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(promotion, "wait_ready", lambda _: None)

    def run(*command: str) -> str:
        return "Database system identifier:           7701744744580567085\n" if any(
            "pg_controldata" in part for part in command
        ) else "152|1064772"

    monkeypatch.setattr(
        promotion,
        "run",
        run,
    )
    identity = promotion.forward_identity(1064772)
    assert identity == {
        "source_system_identifier": promotion.SOURCE_SYSTEM_IDENTIFIER,
        "restored_system_identifier": "7701744744580567085",
        "schema_migrations": 152,
        "media_items": 1064772,
    }


def test_forward_identity_rejects_source_cluster_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(promotion, "wait_ready", lambda _: None)
    monkeypatch.setattr(
        promotion,
        "run",
        lambda *command: f"Database system identifier:           {promotion.SOURCE_SYSTEM_IDENTIFIER}\n",
    )
    with pytest.raises(promotion.PromotionError, match="unexpectedly reuses"):
        promotion.forward_identity(1064772)


def test_promotion_can_resume_after_a_failed_restore_already_removed_the_rejected_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(promotion, "check", lambda: None)
    monkeypatch.setattr(promotion, "docker_inspect", lambda _: None)
    monkeypatch.setattr(promotion, "restore_volume", lambda: (_ for _ in ()).throw(promotion.PromotionError("stop after retry boundary")))
    monkeypatch.setattr(promotion, "run", lambda *command: commands.append(command) or "")
    with pytest.raises(promotion.PromotionError, match="retry boundary"):
        promotion.promote()
    assert ("docker", "rm", promotion.ORIGINAL_CONTAINER) not in commands


def test_promotion_script_is_directly_executable() -> None:
    result = subprocess.run([sys.executable, "scripts/recovery/promote_branch_b.py", "--help"], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
