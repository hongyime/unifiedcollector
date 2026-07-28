import pytest


def test_split_text_prefers_line_boundaries():
    from src.notifications.telegram import _split_text

    text = "a\n" * 4 + "b\n" * 4

    parts = _split_text(text, limit=8)

    assert parts == ["a\na\na\na", "b\nb\nb\nb"]
    assert all(len(part) <= 8 for part in parts)


@pytest.mark.asyncio
async def test_send_splits_long_messages(monkeypatch):
    from src.notifications import telegram

    sent: list[dict] = []

    def fake_post(_token, payload):
        sent.append(payload)
        return True

    monkeypatch.setattr(telegram, "_config", lambda: ("token", "chat", ""))
    monkeypatch.setattr(telegram, "_post", fake_post)
    monkeypatch.setattr(telegram, "_MAX_TEXT_CHARS", 20)

    ok = await telegram.send("first line\nsecond line\nthird line")

    assert ok is True
    assert [p["text"] for p in sent] == ["first line", "second line", "third line"]
    assert all(p["parse_mode"] == "HTML" for p in sent)
