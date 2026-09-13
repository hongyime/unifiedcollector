from contextlib import asynccontextmanager

import pytest

from src.db import migrate


def test_real_migrations_create_rollup_function_before_narrowing_its_trigger():
    names = [path.name for path in migrate._ordered_migrations(migrate.MIGRATIONS_DIR)]
    assert names.index("add_media_source_rollups.sql") < names.index("20260802_limit_media_rollup_trigger_updates.sql")
    assert names.index("add_telegram_is_bot.sql") < names.index("20260906_backfill_telegram_is_bot_from_username.sql")
    assert set(names) == {path.name for path in migrate.MIGRATIONS_DIR.glob("*.sql")}


def test_dependencies_are_transitive_and_unrelated_migrations_keep_lexical_order(tmp_path):
    for name in ["01.sql", "02.sql", "03.sql", "04.sql", "05.sql"]:
        (tmp_path / name).write_text("select 1;")
    ordered = migrate._ordered_migrations(tmp_path, {"02.sql": ("04.sql",), "04.sql": ("05.sql",)})
    assert [path.name for path in ordered] == ["01.sql", "05.sql", "04.sql", "02.sql", "03.sql"]


@pytest.mark.parametrize("dependencies,match", [
    ({"01.sql": ("absent.sql",)}, "unavailable prerequisite absent.sql"),
    ({"01.sql": ("01.sql",)}, "dependency cycle"),
    ({"01.sql": ("02.sql",), "02.sql": ("01.sql",)}, "dependency cycle"),
    ({"01.sql": ("drop_wa_face_tables.sql",)}, "unavailable prerequisite drop_wa_face_tables.sql"),
])
def test_invalid_dependencies_fail_before_database_access(tmp_path, dependencies, match):
    for name in ["01.sql", "02.sql", "drop_wa_face_tables.sql"]:
        (tmp_path / name).write_text("select 1;")
    with pytest.raises(RuntimeError, match=match):
        migrate._ordered_migrations(tmp_path, dependencies)


def test_unrelated_fixture_migrations_do_not_require_production_prerequisites(tmp_path):
    (tmp_path / "fixture.sql").write_text("select 1;")
    assert [path.name for path in migrate._ordered_migrations(tmp_path)] == ["fixture.sql"]


class _LockedConn:
    async def fetchval(self, *_args, **_kwargs):
        return False


class _LockedPool:
    @asynccontextmanager
    async def acquire(self):
        yield _LockedConn()


class _LockErrorConn:
    async def fetchval(self, *_args, **_kwargs):
        raise RuntimeError("db startup race")


class _LockErrorPool:
    @asynccontextmanager
    async def acquire(self):
        yield _LockErrorConn()


@pytest.mark.asyncio
async def test_apply_all_logs_advisory_lock_miss_as_info(caplog):
    with caplog.at_level("INFO", logger="src.db.migrate"):
        summary = await migrate.apply_all(_LockedPool())

    assert summary["deferred"] is True
    assert any(
        record.levelname == "INFO"
        and "another instance is currently migrating" in record.message
        for record in caplog.records
    )
    assert not any(
        record.levelname == "WARNING"
        and "another instance is currently migrating" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_apply_all_defers_when_advisory_lock_check_fails(caplog):
    with caplog.at_level("WARNING", logger="src.db.migrate"):
        summary = await migrate.apply_all(_LockErrorPool())

    assert summary["deferred"] is True
    assert any(
        record.levelname == "WARNING"
        and "advisory lock check failed" in record.message
        for record in caplog.records
    )
