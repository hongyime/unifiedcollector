import pytest

from src.core import tm_probe
from src.core.browser_rotator import DEFAULT_BROWSER_SOURCES


@pytest.mark.asyncio
async def test_set_active_combo_activates_only_combo_and_pauses_rest_of_group():
    calls = []

    class _Conn:
        async def execute(self, sql, *args):
            calls.append((sql, args))
            return "UPDATE 0"

    result = await tm_probe._set_active_combo(_Conn(), ["x", "instagram"])

    assert result["active"] == ["instagram", "x"]
    # paused = every browser source except the combo
    assert set(result["paused"]) == set(DEFAULT_BROWSER_SOURCES) - {"x", "instagram"}
    # first UPDATE enables the combo, second disables the rest
    assert "enabled = true" in calls[0][0]
    assert set(calls[0][1][0]) == {"x", "instagram"}
    assert "enabled = false" in calls[1][0]
    assert "x" not in calls[1][1][0] and "instagram" not in calls[1][1][0]


@pytest.mark.asyncio
async def test_set_active_combo_never_touches_messaging_sources():
    touched = []

    class _Conn:
        async def execute(self, sql, *args):
            touched.extend(args[0])
            return "UPDATE 0"

    await tm_probe._set_active_combo(_Conn(), ["facebook"])
    for msg in ("telegram", "beeper", "whatsapp", "instagram_dm", "search", "website"):
        assert msg not in touched
