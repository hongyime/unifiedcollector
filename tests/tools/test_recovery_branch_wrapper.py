"""Static contracts for Task 13's fail-closed Compose branch selector."""
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "scripts" / "recovery" / "compose_branch.sh"
STATE_VERIFIER = ROOT / "scripts" / "recovery" / "verify_state.sh"
REHEARSAL = ROOT / "scripts" / "recovery" / "rehearse_branch_b.sh"
MEDIA_EXPORT = ROOT / "scripts" / "export_media_reconciliation_rows.sh"
TASK47 = ROOT / "scripts" / "verify_task47_cutover.py"
TASK48 = ROOT / "scripts" / "recovery" / "promote_branch_b.py"
EVIDENCE = ROOT / ".omo" / "evidence" / "collector-production-recovery"


class _ComposeLoader(yaml.SafeLoader):
    pass


_ComposeLoader.add_constructor("!reset", lambda loader, node: loader.construct_sequence(node))


def test_branch_wrapper_uses_the_repo_root_env_file_for_compose_rendering() -> None:
    """Compose resolves its default .env beside docker/, not at repository root."""
    text = WRAPPER.read_text(encoding="utf-8")
    assert 'root_env_file=$(realpath -e -- "$repo_root/.env")' in text
    assert 'env_file=$root_env_file' in text
    assert 'production accepts only the repository-root .env' in text


def test_branch_wrapper_requires_an_explicit_nonproduction_env_file_for_rehearsal() -> None:
    text = WRAPPER.read_text(encoding="utf-8")
    assert '--env-file)' in text
    assert 'rehearsal requires an explicit non-production --env-file' in text
    assert 'production accepts only the repository-root .env' in text


def test_branch_wrapper_keeps_immutable_override_and_volume_guards() -> None:
    text = WRAPPER.read_text(encoding="utf-8")
    assert 'sha256sum --check --status' in text
    assert 'branch/volume mismatch' in text
    assert 'uc-rehearsal-*' in text


def test_rehearsal_overrides_isolate_project_containers_and_ports() -> None:
    for branch in ("baseline", "recovered"):
        override = yaml.load(
            (EVIDENCE / f"task-13-compose-rehearsal-{branch}.yml").read_text(encoding="utf-8"),
            Loader=_ComposeLoader,
        )
        assert override["name"] == "unifiedcollector-recovery-rehearsal"
        postgres = override["services"]["postgres"]
        assert postgres["container_name"] == "uc-rehearsal-postgres"
        assert postgres["restart"] == "no"
        assert postgres["ports"] == []
        assert override["services"]["schema_init"]["container_name"] == "uc-rehearsal-schema-init"


def test_state_verifier_selects_a_rehearsal_container_without_production_fallback() -> None:
    text = STATE_VERIFIER.read_text(encoding="utf-8")
    assert '--namespace production|rehearsal' in text
    assert 'container_name=uc-rehearsal-postgres' in text
    assert 'container_name=unifiedcollector_postgres' in text
    assert '--db-user NAME' in text
    assert 'sudo -n test -d "$media_root"' in text
    assert 'for _ in $(seq 1 30)' in text
    assert 'migration ledger is unreadable after PostgreSQL readiness wait' in text


def test_branch_b_rehearsal_driver_is_disposable_and_never_uses_production_volumes() -> None:
    text = REHEARSAL.read_text(encoding="utf-8")
    assert 'uc-rehearsal-pgdata' in text
    assert 'uc-rehearsal-pgdata-recovered' in text
    assert '--network none' in text
    assert 'docker volume rm "$recovered_volume" "$baseline_volume"' in text
    assert 'task-14-collector-production-recovery.json' in text
    assert '"rehearsal_resources_removed": True' in text
    assert 'mv "$receipt_tmp" "$receipt"' in text
    assert 'sudo -n tee "$logs/candidate.sha256"' in text
    assert 'sudo -n tee "$logs/SHA256SUMS"' in text
    assert 'candidate_sha=$(sudo -n awk' in text
    assert 'enable_disposable_collector_login' in text
    assert '--single -D /var/lib/postgresql/data unifiedcollector' in text
    assert 'ALTER ROLE collector LOGIN;' in text
    assert '--db-user collector --expect baseline' in text
    assert 'remove_rehearsal_postgres' in text
    assert 'docker rm -f uc-rehearsal-postgres' in text
    assert 'wait_for_stable_restore_database' in text
    assert '[[ $successes -ge 2 ]]' in text
    assert 'unifiedcollector_pgdata' not in text


def test_media_reference_export_is_read_only_and_bound_to_the_isolated_restore() -> None:
    text = MEDIA_EXPORT.read_text(encoding="utf-8")
    assert 'uc-task9-restore-*' in text
    assert 'true\\|none' in text
    assert 'COPY (SELECT id, file_path, file_size, sha256 FROM media_items ORDER BY id)' in text
    assert 'psql -q -v ON_ERROR_STOP=1' in text
    assert 'ON_ERROR_STOP=1' in text
    assert 'DELETE ' not in text
    assert 'UPDATE ' not in text


def test_task47_verifier_is_branch_b_specific_and_refuses_running_stack() -> None:
    text = TASK47.read_text(encoding="utf-8")
    assert 'EXPECTED_UUID = "2C30-310F"' in text
    assert 'unifiedcollector_pgdata_recovered' in text
    assert 'findmnt' in text
    assert '"SOURCE,FSTYPE,UUID,TARGET"' in text
    assert 'name.startswith("unifiedcollector_")' in text
    assert 'docker", "compose", "up' not in text


def test_task48_promotion_is_receipt_gated_and_never_starts_the_original_volume() -> None:
    text = TASK48.read_text(encoding="utf-8")
    assert 'task-14-collector-production-recovery.json' in text
    assert 'task-44-artifact-manifest.json' in text
    assert 'task-47-collector-production-recovery.json' in text
    assert 'RECOVERED_VOLUME = "unifiedcollector_pgdata_recovered"' in text
    assert 'ORIGINAL_VOLUME = "unifiedcollector_pgdata"' in text
    assert '"docker", "start", ORIGINAL_CONTAINER' in text
    assert 'ORIGINAL_VOLUME for mount in mounts' in text
    assert '"--network", "none"' in text
    assert 'docker", "compose", "down' not in text
