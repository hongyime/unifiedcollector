from __future__ import annotations

from datetime import datetime, timezone

from .config import settings
from .database import database
from .observability import get_logger

logger = get_logger(__name__)


def calculate_risk_score(session_created_at: datetime, events: list[dict | object]) -> float:
    now = datetime.utcnow()
    # Ensure session_created_at is naive for subtraction
    created = session_created_at.replace(tzinfo=None) if session_created_at.tzinfo else session_created_at
    age_days = max(0.0, (now - created).total_seconds() / 86400.0)
    score = 0.0

    if age_days < 7:
        score += 20.0

    reconnects = 0
    for event in events:
        event_type = getattr(event, "event_type", None) or (event.get("event_type") if isinstance(event, dict) else "")
        detail = getattr(event, "detail", None) or (event.get("detail") if isinstance(event, dict) else "")
        event_type = str(event_type or "").lower()
        detail_s = str(detail or "").lower()

        if event_type in {"connecting", "reconnect", "reconnected"}:
            reconnects += 1
        elif event_type in {"disconnected", "disconnect"}:
            if "logged_out" in detail_s:
                score += 50.0
            elif "bad session file" in detail_s:
                score += 30.0

    score += reconnects * 5.0
    return min(score, 100.0)


class SessionHealthMonitor:
    def __init__(self) -> None:
        self._high_risk_started_at: dict[str, datetime] = {}

    async def run_once(self) -> None:
        sessions = await database.get_active_sessions()
        for session in sessions:
            session_name = session["session_name"]
            events = await database.get_session_events_recent(session_name, settings.SESSION_RISK_WINDOW_SECONDS)
            score = calculate_risk_score(session["created_at"], events)
            threshold = settings.SESSION_RISK_THRESHOLD * 100.0

            logger.info("collector_session_risk", session_name=session_name, score=score)

            if score >= threshold:
                if session_name not in self._high_risk_started_at:
                    self._high_risk_started_at[session_name] = datetime.utcnow()

                elapsed = (datetime.utcnow() - self._high_risk_started_at[session_name]).total_seconds()
                if elapsed >= settings.SESSION_RISK_MINUTES_HIGH * 60:
                    await database.set_session_cooldown(session_name, settings.SESSION_COOLDOWN_SECONDS)
                    logger.warning(
                        "collector_session_auto_cooled_down",
                        session_name=session_name,
                        score=score,
                    )
            else:
                self._high_risk_started_at.pop(session_name, None)


session_health_monitor = SessionHealthMonitor()
