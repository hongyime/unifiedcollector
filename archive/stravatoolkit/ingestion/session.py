from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path

from ingestion.tools.diagnostics.runtime import (
    bootstrap_requests_dependency_warnings,
    emit_requests_dependency_health_once,
)

bootstrap_requests_dependency_warnings()

from ingestion.config import STRAVA_BASE_URL, Settings, now_utc_iso
from ingestion.core.delays import exponential_backoff, random_delay, wait_for_internet

logger = logging.getLogger(__name__)


def _interruptible_sleep(seconds: float, check_interval: float = 0.2) -> None:
    """Sleep in short slices so Ctrl+C is observed quickly."""
    if seconds <= 0:
        return
    end_time = time.time() + seconds
    while True:
        remaining = end_time - time.time()
        if remaining <= 0:
            return
        time.sleep(min(check_interval, remaining))


class SessionError(RuntimeError):
    """Raised when session setup or validation fails."""
    
    def __init__(self, message: str, error_type: str = "unknown"):
        super().__init__(message)
        self.error_type = error_type


class StravaSession:
    def __init__(
        self,
        settings: Settings,
        cookie_value: str,
        *,
        auth_mode: str = "cookiestxt",
        auth_fallback: str = "auto",
        cookies_file: str | None = None,
        shutdown_event=None,
    ):
        self.settings = settings
        self.cookie_value = cookie_value
        self.auth_mode = auth_mode
        self.auth_fallback = auth_fallback
        self.cookies_file = cookies_file
        self.shutdown_event = shutdown_event
        self._persist_callback = None
        emit_requests_dependency_health_once()
        import requests as _requests
        self.client = _requests.Session()
        self.client.headers.update(
            {
                "User-Agent": settings.user_agent,
                "Accept": "application/json, text/plain, */*",
            }
        )
        self._apply_cookie(cookie_value)
        # Configure delay range
        self._delay_range = (
            getattr(settings, "api_delay_min_seconds", 1.0),
            getattr(settings, "api_delay_max_seconds", 3.0),
        )
        self._debug_delays = getattr(settings, "debug_delays", False)
        self._auth_recovery_failures = 0

    @classmethod
    def from_sources(
        cls,
        settings: Settings,
        *,
        auth_mode: str = "cookiestxt",
        auth_fallback: str = "auto",
        cookie_value: str | None = None,
        cookies_file: str | None = None,
    ) -> "StravaSession":
        resolved_cookie = cookie_value
        if not resolved_cookie:
            for source in _initial_cookie_sources(auth_mode, auth_fallback, cookies_file):
                try:
                    resolved_cookie = _load_cookie_for_source(source, settings, cookies_file)
                except SessionError:
                    continue
                if resolved_cookie:
                    break
        if not resolved_cookie:
            raise SessionError("No Strava session cookie was provided.")
        return cls(
            settings,
            resolved_cookie,
            auth_mode=auth_mode,
            auth_fallback=auth_fallback,
            cookies_file=cookies_file,
        )

    def persist_cookie(self) -> None:
        env_path = Path(self.settings.env_path)
        env_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(
            env_path,
            f"STRAVA_SESSION_COOKIE={self.cookie_value}\nCAPTURED_AT={now_utc_iso()}\n",
        )

    def set_persist_callback(self, callback) -> None:
        self._persist_callback = callback

    def clone(self) -> "StravaSession":
        return StravaSession(
            self.settings,
            self.cookie_value,
            auth_mode=self.auth_mode,
            auth_fallback=self.auth_fallback,
            cookies_file=self.cookies_file,
            shutdown_event=self.shutdown_event,
        )

    def validate(self) -> dict:
        response = self._request("/frontend/athletes/current")
        if response.status_code != 200:
            if response.status_code == 429:
                raise SessionError("Session validation hit HTTP 429 (likely rate limited by Strava). Please wait a bit and retry.", error_type="rate_limited")
            raise SessionError(f"Session validation failed with HTTP {response.status_code}", error_type="auth_failed")
        return response.json()["currentAthlete"]

    def get_json(self, path: str, **params) -> tuple[requests.Response, object]:
        response = self._request(path, params=params)
        return response, _safe_json(response)

    def get_text(self, path: str, **params) -> tuple[requests.Response, str]:
        response = self._request(path, params=params)
        return response, response.text

    def _request(self, path: str, params: dict | None = None) -> requests.Response:
        response = self._send_request(path, params=params)
        if not _is_auth_failure_response(response):
            self._auth_recovery_failures = 0
            return response

        self._log_debug(f"Auth failure for {path}: {_response_debug_summary(response)}")
        self.reauthenticate(failed_response=response)
        retry_response = self._send_request(path, params=params)
        if _is_auth_failure_response(retry_response):
            raise SessionError(
                f"Session recovery did not resolve auth failure for {path}: {_response_debug_summary(retry_response)}",
                error_type="auth_failed"
            )
        self._auth_recovery_failures = 0
        return retry_response

    def reauthenticate(self, *, failed_response: requests.Response | None = None) -> None:
        attempted_sources: list[str] = []
        last_error: Exception | None = None
        self._auth_recovery_failures += 1
        self._pause_before_auth_recovery()
        for source in self._recovery_sources():
            if source in attempted_sources:
                continue
            attempted_sources.append(source)
            try:
                candidate_cookie = _load_cookie_for_source(source, self.settings, self.cookies_file)
                if not candidate_cookie or candidate_cookie == self.cookie_value:
                    continue
                self._log_debug(f"Trying auth recovery via {source}.")
                if self._apply_candidate_cookie(candidate_cookie):
                    self._persist_recovered_cookie(source)
                    logger.info(f"Session recovered via {self._describe_source(source)}.")
                    self._auth_recovery_failures = 0
                    return
            except SessionError as exc:
                last_error = exc
                self._log_debug(f"Auth recovery via {source} failed: {exc}")

        should_force_playwright = self.auth_fallback in {"auto", "playwright"}
        if should_force_playwright and "playwright" not in attempted_sources:
            try:
                candidate_cookie = _load_cookie_for_source("playwright", self.settings, self.cookies_file)
                self._log_debug("Trying auth recovery via interactive Playwright login.")
                if self._apply_candidate_cookie(candidate_cookie):
                    self._persist_recovered_cookie("playwright")
                    logger.info("Session recovered via Playwright login. cookies.txt was updated.")
                    self._auth_recovery_failures = 0
                    return
            except SessionError as exc:
                last_error = exc
                self._log_debug(f"Auth recovery via Playwright failed: {exc}")

        raise SessionError(
            "Session expired and automatic recovery failed. Please sign in again in Playwright."
            + (f" Last error: {last_error}" if last_error else "")
            + (f" Response: {_response_debug_summary(failed_response)}" if failed_response is not None else ""),
            error_type="auth_failed"
        )

    def _apply_cookie(self, cookie_value: str) -> None:
        self.cookie_value = cookie_value
        self.client.headers["Cookie"] = f"_strava4_session={cookie_value}"

    def _send_request(self, path: str, params: dict | None = None) -> requests.Response:
        response = None
        max_attempts = self.settings.rate_limit_retries + 1
        for attempt in range(max_attempts):
            # Add delay before each request
            if attempt > 0:
                # Use exponential backoff for retries after 429
                wait_seconds = exponential_backoff(
                    attempt - 1,  # 0-based for exponential calculation
                    base_delay=self.settings.rate_limit_backoff_seconds,
                    max_delay=180.0,  # 3 minutes max (more reasonable than 5 min)
                    backoff_factor=2.5,  # Slightly more aggressive than default 2.0
                    jitter=0.3,  # Reduce jitter to improve predictability
                    debug=self._debug_delays
                )
                logger.warning(
                    f"Strava returned HTTP 429 for {path}. "
                    f"Pausing {wait_seconds:.2f} second(s) before retry {attempt}/{self.settings.rate_limit_retries}..."
                )
                _interruptible_sleep(wait_seconds)
            else:
                # Add random delay before first request
                random_delay(self._delay_range, debug=self._debug_delays, shutdown_event=self.shutdown_event)
            
            try:
                response = self.client.get(
                    f"{STRAVA_BASE_URL}{path}",
                    params=params,
                    timeout=self.settings.request_timeout_seconds,
                    allow_redirects=False,
                )
            except OSError:
                if not wait_for_internet(self.shutdown_event):
                    raise
                continue

            # Return immediately if not rate-limited, or if we've exhausted all attempts
            if response.status_code != 429:
                return response
            elif attempt >= max_attempts - 1:
                # Return even if 429, as we're out of retries
                return response
                
        return response

    def _apply_candidate_cookie(self, cookie_value: str) -> bool:
        previous_cookie = self.cookie_value
        self._apply_cookie(cookie_value)
        try:
            response = self._send_request("/frontend/athletes/current")
        except Exception as exc:
            self._apply_cookie(previous_cookie)
            raise SessionError(f"Cookie validation failed during recovery: {exc}", error_type="network") from exc
        if response.status_code != 200:
            self._apply_cookie(previous_cookie)
            self._log_debug(f"Recovered cookie was still invalid: {_response_debug_summary(response)}")
            return False
        return True

    def _persist_recovered_cookie(self, source: str) -> None:
        self.persist_cookie()
        if self.cookies_file and source == "playwright":
            _write_cookie_file(self.cookies_file, self.cookie_value)
        if self._persist_callback is not None:
            self._persist_callback(self.cookie_value, source)

    def _pause_before_auth_recovery(self) -> None:
        wait_seconds = exponential_backoff(
            self._auth_recovery_failures,
            base_delay=getattr(self.settings, "auth_recovery_backoff_seconds", 30),
            max_delay=getattr(self.settings, "auth_recovery_backoff_cap_seconds", 300),
            debug=self._debug_delays,
        )
        if wait_seconds <= 0:
            return
        logger.info(
            f"Auth recovery cooldown {wait_seconds:.2f} second(s) "
            f"before attempt {self._auth_recovery_failures}."
        )
        _interruptible_sleep(wait_seconds)

    def _recovery_sources(self) -> list[str]:
        primary = "cookiestxt" if self.auth_mode == "cookiestxt" else "env"
        sources: list[str] = [primary]
        if self.auth_fallback == "cookiestxt" and "cookiestxt" not in sources:
            sources.append("cookiestxt")
        if self.auth_fallback == "none":
            return sources
        if self.auth_fallback == "auto":
            for source in ("env", "cookiestxt"):
                if source not in sources:
                    sources.append(source)
            sources.append("playwright")
            return sources
        if self.auth_fallback == "playwright":
            sources.append("playwright")
            return sources
        return sources

    def _describe_source(self, source: str) -> str:
        if source == "cookiestxt":
            return "cookies.txt"
        if source == "env":
            return "toolkit session store"
        if source == "playwright":
            return "Playwright login"
        return source

    def _log_debug(self, message: str) -> None:
        if self.settings.debug_http:
            logger.debug(message)


