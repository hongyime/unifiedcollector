from pathlib import Path


def test_finalizer_builds_before_any_immutable_task44_record() -> None:
    text = Path("scripts/finalize_task44_artifacts.sh").read_text(encoding="utf-8")
    assert "docker compose --env-file .env -f docker/docker-compose.yml build" in text
    assert text.index("docker compose --env-file .env -f docker/docker-compose.yml build") < text.index("create_task44_runtime_manifest.py")
    assert text.index("create_task44_runtime_manifest.py") < text.index("freeze_task44_artifact_manifest.py")
    assert "compose_branch.sh" in text and "--branch recovered" in text
    assert "config --no-interpolate" in text
    assert "up " not in text


def test_finalizer_can_write_a_new_immutable_revision_without_reusing_canonical_paths() -> None:
    text = Path("scripts/finalize_task44_artifacts.sh").read_text(encoding="utf-8")
    assert 'TASK44_REVISION must be empty or r followed by digits' in text
    assert 'task-44-artifact-manifest$suffix.json' in text
    assert 'task-49-activation-manifest$suffix.json' in text
