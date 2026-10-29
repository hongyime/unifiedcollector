#!/usr/bin/env python3
"""Fail-closed Task 48 promotion of the selected Branch-B PostgreSQL restore.

This tool never starts the rejected original volume.  ``--check`` is
read-only; ``--run`` is deliberately unavailable until Task 14, 44, and 47
publish their independently verified receipts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.recovery.apply_task12_media_deletion import DeletionError, apply as apply_task12_media_deletion, verify_proposal


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / ".omo/evidence/collector-production-recovery"
CANDIDATE = Path("/var/lib/unifiedcollector/recovery-candidates/20261023T042116Z_preserved-pg16.dump")
CANDIDATE_SHA256 = "7040e6cc7f73106fc2d138bbf94ba5179845ea90957896f688b38a2d22bad7e6"
# A logical pg_restore necessarily initializes a new PostgreSQL cluster.  This
# identifies the preserved source, not the newly restored target cluster.
SOURCE_SYSTEM_IDENTIFIER = "7695375347612565543"
ORIGINAL_VOLUME = "unifiedcollector_pgdata"
RECOVERED_VOLUME = "unifiedcollector_pgdata_recovered"
ORIGINAL_CONTAINER = "unifiedcollector_postgres"
RESTORE_CONTAINER = "uc-branch-b-restore"
OUTPUT = EVIDENCE / "task-48-forward-authority.json"


class PromotionError(RuntimeError):
    """Promotion is not safe to continue."""


def run(*command: str, input_text: str | None = None) -> str:
    completed = subprocess.run(command, text=True, input=input_text, capture_output=True, check=False)
    if completed.returncode:
        message = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise PromotionError(f"{command[0]} {command[1] if len(command) > 1 else ''}: {message}")
    return completed.stdout


def load_json(name: str) -> dict[str, Any]:
    path = EVIDENCE / name
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromotionError(f"required receipt is unavailable or malformed: {name}") from exc
    if not isinstance(document, dict):
        raise PromotionError(f"required receipt is not an object: {name}")
    return document


def docker_inspect(name: str) -> dict[str, Any] | None:
    completed = subprocess.run(["docker", "inspect", name], text=True, capture_output=True, check=False)
    if completed.returncode:
        return None
    result = json.loads(completed.stdout)
    return result[0] if isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict) else None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_prerequisites() -> None:
    decision = (EVIDENCE / "task-12-authority-decision.md").read_text(encoding="utf-8")
    if "Decision: **Branch B" not in decision or CANDIDATE_SHA256 not in decision:
        raise PromotionError("Task 12 does not select the expected Branch-B candidate")
    rehearsal = load_json("task-14-collector-production-recovery.json")
    if not (
        rehearsal.get("status") == "passed"
        and rehearsal.get("branch") == "recovered"
        and rehearsal.get("candidate_sha256") == CANDIDATE_SHA256
        and rehearsal.get("rehearsal_resources_removed") is True
    ):
        raise PromotionError("Task 14 receipt does not prove a cleaned Branch-B rehearsal")
    cutover = load_json("task-47-collector-production-recovery.json")
    mount = cutover.get("mount")
    if not (
        cutover.get("selected_branch") == "recovered"
        and cutover.get("expected_pgdata") == RECOVERED_VOLUME
        and cutover.get("rendered_bind_root") == "/media/sg-ultra/unifiedcollector"
        and isinstance(mount, dict)
        and mount.get("uuid") == "2C30-310F"
        and mount.get("fstype") == "exfat"
        and cutover.get("production_postgres_running") is False
    ):
        raise PromotionError("Task 47 receipt does not prove the selected Branch-B bind")
    artifact = load_json("task-44-artifact-manifest.json")
    if artifact.get("schema_version") != 1:
        raise PromotionError("Task 44 artifact manifest has an unsupported schema")
    if not CANDIDATE.is_file() or sha256(CANDIDATE) != CANDIDATE_SHA256:
        raise PromotionError("the selected restore candidate is missing or hash-mismatched")
    try:
        verify_proposal()
    except DeletionError as exc:
        raise PromotionError(f"Task 12 media deletion proposal is unsafe: {exc}") from exc


def render_guard() -> None:
    run(
        str(ROOT / "scripts/recovery/compose_branch.sh"),
        "--namespace", "production", "--branch", "recovered",
        "--expect-volume", RECOVERED_VOLUME, "--", "config", "--quiet",
    )


def check() -> None:
    require_prerequisites()
    original = docker_inspect(ORIGINAL_CONTAINER)
    if original is not None and original.get("State", {}).get("Running"):
        raise PromotionError("refusing Task 48 while the rejected original PostgreSQL container is running")
    if docker_inspect(RESTORE_CONTAINER) is not None:
        raise PromotionError("refusing to reuse a prior Branch-B restore container")
    if docker_inspect(RECOVERED_VOLUME) is not None:
        raise PromotionError("refusing to reuse a pre-existing recovered pgdata volume")
    if docker_inspect(ORIGINAL_VOLUME) is None:
        raise PromotionError("the preserved original pgdata volume is unavailable")
    render_guard()


def wait_ready(container: str) -> None:
    for _ in range(120):
        probe = subprocess.run(
            ["docker", "exec", "-u", "postgres", container, "pg_isready", "-U", "postgres", "-d", "unifiedcollector"],
            text=True, capture_output=True, check=False,
        )
        if probe.returncode == 0:
            return
        time.sleep(1)
    raise PromotionError(f"{container} did not become ready")


def restore_volume() -> None:
    run("docker", "volume", "create", RECOVERED_VOLUME)
    run(
        "docker", "run", "-d", "--pull=never", "--network", "none", "--restart", "no",
        "--name", RESTORE_CONTAINER, "-e", "POSTGRES_DB=unifiedcollector",
        "-e", "POSTGRES_USER=postgres", "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
        "-v", f"{RECOVERED_VOLUME}:/var/lib/postgresql/data",
        "-v", f"{CANDIDATE}:/candidate.dump:ro", "pgvector/pgvector:pg16",
    )
    wait_ready(RESTORE_CONTAINER)
    run(
        "docker", "exec", "-u", "postgres", RESTORE_CONTAINER, "sh", "-c",
        "exec /usr/lib/postgresql/16/bin/pg_restore --exit-on-error --no-owner --no-privileges "
        "--host=/var/run/postgresql --username=postgres --dbname=unifiedcollector /candidate.dump",
    )
    run("docker", "stop", RESTORE_CONTAINER)
    run("docker", "rm", RESTORE_CONTAINER)


def verify_forward_container() -> dict[str, Any]:
    inspected = docker_inspect(ORIGINAL_CONTAINER)
    if inspected is None:
        raise PromotionError("recovered PostgreSQL container was not created")
    mounts = inspected.get("Mounts", [])
    pgdata = [mount for mount in mounts if mount.get("Destination") == "/var/lib/postgresql/data"]
    if len(pgdata) != 1 or pgdata[0].get("Name") != RECOVERED_VOLUME:
        raise PromotionError("created PostgreSQL container does not mount the recovered pgdata volume")
    if any(mount.get("Name") == ORIGINAL_VOLUME for mount in mounts):
        raise PromotionError("created PostgreSQL container mounts the rejected original volume")
    return inspected


def bootstrap_collector_login() -> None:
    """Enable only the dump's required non-superuser runtime role.

    The target-native dump deliberately contains no cluster roles.  Its Task-9
    restore therefore created ``collector NOLOGIN`` only to satisfy object
    references.  The Compose schema initializer authenticates as that role, so
    its already-configured password must be applied inside the recovered
    container before the one-shot initializer runs.  The password is expanded
    only inside that container and is never a host command argument or receipt
    field.
    """
    run(
        "docker", "exec", "-u", "postgres", ORIGINAL_CONTAINER, "psql", "-U", "postgres",
        "-d", "unifiedcollector", "-v", "ON_ERROR_STOP=1", "-c",
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'collector') THEN "
        "CREATE ROLE collector LOGIN; "
        "ELSE ALTER ROLE collector LOGIN; "
        "END IF; END $$;",
    )
    run(
        "docker", "exec", "-u", "postgres", ORIGINAL_CONTAINER, "psql", "-U", "postgres",
        "-d", "unifiedcollector", "-v", "ON_ERROR_STOP=1", "-c",
        "DO $$ DECLARE item record; BEGIN "
        "FOR item IN SELECT c.oid::regclass AS name, CASE c.relkind "
        "WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' "
        "WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END AS kind "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relowner='postgres'::regrole "
        "AND c.relkind IN ('r','p','v','m','f') LOOP "
        "EXECUTE format('ALTER %s %s OWNER TO collector', item.kind, item.name); END LOOP; "
        "ALTER SCHEMA public OWNER TO collector; "
        "GRANT USAGE, CREATE ON SCHEMA public TO collector; END $$;",
    )
    run(
        "docker", "exec", "-u", "postgres", ORIGINAL_CONTAINER, "sh", "-ceu",
        r"""
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be configured}"
printf '%s\n%s\n' "$POSTGRES_PASSWORD" "$POSTGRES_PASSWORD" |
  psql -U postgres -d unifiedcollector -v ON_ERROR_STOP=1 -c '\password collector'
