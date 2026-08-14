from src.core.request_persona import build_persona_headers, origin_for_url, persona_for_url


def test_persona_for_url_is_stable_by_domain(monkeypatch):
    monkeypatch.setenv("WEB_REQUEST_PERSONA_MODE", "stable_by_domain")

    first = persona_for_url("https://example.com/a", source="website")
    second = persona_for_url("https://example.com/b", source="website")

    assert first == second


def test_build_persona_headers_can_be_disabled(monkeypatch):
    monkeypatch.setenv("WEB_REQUEST_PERSONA_MODE", "off")

    headers, metadata = build_persona_headers({"User-Agent": "existing"}, "https://example.com/", source="search")

    assert headers["User-Agent"] == "existing"
    assert metadata["enabled"] is False


def test_build_persona_headers_adds_device_and_origin(monkeypatch):
    monkeypatch.setenv("WEB_REQUEST_PERSONA_MODE", "stable_by_domain")
    monkeypatch.setenv("WEB_REQUEST_ORIGIN_POOL", "https://origin.example/")

    headers, metadata = build_persona_headers({}, "https://example.com/", source="website")

    assert "User-Agent" in headers
    assert headers["Origin"] == "https://origin.example"
    assert headers["Referer"] == "https://origin.example/"
    assert headers["Viewport-Width"] == str(metadata["viewport_width"])
    assert metadata["origin"] == origin_for_url("https://example.com/", source="website")
