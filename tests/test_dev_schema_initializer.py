"""Development migration failure paths; no database, settings or worker startup."""
import asyncio
import importlib.util
from pathlib import Path
import types
import unittest
from unittest import mock


class DevSchemaTests(unittest.TestCase):
    def run_initializer(self, result=None, error=None):
        pool = object()
        connection = types.ModuleType("src.db.connection")
        connection.get_pool = mock.AsyncMock(return_value=pool)
        connection.close_pool = mock.AsyncMock()
        migrate = types.ModuleType("src.db.migrate")
        migrate.apply_all = mock.AsyncMock(return_value=result, side_effect=error)
        path = Path(__file__).resolve().parents[1] / "src/db/dev_init.py"
        spec = importlib.util.spec_from_file_location("dev_schema_fixture", path)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict("sys.modules", {"src.db.connection": connection, "src.db.migrate": migrate}):
            spec.loader.exec_module(module)
            try:
                asyncio.run(module.initialize())
            finally:
                migrate.apply_all.assert_awaited_once_with(pool)
                connection.close_pool.assert_awaited_once_with()

    def test_completed_schema_succeeds_and_closes_connection(self):
        self.run_initializer({"schemas": 3, "deferred": False})

    def test_deferred_or_empty_schema_fails_and_closes_connection(self):
        for result in ({"schemas": 3, "deferred": True}, {"schemas": 0}):
            with self.subTest(result=result), self.assertRaisesRegex(RuntimeError, "did not complete"):
                self.run_initializer(result)

    def test_migration_error_is_preserved_and_connection_closed(self):
        with self.assertRaisesRegex(ValueError, "fixture migration failure"):
            self.run_initializer(error=ValueError("fixture migration failure"))


if __name__ == "__main__":
    unittest.main()
