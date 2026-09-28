from src.worker import WorkerService


def test_startup_delay_defaults_to_index_spread_only(monkeypatch):
    monkeypatch.delenv("COLLECTOR_STARTUP_DELAY_SECONDS", raising=False)

    service = WorkerService()

    assert service._source_startup_delay(0) == 0.0
    assert service._source_startup_delay(1) == 3.0
    assert service._source_startup_delay(2) == 6.0


def test_startup_delay_env_var_adds_a_base_offset(monkeypatch):
    monkeypatch.setenv("COLLECTOR_STARTUP_DELAY_SECONDS", "90")

    service = WorkerService()

    assert service._source_startup_delay(0) == 90.0
    assert service._source_startup_delay(1) == 93.0


def test_startup_delay_ignores_malformed_env_value(monkeypatch):
    monkeypatch.setenv("COLLECTOR_STARTUP_DELAY_SECONDS", "not-a-number")

    service = WorkerService()

    assert service._source_startup_delay(0) == 0.0
    assert service._source_startup_delay(1) == 3.0