def _read_cookie_file(path: str) -> str:
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and parts[5] == "_strava4_session":
            return parts[6]
    raise SessionError("The cookies.txt file did not contain _strava4_session.", error_type="auth_failed")


def _write_cookie_file(path: str, cookie_value: str) -> None:
    cookie_path = Path(path)
    cookie_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        cookie_path,
        "# Netscape HTTP Cookie File\n"
        ".strava.com\tTRUE\t/\tTRUE\t2147483647\t_strava4_session\t"
        f"{cookie_value}\n",
    )


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def _read_cookie_from_env_file(path: str | Path) -> str | None:
    env_path = Path(path)
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("STRAVA_SESSION_COOKIE="):
            value = line.split("=", 1)[1].strip()
            return value or None
    return None


def _initial_cookie_sources(auth_mode: str, auth_fallback: str, cookies_file: str | None) -> list[str]:
    sources = ["cookiestxt", "env"] if auth_mode == "cookiestxt" else ["env", "cookiestxt"]
    resolved: list[str] = []
    for source in sources:
        if source == "cookiestxt" and not cookies_file:
            continue
        if source not in resolved:
            resolved.append(source)
    if auth_fallback in {"auto", "playwright"} and "playwright" not in resolved:
        resolved.append("playwright")
    return resolved


