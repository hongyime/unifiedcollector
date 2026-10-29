from pathlib import Path
import subprocess
import sys

import pytest

from src.core.media_reconciliation import ManifestEntry, MediaRow, build_reconciliation_plan, canonical_media_path


def _entry(path: Path, size: int, digest: str = "a" * 64) -> ManifestEntry:
    return ManifestEntry(path=path, file_size=size, sha256=digest)


def _row(media_id: str, path: str, size: int = 3, digest: str = "a" * 64) -> MediaRow:
    return MediaRow(media_id=media_id, file_path=path, file_size=size, sha256=digest)


def test_both_established_container_shapes_map_to_the_same_media_root(tmp_path: Path) -> None:
    root = tmp_path / "collector"
    expected = root / "media" / "source" / "item.jpg"
    assert canonical_media_path("/media/source/item.jpg", root) == expected
    assert canonical_media_path("/vault/media/source/item.jpg", root) == expected


def test_inventory_marks_only_missing_or_mismatched_rows_as_deletion_candidates(tmp_path: Path) -> None:
    root = tmp_path / "collector"
    matching = root / "media" / "source" / "match.jpg"
    wrong = root / "media" / "source" / "wrong.jpg"
    orphan = root / "media" / "raw" / "capture.bin"
    plan = build_reconciliation_plan(
        [
            _row("match", "/media/source/match.jpg"),
            _row("missing", "/vault/media/source/missing.jpg"),
            _row("wrong", "/media/source/wrong.jpg"),
        ],
        [_entry(matching, 3), _entry(wrong, 3, "b" * 64), _entry(orphan, 7)],
        root,
    )
    assert plan.matched == ("match",)
    assert plan.delete_row_candidates == (
        {"media_id": "missing", "reason": "missing_canonical_path"},
        {"media_id": "wrong", "reason": "sha256_mismatch"},
    )
    assert plan.unreferenced_files == (str(orphan),)


def test_inventory_preserves_ambiguous_or_outside_rows_for_manual_review(tmp_path: Path) -> None:
    root = tmp_path / "collector"
    same = root / "media" / "source" / "same.jpg"
    plan = build_reconciliation_plan(
        [_row("first", "/media/source/same.jpg"), _row("second", "/vault/media/source/same.jpg"), _row("outside", "/etc/shadow")],
        [_entry(same, 3)],
        root,
    )
    assert plan.matched == ("first",)
    assert {entry["media_id"] for entry in plan.unresolved_rows} == {"second", "outside"}
    assert not plan.delete_row_candidates


def test_path_traversal_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes"):
        canonical_media_path("/media/../../outside", tmp_path / "collector")


def test_planner_cli_runs_directly_from_the_repository_root() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/plan_media_reconciliation.py", "--help"],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--destination-manifest" in result.stdout
