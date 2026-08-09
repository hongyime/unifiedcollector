from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_collector_startup_restarts_telegram_after_session_sync():
    script = (REPO_ROOT / "scripts" / "register-collector-startup.ps1").read_text(encoding="utf-8")

    assert "UnifiedCollector_Startup.bat" in script
    assert "docker compose -f %COMPOSE% up -d" in script
    assert "set SESSION_TARGET=unifiedcollector_collector_telegram" in script
    assert "docker cp" in script
    assert "docker restart unifiedcollector_collector_telegram" in script
    assert script.index("docker cp") < script.index("docker restart unifiedcollector_collector_telegram")
