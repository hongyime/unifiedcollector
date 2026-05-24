"""
Tests for processing_metrics table in init-db.sql - Task P1.4

Validates: Bug F-006 fix - processing_metrics table exists in init-db.sql
with correct schema and indexes.

Fix Checking Property (F-006):
  FOR ALL X WHERE isBugCondition(X) DO
    result <- DatabaseSchema'(X)
    ASSERT HAS_TABLE(result, 'processing_metrics')
  END FOR

Preservation Checking Property (F-006):
  FOR ALL X WHERE NOT isBugCondition(X) DO
    ASSERT existing_tables(X) == existing_tables'(X)
  END FOR
"""

import re
import unittest


def _load_init_sql() -> str:
    with open("init-db.sql", "r") as f:
        return f.read()


class TestProcessingMetricsTableExists(unittest.TestCase):
    """Fix Checking: processing_metrics table must be defined in init-db.sql."""

    def setUp(self):
        self.sql = _load_init_sql()

    def test_table_definition_present(self):
        """Validates: Requirements 2.6 - processing_metrics table exists."""
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS processing_metrics",
            self.sql,
            "processing_metrics table must be defined in init-db.sql"
        )

    def test_id_column_present(self):
        """Table must have id SERIAL PRIMARY KEY column."""
        # Extract the table block
        match = re.search(
            r"CREATE TABLE IF NOT EXISTS processing_metrics\s*\((.+?)\);",
            self.sql,
            re.DOTALL | re.IGNORECASE,
        )
        self.assertIsNotNone(match, "Could not find processing_metrics table definition")
        table_body = match.group(1)
        self.assertRegex(
            table_body,
            r"id\s+SERIAL\s+PRIMARY\s+KEY",
            "processing_metrics must have id SERIAL PRIMARY KEY"
        )

    def test_metric_name_column_present(self):
        """Table must have metric_name VARCHAR(100) NOT NULL column."""
        match = re.search(
            r"CREATE TABLE IF NOT EXISTS processing_metrics\s*\((.+?)\);",
            self.sql,
            re.DOTALL | re.IGNORECASE,
        )
        self.assertIsNotNone(match)
        table_body = match.group(1)
        self.assertRegex(
            table_body,
            r"metric_name\s+VARCHAR\(100\)\s+NOT\s+NULL",
            "processing_metrics must have metric_name VARCHAR(100) NOT NULL"
        )

    def test_metric_value_column_present(self):
        """Table must have metric_value REAL NOT NULL column."""
        match = re.search(
            r"CREATE TABLE IF NOT EXISTS processing_metrics\s*\((.+?)\);",
            self.sql,
            re.DOTALL | re.IGNORECASE,
        )
        self.assertIsNotNone(match)
        table_body = match.group(1)
        self.assertRegex(
            table_body,
            r"metric_value\s+REAL\s+NOT\s+NULL",
            "processing_metrics must have metric_value REAL NOT NULL"
        )

    def test_recorded_at_column_present(self):
        """Table must have recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP column."""
        match = re.search(
            r"CREATE TABLE IF NOT EXISTS processing_metrics\s*\((.+?)\);",
            self.sql,
            re.DOTALL | re.IGNORECASE,
        )
        self.assertIsNotNone(match)
        table_body = match.group(1)
        self.assertRegex(
            table_body,
            r"recorded_at\s+TIMESTAMP\s+DEFAULT\s+CURRENT_TIMESTAMP",
            "processing_metrics must have recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
        )


class TestProcessingMetricsIndexes(unittest.TestCase):
    """Fix Checking: required indexes must exist for processing_metrics."""

    def setUp(self):
        self.sql = _load_init_sql()

    def test_metric_name_index_exists(self):
        """Index on metric_name must be defined."""
        self.assertRegex(
            self.sql,
            r"CREATE INDEX IF NOT EXISTS idx_metrics_name ON processing_metrics\(metric_name\)",
            "idx_metrics_name index on processing_metrics(metric_name) must exist"
        )

    def test_recorded_at_desc_index_exists(self):
        """Index on recorded_at DESC must be defined."""
        self.assertRegex(
            self.sql,
            r"CREATE INDEX IF NOT EXISTS idx_metrics_time ON processing_metrics\(recorded_at DESC\)",
            "idx_metrics_time index on processing_metrics(recorded_at DESC) must exist"
        )


class TestExistingTablesPreserved(unittest.TestCase):
    """Preservation Checking: existing tables must still be present in init-db.sql."""

    def setUp(self):
        self.sql = _load_init_sql()

    def test_telegram_accounts_preserved(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS telegram_accounts", self.sql)

    def test_telegram_topics_preserved(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS telegram_topics", self.sql)

    def test_face_embeddings_preserved(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS face_embeddings", self.sql)

    def test_scan_checkpoints_preserved(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS scan_checkpoints", self.sql)

    def test_health_checks_preserved(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS health_checks", self.sql)

    def test_processing_errors_preserved(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS processing_errors", self.sql)


if __name__ == "__main__":
    unittest.main()
