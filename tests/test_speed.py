"""Speaking speed uses playback multipliers, not duration multipliers."""

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
import voluptuous as vol
from aiohttp import web
from homeassistant.components.tts import TTSAudioRequest
from homeassistant.exceptions import HomeAssistantError

from custom_components.rime_tts.api import RimeClient
from custom_components.rime_tts.config_flow import selection_schema
from custom_components.rime_tts.const import WS_URLS
from custom_components.rime_tts.tts import RimeTTSEntity


@pytest.mark.parametrize("model", ["coda", "mistv3"])
@pytest.mark.parametrize("speed", [0.4, 1.0, 1.1, 2.5])
async def test_websocket_speed(
    aiohttp_server, socket_enabled, monkeypatch, model, speed
):
    received = []

    async def handler(request):
        received.append(dict(request.query))
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            if message.json().get("operation") == "eos":
                await ws.send_json({"type": "chunk", "data": "YWJj"})
                await ws.close()
                break
        return ws

    app = web.Application()
    app.router.add_get("/ws3", handler)
    server = await aiohttp_server(app)
    monkeypatch.setitem(WS_URLS, "us-west", str(server.make_url("/ws3")))

    async def text():
        yield "Hello."

    async with aiohttp.ClientSession() as session:
        chunks = [
            chunk
            async for chunk in RimeClient(session, "test-key").stream(
                text(), model, "en", "astra", speed
            )
        ]
    assert chunks == [b"abc"]
    assert received[0]["modelId"] == model
    assert float(received[0]["timeScaleFactor"]) == pytest.approx(1 / speed)


@pytest.mark.parametrize(
    "speed", [0, -1, 0.39, 2.51, float("nan"), float("inf"), True, "fast", None]
)
async def test_invalid_speed_never_connects(speed):
    session = MagicMock()

    async def text():
        yield "Hello."

    with pytest.raises(ValueError, match="Speaking speed"):
        await anext(
            RimeClient(session, "test-key").stream(text(), "coda", "en", "astra", speed)
        )
    session.ws_connect.assert_not_called()


async def test_setup_and_options_save_speed(hass, client):
    with patch("custom_components.rime_tts.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            "rime_tts", context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "api_key": "test-key",
                "model": "coda",
                "language": "en",
                "region": "us-west",
                "speed": 1.1,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"voice": "astra"}
        )
        entry = result["result"]
        assert entry.options["speed"] == 1.1
        result = await hass.config_entries.options.async_init(entry.entry_id)
        settings = {"model": "coda", "language": "en", "region": "us-west"}
        assert result["data_schema"](settings)["speed"] == 1.1
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], settings | {"speed": 1.2}
        )
        await hass.config_entries.options.async_configure(
            result["flow_id"], {"voice": "astra"}
        )
        assert entry.options["speed"] == 1.2
        await hass.async_block_till_done()


@pytest.mark.parametrize("speed", [0.3, 2.6])
def test_slider_rejects_out_of_range(speed):
    schema = selection_schema({"coda": {"en": ["astra"]}}, {})
    with pytest.raises(vol.Invalid):
        schema({"model": "coda", "language": "en", "region": "us-west", "speed": speed})


@pytest.mark.parametrize("configured", [None, 1.1])
async def test_entity_speed_defaults_and_overrides(hass, entry, client, configured):
    entry.add_to_hass(hass)
    if configured is not None:
        hass.config_entries.async_update_entry(
            entry, options=dict(entry.options) | {"speed": configured}
        )
    assert await hass.config_entries.async_setup(entry.entry_id)
    entity = RimeTTSEntity(entry)
    entity.hass = hass
    calls = []

    async def stream(text, model, language, voice, speed):
        calls.append(speed)
        async for _ in text:
            yield b"audio"

    expected = 1.0 if configured is None else configured
    assert entity.default_options == {"speed": expected}
    assert "speed" in entity.supported_options
    with patch.object(entry.runtime_data.client, "stream", side_effect=stream):
        assert await entity.async_get_tts_audio("Hello.", "en", {}) == ("mp3", b"audio")

        async def text():
            yield "Hello."

        response = await entity.async_stream_tts_audio(
            TTSAudioRequest("en", {"speed": 1.2}, text())
        )
        assert [chunk async for chunk in response.data_gen] == [b"audio"]
    assert calls == [expected, 1.2]
    assert entity.default_options == {"speed": expected}


async def test_invalid_service_speed_does_not_fallback(hass, entry, client):
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options=dict(entry.options) | {"fallback_engine": "tts.backup"}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    entity = RimeTTSEntity(entry)
    entity.hass = hass
    with patch.object(entity, "_fallback", new_callable=AsyncMock) as fallback:
        with pytest.raises(HomeAssistantError, match="Speaking speed"):
            await entity.async_get_tts_audio("Hello.", "en", {"speed": 0})
        fallback.assert_not_awaited()
