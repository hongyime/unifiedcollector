"""Pure-unit tests for src.core.auth_session.IgSession.

All HTTP traffic is mocked via httpx.MockTransport — NO live IG calls.

Coverage:
  1. AST parse — implicit (import succeeds).
  2. SessionCapsule Protocol — IgSession satisfies it (runtime check).
  3. Atomic save() + load() round-trip preserves cookies/meta.
  4. load() on missing file -> False (no crash).
  5. load() of legacy bare-dict pickle still recovers cookies.
  6. refresh() on 200 -> logged_in + last_warmup_at advances.
  7. refresh() absorbs Set-Cookie values into self.cookies.
  8. refresh() on 401 -> logged_out, last_warmup_at unchanged.
  9. refresh() on body containing 'challenge_required' -> CHALLENGE_REQUIRED.
 10. is_alive() with no user_id falls back to refresh().
 11. is_alive() 200 -> True; 403 -> False + logged_out.
 12. is_alive() challenge body -> False + CHALLENGE_REQUIRED.
 13. maybe_warmup() skips when last_warmup_at is fresh.
 14. maybe_warmup() fires refresh when stale.
 15. __repr__ MASKS cookie values (no sessionid leakage).
 16. _looks_like_challenge heuristics.
"""
from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from src.core.auth_session import (
    CHALLENGE_REQUIRED,
    DEFAULT_MIN_WARMUP_INTERVAL_S,
    LOGGED_IN,
    LOGGED_OUT,
    IgSession,
    SessionCapsule,
    _looks_like_challenge,
    default_cookie_path,
)


# ---------- helpers --------------------------------------------------------


def make_transport(handler):
    """Wrap a request->Response handler into an httpx MockTransport."""
    return httpx.MockTransport(handler)


@pytest_asyncio.fixture
async def mock_client_factory():
    """Factory: given a handler, yield an httpx.AsyncClient bound to a mock transport."""
    created = []

    def _make(handler):
        client = httpx.AsyncClient(transport=make_transport(handler))
        created.append(client)
        return client

    yield _make
    for c in created:
        await c.aclose()


# ---------- protocol -------------------------------------------------------


def test_satisfies_session_capsule_protocol():
    s = IgSession(account_name="alice")
    assert isinstance(s, SessionCapsule)


def test_default_cookie_path():
    p = default_cookie_path("alice")
    assert p == Path("data/instagram/alice/cookies.pkl")


# ---------- persistence ----------------------------------------------------


@pytest.mark.asyncio
async def test_save_and_load_roundtrip(tmp_path):
    p = tmp_path / "alice" / "cookies.pkl"
    s = IgSession(account_name="alice", cookie_path=p)
    s.cookies = {"sessionid": "secret-abc", "csrftoken": "csrf-xyz"}
    s.login_status = LOGGED_IN
    s.last_warmup_at = 12345.0
    s.user_id = "9876543210"

    await s.save()
    assert p.exists(), "atomic save should produce the file"

    s2 = IgSession(account_name="alice", cookie_path=p)
    ok = await s2.load()
    assert ok is True
    assert s2.cookies == {"sessionid": "secret-abc", "csrftoken": "csrf-xyz"}
    assert s2.login_status == LOGGED_IN
    assert s2.last_warmup_at == 12345.0
    assert s2.user_id == "9876543210"


@pytest.mark.asyncio
async def test_load_missing_file_returns_false(tmp_path):
    s = IgSession(account_name="ghost", cookie_path=tmp_path / "nope.pkl")
    assert await s.load() is False
    assert s.login_status == LOGGED_OUT


@pytest.mark.asyncio
async def test_load_legacy_bare_dict(tmp_path):
    import pickle as _pkl

    p = tmp_path / "legacy.pkl"
    p.write_bytes(_pkl.dumps({"sessionid": "old", "csrftoken": "old2"}))

    s = IgSession(account_name="legacy", cookie_path=p)
    ok = await s.load()
    assert ok is True
    assert s.cookies == {"sessionid": "old", "csrftoken": "old2"}
    assert s.login_status == LOGGED_IN


@pytest.mark.asyncio
async def test_save_atomic_no_partial_on_error(tmp_path, monkeypatch):
    """If pickle.dump fails, no .pkl turd is left behind in the parent dir."""
    p = tmp_path / "dst" / "cookies.pkl"
    s = IgSession(account_name="bob", cookie_path=p)
    s.cookies = {"sessionid": "x"}

    import src.core.auth_session as mod

    def boom(_path, _payload):
        raise RuntimeError("disk full")

    monkeypatch.setattr(mod, "_atomic_pickle_write", boom)
    with pytest.raises(RuntimeError):
        await s.save()
    assert not p.exists()


# ---------- refresh --------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_200_marks_logged_in_and_advances_warmup(mock_client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='{"status":"ok"}')

    client = mock_client_factory(handler)
    s = IgSession(account_name="alice", client=client)
    before = s.last_warmup_at
    ok = await s.refresh()
    assert ok is True
    assert s.login_status == LOGGED_IN
    assert s.last_warmup_at > before