def _load_cookie_for_source(source: str, settings: Settings, cookies_file: str | None) -> str:
    if source == "env":
        cookie = _read_cookie_from_env_file(settings.env_path)
        if not cookie:
            raise SessionError("No session cookie was stored in the toolkit env file.", error_type="auth_failed")
        return cookie
    if source == "cookiestxt":
        if not cookies_file:
            raise SessionError("No cookies.txt path was configured.", error_type="auth_failed")
        return _read_cookie_file(cookies_file)
    if source == "playwright":
        cookie = _interactive_playwright_cookie(settings)
        if cookies_file:
            _write_cookie_file(cookies_file, cookie)
        return cookie
    raise SessionError(f"Unknown auth source '{source}'.")


def _is_auth_failure_response(response: requests.Response) -> bool:
    if response.status_code in {302, 401, 403}:
        return True
    location = response.headers.get("Location", "")
    return "/login" in location or "/login" in str(response.url)


def _response_debug_summary(response: requests.Response | None) -> str:
    if response is None:
        return "no response"
    content_type = response.headers.get("Content-Type", "")
    location = response.headers.get("Location", "")
    return (
        f"HTTP {response.status_code}, url={response.url}, content_type={content_type or 'unknown'}"
        + (f", location={location}" if location else "")
    )


