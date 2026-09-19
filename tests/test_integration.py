"""Home Assistant lifecycle, flows, voice selection, and audio adapters."""

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.components.tts import TTSAudioRequest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError

from custom_components.rime_tts.api import RimeAuthError, RimeError
from custom_components.rime_tts.tts import RimeTTSEntity


async def test_setup_unload(hass, entry, client):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert any(
        state.entity_id.startswith("tts.rime") for state in hass.states.async_all()
    )
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (RimeAuthError("bad key"), ConfigEntryState.SETUP_ERROR),
        (RimeError("offline"), ConfigEntryState.SETUP_RETRY),
    ],
)
async def test_setup_failure(hass, entry, client, error, state):
    entry.add_to_hass(hass)
    client.side_effect = error
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is state


async def test_config_flow(hass, client):
    with patch("custom_components.rime_tts.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            "rime_tts", context={"source": "user"}
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "api_key": "test-key",
                "model": "coda",
                "language": "en",
                "region": "us-east",
            },
        )
        assert result["step_id"] == "voice"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"voice": "astra"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"] == {"api_key": "test-key"}
        assert result["options"] == {
            "model": "coda",
            "language": "en",
            "region": "us-east",
            "voice": "astra",
        }
        await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("error", "expected"),
    [(RimeAuthError("no"), "invalid_auth"), (RimeError("no"), "cannot_connect")],
)
async def test_config_errors(hass, client, error, expected):
    client.side_effect = error
    result = await hass.config_entries.flow.async_init(
        "rime_tts",
        context={"source": "user"},
        data={
            "api_key": "test-key",
            "model": "coda",
            "language": "en",
            "region": "us-west",
        },
    )
    assert result["errors"] == {"base": expected}


async def test_invalid_language(hass, client):
    result = await hass.config_entries.flow.async_init(
        "rime_tts",
        context={"source": "user"},
        data={
            "api_key": "test-key",
            "model": "mistv3",
            "language": "es",
            "region": "us-east",
        },
    )
    assert result["errors"] == {"language": "invalid_language"}
    client.assert_not_called()


async def test_options(hass, entry, client):
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"model": "coda", "language": "es", "region": "us-east"}
    )
    assert result["step_id"] == "voice"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"voice": "alba"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["voice"] == "alba"


async def test_reauth(hass, entry, client):
    entry.add_to_hass(hass)
    with patch(
        "homeassistant.config_entries.ConfigEntries.async_reload", return_value=True
    ):
        result = await hass.config_entries.flow.async_init(
            "rime_tts",
            context={"source": "reauth", "entry_id": entry.entry_id},
            data=entry.data,
        )
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"api_key": "replacement-key"}
        )
        assert result["reason"] == "reauth_successful"
        assert entry.data["api_key"] == "replacement-key"


async def test_tts_adapters(hass, entry, client):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    entity = RimeTTSEntity(entry)
    entity.hass = hass
    assert entity.default_language == "en"
    assert [v.voice_id for v in entity.async_get_supported_voices("en")] == [
        "astra",
        "boulder",
    ]
    calls = []

    async def stream(text, model, language, voice):
        calls.append((model, language, voice))
        async for part in text:
            yield part.encode()

    with patch.object(entry.runtime_data.client, "stream", side_effect=stream):
        assert await entity.async_get_tts_audio("Hello.", "en", {}) == (
            "mp3",
            b"Hello.",
        )
        assert await entity.async_get_tts_audio("Hola.", "es", {}) == ("mp3", b"Hola.")
        assert calls[-1] == ("coda", "es", "alba")

        async def text():
            yield "Streaming."

        response = await entity.async_stream_tts_audio(
            TTSAudioRequest("en", {"voice": "boulder"}, text())
        )
        assert response.extension == "mp3"
        assert [chunk async for chunk in response.data_gen] == [b"Streaming."]
        assert calls[-1][-1] == "boulder"
    with pytest.raises(HomeAssistantError):
        await entity.async_get_tts_audio("Hello.", "en", {"voice": "alba"})


async def test_entity_stream_closes_transport(hass, entry, client):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    entity = RimeTTSEntity(entry)
    closed = asyncio.Event()

    async def stream(*args):
        try:
            yield b"audio"
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def text():
        yield "Hello."

    with patch.object(entry.runtime_data.client, "stream", side_effect=stream):
        response = await entity.async_stream_tts_audio(
            TTSAudioRequest("en", {}, text())
        )
        assert await anext(response.data_gen) == b"audio"
        await response.data_gen.aclose()
        assert closed.is_set()
