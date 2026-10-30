from pathlib import Path


def test_task53_forward_rehearsal_is_disposable_and_requires_a_verified_dump() -> None:
    text = Path("scripts/recovery/rehearse_task53_forward.sh").read_text(encoding="utf-8")

    assert 'set -Eeuo pipefail' in text
    assert 'task-53-production-forward-db.dump' in text
    assert 'task-53-production-forward-cursor-queue.json' in text
    assert 'Task 53 rehearsal receipt already exists' in text
    assert 'sha256sum --check --status' in text
    assert 'pgvector/pgvector:pg16 pg_restore --list /task-53-forward.dump' in text
    assert 'mktemp -d "$evidence/task-53-rehearsal-XXXXXXXX"' in text
    assert 'uc-rehearsal-pgdata-recovered' in text
    assert '--namespace rehearsal' in text
    assert '--branch recovered' in text
    assert 'docker volume rm "$volume"' in text
    assert 'task53_cursor_queue_import' in text
    assert 'verify_task53_media_union.py' in text
    assert 'task-53-rehearsal-media-' in text
    assert 'rmdir -- "$media_stage"' in text
    assert 'unifiedcollector_pgdata_recovered' not in text