""",
    )


def existing_deletion() -> dict[str, Any] | None:
    """Return only a receipt that proves the exact approved deletion committed."""
    path = EVIDENCE / "task-48-media-deletion-receipt.json"
    if not path.exists():
        return None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromotionError("existing Task 12 deletion receipt is malformed") from exc
    _, count, candidate_sha = verify_proposal()
    if not (
        receipt.get("task") == 12
        and receipt.get("deleted_media_items") == count
        and receipt.get("remaining_media_items") == 1064772 - count
        and receipt.get("candidate_tsv_sha256") == candidate_sha
    ):
        raise PromotionError("existing Task 12 deletion receipt is not bound to the approved proposal")
    return receipt


def forward_identity(expected_media_items: int) -> dict[str, Any]:
    wait_ready(ORIGINAL_CONTAINER)
    control = run("docker", "exec", "-u", "postgres", ORIGINAL_CONTAINER, "/usr/lib/postgresql/16/bin/pg_controldata", "/var/lib/postgresql/data")
    marker = "Database system identifier:"
    line = next((item for item in control.splitlines() if item.startswith(marker)), "")
    restored_identifier = line.removeprefix(marker).strip()
    if not restored_identifier.isdigit():
        raise PromotionError("recovered PostgreSQL system identifier is unavailable")
    if restored_identifier == SOURCE_SYSTEM_IDENTIFIER:
        raise PromotionError("recovered logical-restore cluster unexpectedly reuses the source system identifier")
    counts = run(
        "docker", "exec", "-u", "postgres", ORIGINAL_CONTAINER, "psql", "-U", "postgres", "-At", "-d", "unifiedcollector",
        "-v", "ON_ERROR_STOP=1", "-c",
        "SELECT (SELECT count(*) FROM schema_migrations), (SELECT count(*) FROM media_items);",
    ).strip().split("|")
    if counts != ["152", str(expected_media_items)]:
        raise PromotionError(f"recovered database counts do not match Task 12: {counts}")
    return {
        "source_system_identifier": SOURCE_SYSTEM_IDENTIFIER,
        "restored_system_identifier": restored_identifier,
        "schema_migrations": 152,
        "media_items": expected_media_items,
    }


def write_receipt(original: dict[str, Any] | None, recovered: dict[str, Any], identity: dict[str, Any], deletion: dict[str, Any]) -> None:
    document = {
        "schema_version": 1,
        "task": 48,
        "selected_branch": "recovered",
        "forward_authority": "restored-pg16-plus-sg-ultra",
        "candidate_sha256": CANDIDATE_SHA256,
        "original_container": original,
        "original_volume": ORIGINAL_VOLUME,
        "original_volume_preserved": True,
        "recovered_volume": RECOVERED_VOLUME,
        "recovered_container_image": recovered.get("Image"),
        "recovered_mounts": recovered.get("Mounts"),
        "collector_login_bootstrap": "completed inside recovered container; password omitted",
        "identity": identity,
        "task_12_media_deletion": deletion,
        "rollback_route": "quiesce recovered services, capture forward state, then keep recovered authority stopped; never start rejected original",
        "completed_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    temporary = OUTPUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT)


def promote() -> None:
    check()
    original = docker_inspect(ORIGINAL_CONTAINER)
    try:
        # A failed restore can already have removed the stopped rejected
        # container; its volume remains the rollback evidence and is never reused.
        if original is not None:
            run("docker", "rm", ORIGINAL_CONTAINER)
        restore_volume()
        run(
            str(ROOT / "scripts/recovery/compose_branch.sh"), "--namespace", "production",
            "--branch", "recovered", "--expect-volume", RECOVERED_VOLUME,
            "--", "up", "--no-start", "--no-deps", "postgres",
        )
        recovered = verify_forward_container()
        run("docker", "start", ORIGINAL_CONTAINER)
        forward_identity(1064772)
        try:
            deletion = apply_task12_media_deletion(
                ORIGINAL_CONTAINER, EVIDENCE / "task-48-media-deletion-receipt.json"
            )
        except DeletionError as exc:
            raise PromotionError(f"Task 12 media deletion could not be applied: {exc}") from exc
        bootstrap_collector_login()
        identity = forward_identity(1064772 - int(deletion["deleted_media_items"]))
        run(
            str(ROOT / "scripts/recovery/compose_branch.sh"), "--namespace", "production",
            "--branch", "recovered", "--expect-volume", RECOVERED_VOLUME,
            "--", "run", "--rm", "schema_init",
        )
        write_receipt(original, recovered, identity, deletion)
    except Exception:
        subprocess.run(["docker", "rm", "-f", RESTORE_CONTAINER], text=True, capture_output=True, check=False)
        subprocess.run(["docker", "stop", ORIGINAL_CONTAINER], text=True, capture_output=True, check=False)
        raise


def resume() -> None:
    """Finish a failed pre-deletion Branch-B promotion without re-restoring."""
    require_prerequisites()
    render_guard()
    if docker_inspect(RESTORE_CONTAINER) is not None:
        raise PromotionError("refusing to resume while a restore container exists")
    if (EVIDENCE / "task-48-forward-authority.json").exists():
        raise PromotionError("refusing to resume an already-promoted recovered authority")
    recovered = verify_forward_container()
    if recovered.get("State", {}).get("Running"):
        raise PromotionError("refusing to resume while recovered PostgreSQL is running")
    try:
        run("docker", "start", ORIGINAL_CONTAINER)
        deletion = existing_deletion()
        forward_identity(1064772 - int(deletion["deleted_media_items"]) if deletion else 1064772)
        if deletion is None:
            try:
                deletion = apply_task12_media_deletion(
                    ORIGINAL_CONTAINER, EVIDENCE / "task-48-media-deletion-receipt.json"
                )
            except DeletionError as exc:
                raise PromotionError(f"Task 12 media deletion could not be applied: {exc}") from exc
        bootstrap_collector_login()
        identity = forward_identity(1064772 - int(deletion["deleted_media_items"]))
        run(
            str(ROOT / "scripts/recovery/compose_branch.sh"), "--namespace", "production",
            "--branch", "recovered", "--expect-volume", RECOVERED_VOLUME,
            "--", "run", "--rm", "schema_init",
        )
        write_receipt(
            {"status": "removed during the prior failed pre-deletion promotion; original volume remains preserved"},
            recovered, identity, deletion,
        )
    except Exception:
        subprocess.run(["docker", "stop", ORIGINAL_CONTAINER], text=True, capture_output=True, check=False)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="verify prerequisites without Docker mutation")
    mode.add_argument("--run", action="store_true", help="perform the gated Branch-B promotion")
    mode.add_argument("--resume", action="store_true", help="finish an interrupted pre-deletion Branch-B promotion")
    args = parser.parse_args()
    try:
        if args.check:
            check()
            print("PASS: Branch-B promotion prerequisites are complete")
        elif args.run:
            promote()
            print(f"PASS: Branch-B forward authority evidence={OUTPUT}")
        else:
            resume()
            print(f"PASS: Branch-B forward authority evidence={OUTPUT}")
    except PromotionError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