def _safe_json(response: requests.Response) -> object:
    if not response.text:
        return {}
    try:
        return response.json()
    except ValueError:
        return {}


def _interactive_playwright_cookie(settings: Settings) -> str:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = None
        launch_errors: list[str] = []
        for channel in ("chrome", "msedge", ""):
            try:
                if channel:
                    browser = playwright.chromium.launch(headless=False, channel=channel)
                else:
                    browser = playwright.chromium.launch(headless=False)
                if getattr(settings, "debug_http", False):
                    chosen = channel or "bundled-chromium"
                    logger.debug(f"Playwright opened using {chosen}.")
                break
            except Exception as exc:
                launch_errors.append(f"{channel or 'bundled-chromium'}: {exc}")
        if browser is None:
            raise SessionError("Could not launch Chrome, Edge, or bundled Chromium for Playwright login.", error_type="auth_failed")

        try:
            page = browser.new_page(user_agent=settings.user_agent)
            page.set_default_navigation_timeout(30_000)  # 30s default
            page.goto(f"{STRAVA_BASE_URL}/login", wait_until="domcontentloaded")
            page.wait_for_url("**/dashboard*", timeout=60_000)  # 60s max
            cookies = page.context.cookies()
        except Exception as exc:
            browser.close()
            detail = "; ".join(launch_errors)
            if detail:
                raise SessionError(f"Playwright login did not complete successfully: {exc}. Launch attempts: {detail}", error_type="auth_failed") from exc
            raise SessionError(f"Playwright login did not complete successfully: {exc}", error_type="auth_failed") from exc
        browser.close()

    for cookie in cookies:
        if cookie.get("name") == "_strava4_session":
            return str(cookie["value"])
    raise SessionError("Playwright login completed without finding _strava4_session.", error_type="auth_failed")
