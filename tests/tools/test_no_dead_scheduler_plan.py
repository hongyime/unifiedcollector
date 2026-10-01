"""The deleted scheduler plan must not be cited as if it still exists."""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_DEAD = "docs/plans/scheduler-refactor.md"


def test_deleted_scheduler_plan_is_not_cited() -> None:
    hits: list[str] = []
    for folder in ("src", "tests"):
        for path in (_ROOT / folder).rglob("*.py"):
            if path.resolve() == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8")
            if _DEAD in text:
                hits.append(str(path.relative_to(_ROOT)))
    assert hits == []
    assert not (_ROOT / _DEAD).exists()
