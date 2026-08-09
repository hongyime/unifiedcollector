from src.worker import WorkerService


def test_instagram_idle_seconds_alias_controls_cycle_sleep(monkeypatch):
    monkeypatch.delenv("COLLECTOR_CYCLE_SLEEP_INSTAGRAM", raising=False)
    monkeypatch.delenv("COLLECTOR_CYCLE_SLEEP_SECONDS", raising=False)
    monkeypatch.setenv("INSTAGRAM_IDLE_SECONDS", "1800")

    assert WorkerService()._cycle_sleep("instagram") == 1800.0


def test_instagram_cycle_sleep_defaults_to_thirty_minutes(monkeypatch):
    monkeypatch.delenv("COLLECTOR_CYCLE_SLEEP_INSTAGRAM", raising=False)
    monkeypatch.delenv("INSTAGRAM_IDLE_SECONDS", raising=False)
    monkeypatch.delenv("COLLECTOR_CYCLE_SLEEP_SECONDS", raising=False)

    assert WorkerService()._cycle_sleep("instagram") == 1800.0


def test_source_cycle_sleep_overrides_instagram_idle_alias(monkeypatch):
    monkeypatch.setenv("COLLECTOR_CYCLE_SLEEP_INSTAGRAM", "600")
    monkeypatch.setenv("INSTAGRAM_IDLE_SECONDS", "1800")

    assert WorkerService()._cycle_sleep("instagram") == 600.0
