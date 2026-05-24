import importlib
import importlib.util
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module(module_name: str, relative_path: str):
    module_path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _mock_script_location(monkeypatch, module, tmp_path: Path) -> Path:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    mocked_file = scripts_dir / "run_migrations.py"
    mocked_file.write_text("# mock script location\n", encoding="utf-8")
    monkeypatch.setattr(module, "__file__", str(mocked_file))
    return scripts_dir


def test_resolve_migration_plan_defaults_to_bootstrap(monkeypatch, tmp_path):
    run_migrations = _load_module(
        "run_migrations_test_bootstrap",
        "infrastructure/scripts/run_migrations.py",
    )

    _mock_script_location(monkeypatch, run_migrations, tmp_path)
    bootstrap_sql = tmp_path / "init-db.sql"
    bootstrap_sql.write_text("SELECT 1;\n", encoding="utf-8")

    monkeypatch.delenv("MIGRATIONS_DIR", raising=False)

    plan = run_migrations.resolve_migration_plan()

    assert plan.mode == "bootstrap"
    assert [p.name for p in plan.files] == ["init-db.sql"]


def test_resolve_migration_plan_uses_configured_migrations_dir(monkeypatch, tmp_path):
    run_migrations = _load_module(
        "run_migrations_test_migrations",
        "infrastructure/scripts/run_migrations.py",
    )

    _mock_script_location(monkeypatch, run_migrations, tmp_path)

    migrations_dir = tmp_path / "custom_migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    (migrations_dir / "002_second.sql").write_text("SELECT 2;\n", encoding="utf-8")
    (migrations_dir / "001_first.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (migrations_dir / "README.txt").write_text("ignore\n", encoding="utf-8")

    monkeypatch.setenv("MIGRATIONS_DIR", str(migrations_dir))

    plan = run_migrations.resolve_migration_plan()

    assert plan.mode == "migrations"
    assert [p.name for p in plan.files] == ["001_first.sql", "002_second.sql"]


def test_resolve_migration_plan_noop_when_no_sources(monkeypatch, tmp_path):
    run_migrations = _load_module(
        "run_migrations_test_noop",
        "infrastructure/scripts/run_migrations.py",
    )

    _mock_script_location(monkeypatch, run_migrations, tmp_path)
    monkeypatch.delenv("MIGRATIONS_DIR", raising=False)

    plan = run_migrations.resolve_migration_plan()

    assert plan.mode == "noop"
    assert plan.files == []


def test_clear_queues_normalize_queue_name():
    clear_queues = _load_module(
        "clear_queues_test",
        "infrastructure/scripts/clear_queues.py",
    )

    assert clear_queues.normalize_queue_name("dead_letter_queue") == clear_queues.CANONICAL_DLQ_QUEUE
    assert clear_queues.normalize_queue_name(clear_queues.CANONICAL_DLQ_QUEUE) == clear_queues.CANONICAL_DLQ_QUEUE


def test_manage_dlq_candidates_default_and_legacy(monkeypatch):
    monkeypatch.delenv("DLQ_NAME", raising=False)
    manage_dlq_default = _load_module(
        "manage_dlq_test_default",
        "infrastructure/scripts/manage_dlq.py",
    )

    assert manage_dlq_default._dlq_candidates() == ["dlq.failed", "dead_letter_queue"]

    monkeypatch.setenv("DLQ_NAME", "dead_letter_queue")
    manage_dlq_legacy = _load_module(
        "manage_dlq_test_legacy",
        "infrastructure/scripts/manage_dlq.py",
    )

    assert manage_dlq_legacy._dlq_candidates() == ["dead_letter_queue", "dlq.failed"]
