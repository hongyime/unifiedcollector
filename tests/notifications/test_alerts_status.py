import pytest


@pytest.mark.asyncio
async def test_notify_status_splits_429s_from_auth_errors(monkeypatch):
    from src.notifications import alerts

    sent: list[str] = []

    async def fake_send(text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(alerts.telegram, "send", fake_send)

    ok = await alerts.notify_status({
        "ok": False,
        "hourly_ingestion": {
            "totals": {
                "records": 10,
                "messages": 2,
                "files": 1,
                "rate_limits": 1,
                "access_errors": 1,
            },
            "sources": [
                {
                    "source": "instagram",
                    "records": 4,
                    "messages": 0,
                    "files": 1,
                    "rate_limits": 1,
                    "access_errors": 1,
                }
            ],
        },
        "active_rate_limits": [
            {
                "service": "instagram_rate_limit",
                "seconds_remaining": 3600,
                "streak": 7,
            }
        ],
        "rate_limit_events": [
            {
                "source": "instagram",
                "account": "bryanseah234",
                "scope": "profile_fetch",
                "status_code": 429,
                "count": 1,
            }
        ],
        "access_events": [
            {
                "source": "instagram",
                "account": "bryanseah234",
                "scope": "profile_fetch_playwright",
                "status_code": 401,
                "count": 1,
                "reason": "Playwright profile auth response",
            }
        ],
        "vault": {
            "root": "/vault",
            "available": True,
            "writable": True,
            "free_bytes": 1024,
            "artifacts_queued": 0,
            "artifacts_partial": 0,
            "sidecar_failures": 0,
            "counts_error": "TimeoutError",
        },
    })

    assert ok is True
    msg = sent[0]
    assert "Recorded HTTP 429 events this hour: 1." in msg
    assert "Recorded auth/access HTTP errors this hour: 1." in msg
    assert "Instagram: 4 source rows, 1 media file, 1 HTTP 429 event, 1 auth/access HTTP error" in msg
    assert "Session/auth HTTP failures this hour:" in msg
    assert "HTTP 401" in msg
    assert "Writable at <code>/vault</code>" in msg
    assert "Artifact health counts timed out" in msg