@pytest.mark.asyncio
async def test_refresh_absorbs_set_cookie(mock_client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="ok",
            headers={"set-cookie": "rolled=value-1; Path=/"},
        )

    client = mock_client_factory(handler)
    s = IgSession(account_name="alice", client=client)
    s.cookies = {"sessionid": "old"}
    ok = await s.refresh()
    assert ok is True
    assert s.cookies.get("rolled") == "value-1"
    assert s.cookies.get("sessionid") == "old"  # preserved


@pytest.mark.asyncio
async def test_refresh_401_marks_logged_out(mock_client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    client = mock_client_factory(handler)
    s = IgSession(account_name="alice", client=client)
    s.last_warmup_at = 100.0
    ok = await s.refresh()
    assert ok is False
    assert s.login_status == LOGGED_OUT
    assert s.last_warmup_at == 100.0  # not advanced on failure


@pytest.mark.asyncio
async def test_refresh_challenge_body_flags_challenge(mock_client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text='{"message":"challenge_required","status":"fail"}',
        )

    client = mock_client_factory(handler)
    s = IgSession(account_name="alice", client=client)
    ok = await s.refresh()
    assert ok is False
    assert s.login_status == CHALLENGE_REQUIRED
    assert s.is_challenge is True


@pytest.mark.asyncio
async def test_refresh_network_error_returns_false(mock_client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = mock_client_factory(handler)
    s = IgSession(account_name="alice", client=client)
    ok = await s.refresh()
    assert ok is False
    assert s.login_status == LOGGED_OUT  # unchanged from default


# ---------- is_alive -------------------------------------------------------


@pytest.mark.asyncio
async def test_is_alive_no_user_id_falls_back_to_refresh(mock_client_factory):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="ok")

    client = mock_client_factory(handler)
    s = IgSession(account_name="alice", client=client)  # no user_id
    ok = await s.is_alive()
    assert ok is True
    assert any("/accounts/edit/" in u for u in calls)


@pytest.mark.asyncio
async def test_is_alive_200_true_403_false(mock_client_factory):
    def handler_ok(request):
        return httpx.Response(200, text='{"user":{"pk":"42"}}')

    client = mock_client_factory(handler_ok)
    s = IgSession(account_name="alice", user_id="42", client=client)
    assert await s.is_alive() is True
    assert s.login_status == LOGGED_IN

    def handler_403(request):
        return httpx.Response(403, text="forbidden")

    client2 = mock_client_factory(handler_403)
    s2 = IgSession(account_name="alice", user_id="42", client=client2)
    assert await s2.is_alive() is False
    assert s2.login_status == LOGGED_OUT


@pytest.mark.asyncio
async def test_is_alive_challenge_body(mock_client_factory):
    def handler(request):
        return httpx.Response(
            400,
            text='{"message":"challenge_required"}',
        )

    client = mock_client_factory(handler)
    s = IgSession(account_name="alice", user_id="42", client=client)
    ok = await s.is_alive()
    assert ok is False
    assert s.login_status == CHALLENGE_REQUIRED


# ---------- maybe_warmup ---------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_warmup_skips_when_fresh(mock_client_factory):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, text="ok")

    client = mock_client_factory(handler)
    s = IgSession(account_name="alice", client=client)
    s.last_warmup_at = time.time()  # very fresh
    fired = await s.maybe_warmup(min_interval_s=600)
    assert fired is False
    assert calls == [], "no HTTP call should be made when fresh"


@pytest.mark.asyncio
async def test_maybe_warmup_fires_when_stale(mock_client_factory):
    def handler(request):
        return httpx.Response(200, text="ok")

    client = mock_client_factory(handler)
    s = IgSession(account_name="alice", client=client)
    s.last_warmup_at = 1.0  # very stale
    fired = await s.maybe_warmup(min_interval_s=600)
    assert fired is True
    assert s.last_warmup_at > 1.0


@pytest.mark.asyncio
async def test_maybe_warmup_default_interval_constant():
    assert DEFAULT_MIN_WARMUP_INTERVAL_S == 600.0


# ---------- repr / safety --------------------------------------------------


def test_repr_masks_cookie_values():
    s = IgSession(account_name="alice")
    s.cookies = {"sessionid": "TOP-SECRET-VALUE", "csrftoken": "ANOTHER-SECRET"}
    r = repr(s)
    assert "TOP-SECRET-VALUE" not in r
    assert "ANOTHER-SECRET" not in r
    assert "alice" in r
    assert "masked" in r


# ---------- challenge heuristic --------------------------------------------


def test_looks_like_challenge_json_body():
    assert _looks_like_challenge(200, '{"message":"challenge_required"}') is True


def test_looks_like_challenge_html_redirect():
    assert _looks_like_challenge(403, '<a href="/challenge/foo/bar/">') is True


def test_looks_like_challenge_negative():
    assert _looks_like_challenge(200, '{"status":"ok"}') is False
    assert _looks_like_challenge(500, "internal error") is False
