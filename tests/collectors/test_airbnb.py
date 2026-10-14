from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.collectors import airbnb as airbnb_mod
from src.collectors.airbnb import AirbnbCollector


@pytest.mark.asyncio
async def test_probe_profile_uses_numeric_airbnb_profile_route(monkeypatch):
    response = MagicMock(
        status_code=200,
        text=(
            '{"first_name":"Ada","last_name":"Lovelace",'
            '"picture_url":"https://cdn.example/avatar.jpg",'
            '"created_at":"2020-01-02","is_superhost":true}'
        ),
    )
    client = MagicMock()
    client.get = AsyncMock(return_value=response)

    @asynccontextmanager
    async def fake_client(*args, **kwargs):
        yield client

    monkeypatch.setattr(airbnb_mod.httpx, "AsyncClient", fake_client)
    collector = AirbnbCollector()

    result = await collector.probe_profile("350595032")

    assert client.get.await_args.args[0] == "https://www.airbnb.com/users/show/350595032"
    assert result.exists is True
    assert result.fields["display_name"] == "Ada Lovelace"


@pytest.mark.asyncio
async def test_probe_profile_treats_unparsed_200_as_error_not_404(monkeypatch):
    response = MagicMock(status_code=200, text="<html>App shell</html>")
    client = MagicMock()
    client.get = AsyncMock(return_value=response)

    @asynccontextmanager
    async def fake_client(*args, **kwargs):
        yield client

    monkeypatch.setattr(airbnb_mod.httpx, "AsyncClient", fake_client)
    result = await AirbnbCollector().probe_profile("350595032")

    assert result.exists is False
    assert result.error == "profile_payload_unavailable"
