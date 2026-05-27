import os
import asyncio
from dataclasses import dataclass
from pathlib import Path

import asyncpg


@dataclass(frozen=True)
class MigrationPlan:
    mode: str
    files: list[Path]


def _resolve_sql_files(directory: Path) -> list[Path]:
    if not directory.exists() or not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if path.suffix.lower() == ".sql")


def resolve_migration_plan() -> MigrationPlan:
    base_dir = Path(__file__).resolve().parent
    explicit_dir = os.environ.get("MIGRATIONS_DIR")

    if explicit_dir:
        explicit_path = Path(explicit_dir).expanduser()
        if not explicit_path.is_absolute():
            explicit_path = explicit_path.resolve()

        explicit_files = _resolve_sql_files(explicit_path)
        if explicit_files:
            return MigrationPlan(mode="migrations", files=explicit_files)

        print(f"Configured MIGRATIONS_DIR has no SQL files: {explicit_path}")

    default_dir = (base_dir / ".." / "migrations").resolve()
    default_files = _resolve_sql_files(default_dir)
    if default_files:
        return MigrationPlan(mode="migrations", files=default_files)

    bootstrap_sql = (base_dir / ".." / "init-db.sql").resolve()
    if bootstrap_sql.exists():
        return MigrationPlan(mode="bootstrap", files=[bootstrap_sql])

    return MigrationPlan(mode="noop", files=[])


def _version_for_file(file_path: Path, mode: str) -> str:
    if mode == "bootstrap":
        return f"bootstrap:{file_path.name}"

    stem = file_path.stem
    if "_" in stem:
        return stem.split("_", 1)[0]
    return stem

async def run_migrations():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise ValueError(
            "DATABASE_URL environment variable is required. "
            "Set it to a valid asyncpg DSN, e.g. "
            "postgresql://user:password@host:5432/dbname"
        )
    print(f"Connecting to database...")
    conn = await asyncpg.connect(db_url)

    try:
        # Create schema_version table if it doesn't exist
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS schema_version (
                version VARCHAR(255) PRIMARY KEY,
                applied_at TIMESTAMPTZ DEFAULT NOW()
            );
        ''')
        print("Checked schema_version table.")

        plan = resolve_migration_plan()
        print(f"Migration mode: {plan.mode}")

        if plan.mode == "noop":
            print("No migration SQL files found and no bootstrap SQL available. Exiting without changes.")
            return

        for migration_file in plan.files:
            version = _version_for_file(migration_file, plan.mode)

            # Check if applied
            applied = await conn.fetchval('SELECT version FROM schema_version WHERE version = $1', version)
            if applied:
                print(f"Skipping {migration_file.name} - already applied.")
                continue

            print(f"Applying {migration_file.name}...")
            sql = migration_file.read_text(encoding="utf-8")

            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute('INSERT INTO schema_version (version) VALUES ($1)', version)

            print(f"Successfully applied {migration_file.name}.")

        print("All migrations applied successfully.")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(run_migrations())
