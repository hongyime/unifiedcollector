"""Deterministic request persona headers for web/search fetches.

The personas are used to capture legitimate content variance across desktop,
mobile, language, and referring context. They are not a retry-bypass mechanism:
rate limits, cooldowns, and per-domain pacing remain authoritative.
"""
from __future__ import annotations

import hashlib
import os
import random
from dataclasses import dataclass
from urllib.parse import urlparse


def _env_flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _csv_values(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return values or default


@dataclass(frozen=True)
class RequestPersona:
    name: str
    device: str
    user_agent: str
    accept_language: str
    viewport_width: int

    @property
    def sec_ch_ua_mobile(self) -> str:
        return "?1" if self.device == "mobile" else "?0"


DEFAULT_PERSONAS: tuple[RequestPersona, ...] = (
    RequestPersona(
        name="chrome-desktop-us",
        device="desktop",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        accept_language="en-US,en;q=0.9",
        viewport_width=1440,
    ),
    RequestPersona(
        name="edge-desktop-sg",
        device="desktop",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
        ),
        accept_language="en-SG,en;q=0.9",
        viewport_width=1366,
    ),
    RequestPersona(
        name="chrome-mobile-us",
        device="mobile",
        user_agent=(
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
        ),
        accept_language="en-US,en;q=0.9",
        viewport_width=412,
    ),
)

DEFAULT_ORIGIN_POOL = (
    "https://www.google.com/",
    "https://www.bing.com/",
    "https://duckduckgo.com/",
)


def _stable_index(seed: str, size: int) -> int:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % max(1, size)


def _host_for_url(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return (parsed.hostname or parsed.netloc or url).lower().strip(".")


def persona_for_url(url: str, *, source: str = "") -> RequestPersona:
    """Return a deterministic or rotating persona for a URL.

    ``WEB_REQUEST_PERSONA_MODE=off`` keeps caller-supplied headers untouched.
    ``stable_by_domain`` is the default so repeated visits to a domain do not
    flap between devices every cycle. ``rotate`` is available for deliberate
    content-variance probes.
    """
    mode = os.getenv("WEB_REQUEST_PERSONA_MODE", "stable_by_domain").strip().lower()
    if mode == "rotate":
        return random.choice(DEFAULT_PERSONAS)
    seed = f"{source}:{_host_for_url(url)}"
    return DEFAULT_PERSONAS[_stable_index(seed, len(DEFAULT_PERSONAS))]


def origin_for_url(url: str, *, source: str = "") -> str:
    origins = _csv_values("WEB_REQUEST_ORIGIN_POOL", DEFAULT_ORIGIN_POOL)
    mode = os.getenv("WEB_REQUEST_PERSONA_MODE", "stable_by_domain").strip().lower()
    if mode == "rotate":
        return random.choice(origins)
    seed = f"origin:{source}:{_host_for_url(url)}"
    return origins[_stable_index(seed, len(origins))]


def build_persona_headers(
    base_headers: dict[str, str] | None,
    url: str,
    *,
    source: str = "",
) -> tuple[dict[str, str], dict[str, object]]:
    """Merge persona headers into ``base_headers`` and return audit metadata."""
    headers = dict(base_headers or {})
    mode = os.getenv("WEB_REQUEST_PERSONA_MODE", "stable_by_domain").strip().lower()
    if mode == "off" or not _env_flag("WEB_REQUEST_PERSONA_ENABLED", "1"):
        return headers, {"enabled": False, "mode": mode or "off"}

    persona = persona_for_url(url, source=source)
    headers["User-Agent"] = persona.user_agent
    headers["Accept-Language"] = persona.accept_language
    headers.setdefault(
        "Accept",
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    )
    headers["Sec-CH-UA-Mobile"] = persona.sec_ch_ua_mobile
    headers["Viewport-Width"] = str(persona.viewport_width)

    origin = ""
    if _env_flag("WEB_REQUEST_SEND_ORIGIN", "1"):
        origin = origin_for_url(url, source=source)
        headers.setdefault("Origin", origin.rstrip("/"))
        headers.setdefault("Referer", origin)

    metadata = {
        "enabled": True,
        "mode": mode,
        "persona": persona.name,
        "device": persona.device,
        "viewport_width": persona.viewport_width,
        "accept_language": persona.accept_language,
        "origin": origin,
    }
    return headers, metadata
