from pathlib import Path


def test_completion_captures_only_quiesced_private_artifacts_before_release() -> None:
    text = Path("scripts/recovery/complete_task53_forward_capture.sh").read_text(encoding="utf-8")

    assert 'set -Eeuo pipefail' in text
    assert 'pg_restore --list /evidence/task-53-production-forward-db.dump' in text
    assert 'capture_task53_cursor_queue.py' in text
    assert 'COPY (SELECT id, file_path, file_size, sha256 FROM media_items ORDER BY id)' in text
    assert 'writer remains running' in text
    assert 'writer_resume_order' in text
    assert 'task-53-production-forward-capture-complete' in text


def test_fresh_capture_guard_resumes_only_approved_writers_on_every_exit() -> None:
    text = Path("scripts/recovery/run_task53_forward_capture.sh").read_text(encoding="utf-8")

    assert 'set -Eeuo pipefail' in text
    assert 'task53_forward_capture.py' in text
    assert 'trap cleanup EXIT' in text
    assert 'writers_may_be_stopped' in text
    assert 'unifiedcollector_spiderfoot' in text
    assert 'resume_containers=(unifiedcollector_wa_bridge_1 unifiedcollector_wa_bridge_2 unifiedcollector_collector_whatsapp unifiedcollector_collector_paypal unifiedcollector_recon_egress_guard unifiedcollector_spiderfoot)' in text
    assert 'docker start "$container"' in text
    assert 'pg_dump --format=custom --compress=0' in text
    assert 'pg_restore --list /task-53-forward.dump' in text
    assert 'complete_task53_forward_capture.sh' in text
    assert 'refusing to overwrite' in text


def test_post_capture_guard_requires_a_complete_point_and_restored_writers() -> None:
    text = Path("scripts/recovery/run_task53_post_capture.sh").read_text(encoding="utf-8")

    assert 'set -Eeuo pipefail' in text
    assert 'task-53-production-forward-capture-complete' in text
    assert 'writer is not restored' in text
    assert 'build_task53_media_coverage.py' in text
    assert 'rehearse_task53_forward.sh" --run' in text
    assert 'fresh Task 53 coverage artifacts already exist' in text


def test_post_capture_watcher_waits_for_the_exact_capture_unit() -> None:
    text = Path("scripts/recovery/wait_for_task53_capture.sh").read_text(encoding="utf-8")

    assert 'set -Eeuo pipefail' in text
    assert 'unit=uc-task53-recapture.service' in text
    assert 'systemctl is-active --quiet "$unit"' in text
    assert 'sleep 60' in text
    assert 'run_task53_post_capture.sh" --run' in text
