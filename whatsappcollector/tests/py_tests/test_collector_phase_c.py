import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


COLLECTOR_ROOT = Path(__file__).resolve().parents[2] / "services" / "collector"
if str(COLLECTOR_ROOT) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_ROOT))


def test_collector_settings_defaults():
    from collector.config import Settings, settings

    assert Settings.model_fields["BROKER_TYPE"].default == "rabbitmq"
    assert settings.MAX_PAYLOAD_BYTES == 10 * 1024 * 1024
    assert settings.COLLECTOR_BACKFILL_REQ_PER_MIN >= 1


def test_collector_import_contract():
    from collector.config import settings

    assert settings.SERVICE_NAME == "collector"


def test_session_risk_score_logged_out_penalty():
    from collector.session_health import calculate_risk_score

    created_at = datetime.now(timezone.utc) - timedelta(days=1)
    events = [
        {"event_type": "connecting", "detail": "retry"},
        {"event_type": "disconnected", "detail": "logged_out"},
    ]

    score = calculate_risk_score(created_at, events)
    assert score >= 75.0


def test_session_risk_score_caps_at_100():
    from collector.session_health import calculate_risk_score

    created_at = datetime.now(timezone.utc) - timedelta(days=1)
    events = [{"event_type": "connecting", "detail": "retry"} for _ in range(30)]

    score = calculate_risk_score(created_at, events)
    assert score == 100.0
