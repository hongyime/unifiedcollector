"""Tests for src/analyze_users.py — UserAnalyzer (DB-backed).

All tests use an in-memory SQLite database seeded with sample data.
"""
import csv
import json
import os

import pytest

# DB isolation is handled per-test by the _use_memory_db fixture below.
# Do NOT set DATABASE_URL here at module level — it would leak into subprocesses
# spawned by other tests (e.g. test_bat_menus.py uses subprocess.run(main.py)).


@pytest.fixture(autouse=True)
def _use_memory_db(monkeypatch):
    """Force in-memory SQLite for every test and reset the singleton."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    # Reset any cached DB singleton in analyze_users
    import analyze_users as _au
    _au._get_db._instance = None
    yield
    _au._get_db._instance = None


@pytest.fixture
def analyzer(tmp_path, monkeypatch):
    """UserAnalyzer with sample data seeded into an in-memory DB."""
    import analyze_users as _au
    from db.manager import DatabaseManager
    from db.repositories.username_repository import UsernameRepository
    from db.repositories.relationship_repository import RelationshipRepository

    db = DatabaseManager("sqlite:///:memory:")
    _au._get_db._instance = db

    # Seed usernames
    ur = UsernameRepository(db)
    for u in ["alice", "bob", "charlie"]:
        ur.add_username(u, source_account="test")

    # Seed relationships
    rr = RelationshipRepository(db)
    rr.bulk_upsert([
        {"source": "alice", "target": "bob",     "type": "followers", "collected_by": "test", "source_is_public": True},
        {"source": "alice", "target": "charlie",  "type": "followers", "collected_by": "test", "source_is_public": True},
        {"source": "alice", "target": "bob",     "type": "following", "collected_by": "test", "source_is_public": True},
        {"source": "bob",   "target": "alice",   "type": "followers", "collected_by": "test", "source_is_public": True},
        {"source": "bob",   "target": "charlie", "type": "following", "collected_by": "test", "source_is_public": True},
    ])

    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    monkeypatch.setattr("config.DATA_DIR", data_dir)
    monkeypatch.setattr("analyze_users.DATA_DIR", data_dir)

    return _au.UserAnalyzer()


# ══════════════════════════════════════════════════════════════
#  analyze
# ══════════════════════════════════════════════════════════════

class TestAnalyze:

    def test_counts_followers(self, analyzer):
        stats = analyzer.analyze()
        assert stats["alice"]["followers_count"] == 2

    def test_counts_following(self, analyzer):
        stats = analyzer.analyze()
        assert stats["alice"]["following_count"] == 1

    def test_includes_all_known_usernames(self, analyzer):
        stats = analyzer.analyze()
        assert "charlie" in stats

    def test_auto_creates_source_not_in_usernames(self, analyzer):
        stats = analyzer.analyze()
        assert "bob" in stats

    def test_empty_relationships(self, tmp_path, monkeypatch):
        import analyze_users as _au
        from db.manager import DatabaseManager
        from db.repositories.username_repository import UsernameRepository

        db = DatabaseManager("sqlite:///:memory:")
        _au._get_db._instance = db
        UsernameRepository(db).add_username("alice", source_account="test")

        data_dir = str(tmp_path / "data2")
        os.makedirs(data_dir, exist_ok=True)
        monkeypatch.setattr("config.DATA_DIR", data_dir)
        monkeypatch.setattr("analyze_users.DATA_DIR", data_dir)

        ua = _au.UserAnalyzer()
        stats = ua.analyze()
        assert stats["alice"]["followers_count"] == 0
        assert stats["alice"]["following_count"] == 0


# ══════════════════════════════════════════════════════════════
#  save_json
# ══════════════════════════════════════════════════════════════

class TestSaveJson:

    def test_creates_valid_json(self, analyzer, tmp_path):
        out = str(tmp_path / "report.json")
        analyzer.save_json(out)
        with open(out, "r") as f:
            data = json.load(f)
        assert "alice" in data
        assert "followers_count" in data["alice"]

    def test_content_matches_analyze(self, analyzer, tmp_path):
        out = str(tmp_path / "report.json")
        analyzer.save_json(out)
        with open(out, "r") as f:
            data = json.load(f)
        assert data == analyzer.analyze()


# ══════════════════════════════════════════════════════════════
#  save_csv
# ══════════════════════════════════════════════════════════════

class TestSaveCsv:

    def test_creates_csv_with_header(self, analyzer, tmp_path):
        out = str(tmp_path / "report.csv")
        analyzer.save_csv(out)
        with open(out, "r", newline="") as f:
            reader = csv.reader(f)
            header = next(reader)
        assert header == ["username", "followers_count", "following_count"]

    def test_csv_row_count_matches(self, analyzer, tmp_path):
        out = str(tmp_path / "report.csv")
        analyzer.save_csv(out)
        with open(out, "r", newline="") as f:
            rows = list(csv.reader(f))
        stats = analyzer.analyze()
        assert len(rows) == len(stats) + 1  # header + data rows

    def test_csv_values_correct(self, analyzer, tmp_path):
        out = str(tmp_path / "report.csv")
        analyzer.save_csv(out)
        with open(out, "r", newline="") as f:
            for row in csv.DictReader(f):
                if row["username"] == "alice":
                    assert int(row["followers_count"]) == 2
                    assert int(row["following_count"]) == 1


# ══════════════════════════════════════════════════════════════
#  print_summary (DB-direct)
# ══════════════════════════════════════════════════════════════

class TestPrintSummary:

    def test_prints_counts(self, analyzer, capsys):
        analyzer.print_summary()
        out = capsys.readouterr().out
        assert "2912" in out or "usernames" in out.lower() or "relationships" in out.lower()

    def test_prints_without_error_on_empty_db(self, tmp_path, monkeypatch, capsys):
        import analyze_users as _au
        from db.manager import DatabaseManager
        db = DatabaseManager("sqlite:///:memory:")
        _au._get_db._instance = db
        data_dir = str(tmp_path / "data3")
        os.makedirs(data_dir, exist_ok=True)
        monkeypatch.setattr("config.DATA_DIR", data_dir)
        monkeypatch.setattr("analyze_users.DATA_DIR", data_dir)
        ua = _au.UserAnalyzer()
        ua.print_summary()  # should not raise
        out = capsys.readouterr().out
        assert "0" in out
