from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ingestion.session import StravaSession, SessionError, _read_cookie_file, _read_cookie_from_env_file, _write_cookie_file


def test_write_cookie_file_round_trips(tmp_path: Path) -> None:
    cookie_path = tmp_path / "cookies.txt"
    _write_cookie_file(str(cookie_path), "fresh-cookie")
    assert _read_cookie_file(str(cookie_path)) == "fresh-cookie"


def test_reauthenticate_uses_cookies_then_playwright_and_updates_cookie_file(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    cookie_path = tmp_path / "cookies.txt"
    cookie_path.write_text(
        "# Netscape HTTP Cookie File\n.strava.com\tTRUE\t/\tTRUE\t2147483647\t_strava4_session\tstale-cookie\n",
        encoding="utf-8",
    )
    settings = SimpleNamespace(
        env_path=env_path,
        request_timeout_seconds=5,
        user_agent="ua",
        debug_http=False,
        auth_recovery_backoff_seconds=1,
        auth_recovery_backoff_cap_seconds=5,
    )
    session = StravaSession(settings, "current-cookie", auth_mode="cookiestxt", auth_fallback="auto", cookies_file=str(cookie_path))

    monkeypatch.setattr("ingestion.session._interactive_playwright_cookie", lambda settings: "fresh-cookie")

    class FakeResponse:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.headers = {"Content-Type": "text/html"}
            self.url = "https://www.strava.com/frontend/athletes/current"

    seen_cookies: list[str] = []

    def fake_send_request(path: str, params=None):
        seen_cookies.append(session.cookie_value)
        if session.cookie_value == "fresh-cookie":
            return FakeResponse(200)
        return FakeResponse(401)

    monkeypatch.setattr(session, "_send_request", fake_send_request)

    session.reauthenticate()

    assert session.cookie_value == "fresh-cookie"
    assert "stale-cookie" in seen_cookies
    assert _read_cookie_file(str(cookie_path)) == "fresh-cookie"
    assert _read_cookie_from_env_file(env_path) == "fresh-cookie"


def test_request_raises_clear_error_when_recovery_fails(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    settings = SimpleNamespace(
        env_path=env_path,
        request_timeout_seconds=5,
        user_agent="ua",
        debug_http=False,
        auth_recovery_backoff_seconds=1,
        auth_recovery_backoff_cap_seconds=5,
    )
    session = StravaSession(settings, "current-cookie", auth_mode="cookiestxt", auth_fallback="none", cookies_file=None)

    class FakeResponse:
        status_code = 401
        headers = {"Content-Type": "text/html"}
        url = "https://www.strava.com/frontend/athletes/current"

    monkeypatch.setattr(session, "_send_request", lambda path, params=None: FakeResponse())

    try:
        session._request("/frontend/athletes/current")
    except SessionError as exc:
        assert "automatic recovery failed" in str(exc) or "Session expired" in str(exc)
    else:
        raise AssertionError("Expected SessionError when auth recovery is unavailable")


def test_validate_retries_http_429_then_succeeds(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    settings = SimpleNamespace(
        env_path=env_path,
        request_timeout_seconds=5,
        user_agent="ua",
        debug_http=False,
        rate_limit_retries=1,
        rate_limit_backoff_seconds=30,
        auth_recovery_backoff_seconds=1,
        auth_recovery_backoff_cap_seconds=5,
    )
    monkeypatch.setattr("ingestion.session.random_delay", lambda *args, **kwargs: None)
    monkeypatch.setattr("ingestion.session.exponential_backoff", lambda *args, **kwargs: 30)
    
    session = StravaSession(settings, "current-cookie", auth_mode="cookiestxt", auth_fallback="none", cookies_file=None)

    class FakeResponse:
        def __init__(self, status_code: int, payload=None):
            self.status_code = status_code
            self.headers = {"Content-Type": "application/json"}
            self.url = "https://www.strava.com/frontend/athletes/current"
            self._payload = payload or {}

        def json(self):
            return self._payload

    responses = [FakeResponse(429), FakeResponse(200, {"currentAthlete": {"id": 123}})]
    sleep_calls: list[int] = []

    monkeypatch.setattr(session.client, "get", lambda *args, **kwargs: responses.pop(0))
    # _interruptible_sleep calls time.sleep(0.2) in a loop; patch it to be a no-op
    # and verify that at least one sleep call was made (backoff was invoked).
    monkeypatch.setattr("ingestion.session._interruptible_sleep", lambda seconds: sleep_calls.append(seconds))

    athlete = session.validate()

    assert athlete["id"] == 123
    # _interruptible_sleep should have been called once with the 30-second backoff
    assert sleep_calls == [30]


def test_validate_raises_clear_error_after_repeated_http_429(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    settings = SimpleNamespace(
        env_path=env_path,
        request_timeout_seconds=5,
        user_agent="ua",
        debug_http=False,
        rate_limit_retries=3,
        rate_limit_backoff_seconds=30,
        auth_recovery_backoff_seconds=1,
        auth_recovery_backoff_cap_seconds=5,
    )
    session = StravaSession(settings, "current-cookie", auth_mode="cookiestxt", auth_fallback="none", cookies_file=None)

    class FakeResponse:
        status_code = 429
        headers = {"Content-Type": "application/json"}
        url = "https://www.strava.com/frontend/athletes/current"

        def json(self):
            return {}

    monkeypatch.setattr(session.client, "get", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr("ingestion.session.time.sleep", lambda seconds: None)

    try:
        session.validate()
    except SessionError as exc:
        assert "likely rate limited" in str(exc)
    else:
        raise AssertionError("Expected SessionError after repeated HTTP 429 responses")


def test_send_request_pauses_and_retries_http_429(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    settings = SimpleNamespace(
        env_path=env_path,
        request_timeout_seconds=5,
        user_agent="ua",
        debug_http=False,
        rate_limit_retries=2,
        rate_limit_backoff_seconds=10,
        auth_recovery_backoff_seconds=1,
        auth_recovery_backoff_cap_seconds=5,
    )
    monkeypatch.setattr("ingestion.session.random_delay", lambda *args, **kwargs: None)
    # Mock exponential backoff to return predictable values
    # attempt 1 -> 20, attempt 2 -> 30
    def mock_backoff(attempt, **kwargs):
        return (attempt + 1) * 10
    monkeypatch.setattr("ingestion.session.exponential_backoff", mock_backoff)
    
    session = StravaSession(settings, "current-cookie", auth_mode="cookiestxt", auth_fallback="none", cookies_file=None)

    class FakeResponse:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.headers = {"Content-Type": "application/json"}
            self.url = "https://www.strava.com/frontend/athletes/current"

        def json(self):
            return {}

    responses = [FakeResponse(429), FakeResponse(429), FakeResponse(200)]
    sleep_calls: list[int] = []

    monkeypatch.setattr(session.client, "get", lambda *args, **kwargs: responses.pop(0))
    # Patch _interruptible_sleep to capture backoff amounts without actually sleeping
    monkeypatch.setattr("ingestion.session._interruptible_sleep", lambda seconds: sleep_calls.append(seconds))

    response = session._send_request("/frontend/athletes/current")

    assert response.status_code == 200
    # _send_request calls exponential_backoff(attempt - 1, ...) so:
    # attempt=1 → mock_backoff(0) = 10, attempt=2 → mock_backoff(1) = 20
    assert sleep_calls == [10, 20]


def test_reauthenticate_applies_backoff_and_resets_after_success(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    cookie_path = tmp_path / "cookies.txt"
    cookie_path.write_text(
        "# Netscape HTTP Cookie File\n.strava.com\tTRUE\t/\tTRUE\t2147483647\t_strava4_session\tfresh-cookie\n",
        encoding="utf-8",
    )
    settings = SimpleNamespace(
        env_path=env_path,
        request_timeout_seconds=5,
        user_agent="ua",
        debug_http=False,
        auth_recovery_backoff_seconds=10,
        auth_recovery_backoff_cap_seconds=60,
    )
    session = StravaSession(settings, "stale-cookie", auth_mode="cookiestxt", auth_fallback="cookiestxt", cookies_file=str(cookie_path))

    class FakeResponse:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.headers = {"Content-Type": "text/html"}
            self.url = "https://www.strava.com/frontend/athletes/current"

    sleep_calls: list[int] = []
    monkeypatch.setattr("ingestion.session.exponential_backoff", lambda attempt, **kwargs: attempt * 10)
    monkeypatch.setattr("ingestion.session._interruptible_sleep", lambda seconds: sleep_calls.append(seconds))
    monkeypatch.setattr(session, "_send_request", lambda path, params=None: FakeResponse(200))

    session.reauthenticate()

    assert session.cookie_value == "fresh-cookie"
    assert sleep_calls == [10]
    assert session._auth_recovery_failures == 0
