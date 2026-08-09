import pytest
import io
from urllib.error import HTTPError


def test_split_text_prefers_line_boundaries():
    from src.notifications.telegram import _split_text

    text = "a\n" * 4 + "b\n" * 4

    parts = _split_text(text, limit=8)

    assert parts == ["a\na\na\na", "b\nb\nb\nb"]
    assert all(len(part) <= 8 for part in parts)


def test_text_post_records_retry_after_cooldown(monkeypatch):
    from src.notifications import telegram

    class FakeHeaders:
        def get(self, _name):
            return None

    monkeypatch.setattr(telegram.time, "time", lambda: 1000.0)
    telegram._TEXT_COOLDOWN_UNTIL = 0

    def fake_urlopen(_req, timeout=15):
        raise HTTPError(
            url="https://api.telegram.org/botTOKEN/sendMessage",
            code=429,
            msg="Too Many Requests",
            hdrs=FakeHeaders(),
            fp=io.BytesIO(b'{"ok":false,"parameters":{"retry_after":17}}'),
        )

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)

    ok = telegram._post("token", {"chat_id": "chat", "text": "hello"})

    assert ok is False
    assert telegram._TEXT_COOLDOWN_UNTIL == 1017.0
    telegram._TEXT_COOLDOWN_UNTIL = 0


def test_text_post_skips_during_cooldown(monkeypatch):
    from src.notifications import telegram

    def fake_urlopen(_req, timeout=15):
        raise AssertionError("urlopen should not be called during cooldown")

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(telegram.time, "time", lambda: 1000.0)
    telegram._TEXT_COOLDOWN_UNTIL = 1015.0

    ok = telegram._post("token", {"chat_id": "chat", "text": "hello"})

    assert ok is False
    telegram._TEXT_COOLDOWN_UNTIL = 0


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




@pytest.mark.asyncio
async def test_send_many_skips_falsy_and_sends_each(monkeypatch):
    """send_many should call send() once per non-empty entry, skipping falsy ones."""
    from src.notifications import telegram

    sent: list[str] = []
    sleeps: list[float] = []

    async def fake_send(text: str, parse_mode: str = "HTML") -> bool:
        sent.append(text)
        return True

    async def fake_sleep(secs):
        sleeps.append(secs)

    monkeypatch.setattr(telegram, "send", fake_send)
    monkeypatch.setattr(telegram.asyncio, "sleep", fake_sleep)

    ok = await telegram.send_many([
        "alpha section",
        "",           # skipped
        None,          # skipped
        "beta section",
        "gamma section",
    ])

    assert ok is True
    assert sent == ["alpha section", "beta section", "gamma section"]
    # 200ms pause between consecutive sends, so N-1 sleeps for N sends.
    assert sleeps == [0.2, 0.2]


@pytest.mark.asyncio
async def test_send_many_returns_false_when_any_send_fails(monkeypatch):
    from src.notifications import telegram

    calls = {"n": 0}

    async def flaky_send(text: str, parse_mode: str = "HTML") -> bool:
        calls["n"] += 1
        # Second send fails.
        return calls["n"] != 2

    async def fake_sleep(_secs):
        pass

    monkeypatch.setattr(telegram, "send", flaky_send)
    monkeypatch.setattr(telegram.asyncio, "sleep", fake_sleep)

    ok = await telegram.send_many(["one", "two", "three"])

    assert ok is False
    # Still attempts all three so a mid-batch failure does not silently drop later sections.
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_send_many_empty_list_is_true_noop(monkeypatch):
    from src.notifications import telegram

    async def fake_send(_text: str, parse_mode: str = "HTML") -> bool:
        raise AssertionError("send() must not be called for empty/falsy-only lists")

    async def fake_sleep(_secs):
        raise AssertionError("no sleeps expected for empty input")

    monkeypatch.setattr(telegram, "send", fake_send)
    monkeypatch.setattr(telegram.asyncio, "sleep", fake_sleep)

    assert await telegram.send_many([]) is True
    assert await telegram.send_many([None, "", None]) is True
