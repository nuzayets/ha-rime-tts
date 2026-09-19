"""Fallback preserves text, audio formats, and cancellation boundaries."""

import asyncio
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from homeassistant.components.tts import DATA_COMPONENT, TTSAudioRequest
from homeassistant.exceptions import HomeAssistantError

from custom_components.rime_tts.api import RimeAuthError, RimeError
from custom_components.rime_tts.const import WS_URLS
from custom_components.rime_tts.fallback import RecordedText
from custom_components.rime_tts.tts import RimeTTSEntity


@pytest.fixture
async def fallback(hass, entry, client):
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options=dict(entry.options) | {"fallback_engine": "tts.backup"}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    entity = RimeTTSEntity(entry)
    entity.hass = hass
    entity.entity_id = "tts.test_rime"
    backup = SimpleNamespace(
        available=True,
        supported_languages=["en-GB", "es"],
        default_language="en-GB",
        default_options={"voice": "backup"},
        async_internal_get_tts_audio=AsyncMock(return_value=("wav", b"backup audio")),
    )
    with patch.object(hass.data[DATA_COMPONENT], "get_entity", return_value=backup):
        yield entity, backup


@pytest.mark.parametrize("error", [RimeError("offline"), RimeAuthError("bad key")])
async def test_fallback_without_consuming_input(fallback, entry, error):
    entity, backup = fallback

    async def failing(*args):
        raise error
        yield

    with (
        patch.object(entry.runtime_data.client, "stream", side_effect=failing),
        patch.object(entry, "async_start_reauth") as reauth,
    ):
        assert await entity.async_get_tts_audio(
            "Hello world.", "en", {"voice": "astra"}
        ) == ("wav", b"backup audio")
        backup.async_internal_get_tts_audio.assert_awaited_once_with(
            "Hello world.", "en-GB", {"voice": "backup"}
        )
        assert reauth.called == isinstance(error, RimeAuthError)


async def test_pending_input_survives_rime_sender_cancellation(fallback, entry):
    entity, backup = fallback
    waiting = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    source_closed = asyncio.Event()

    async def text():
        try:
            yield "Hello "
            waiting.set()
            await release.wait()
            yield "world."
        finally:
            source_closed.set()

    async def failing(source, *args):
        assert await anext(source) == "Hello "
        reader = asyncio.create_task(anext(source))
        await waiting.wait()
        reader.cancel()
        with suppress(asyncio.CancelledError):
            await reader
        cancelled.set()
        raise RimeError("connection dropped")
        yield

    with patch.object(entry.runtime_data.client, "stream", side_effect=failing):
        task = asyncio.create_task(
            entity.async_stream_tts_audio(TTSAudioRequest("en", {}, text()))
        )
        async with asyncio.timeout(2):
            await cancelled.wait()
            assert not source_closed.is_set()
            release.set()
            response = await task
        assert response.extension == "wav"
        assert [part async for part in response.data_gen] == [b"backup audio"]
    backup.async_internal_get_tts_audio.assert_awaited_once_with(
        "Hello world.", "en-GB", {"voice": "backup"}
    )
    assert source_closed.is_set()


async def test_no_fallback_after_audio(fallback, entry):
    entity, backup = fallback

    async def stream(source, *args):
        await anext(source)
        yield b"first"
        raise RimeError("interrupted")

    async def text():
        yield "Hello."

    with patch.object(entry.runtime_data.client, "stream", side_effect=stream):
        response = await entity.async_stream_tts_audio(
            TTSAudioRequest("en", {}, text())
        )
        assert response.extension == "mp3"
        assert await anext(response.data_gen) == b"first"
        with pytest.raises(HomeAssistantError, match="interrupted"):
            await anext(response.data_gen)
    backup.async_internal_get_tts_audio.assert_not_awaited()


@pytest.mark.parametrize("read_audio", [False, True])
async def test_close_response_releases_source_and_socket(fallback, entry, read_audio):
    entity, backup = fallback
    source_closed, socket_closed = asyncio.Event(), asyncio.Event()

    async def text():
        try:
            yield "Hello."
            await asyncio.Event().wait()
        finally:
            source_closed.set()

    async def stream(source, *args):
        try:
            await anext(source)
            yield b"first"
            await asyncio.Event().wait()
        finally:
            socket_closed.set()

    with patch.object(entry.runtime_data.client, "stream", side_effect=stream):
        response = await entity.async_stream_tts_audio(
            TTSAudioRequest("en", {}, text())
        )
        if read_audio:
            assert await anext(response.data_gen) == b"first"
        await response.data_gen.aclose()
    assert source_closed.is_set() and socket_closed.is_set()
    backup.async_internal_get_tts_audio.assert_not_awaited()


async def test_no_fallback_for_invalid_voice(fallback):
    entity, backup = fallback
    with pytest.raises(HomeAssistantError, match="Select a Rime voice"):
        await entity.async_get_tts_audio("Hello.", "en", {"voice": "invalid"})
    backup.async_internal_get_tts_audio.assert_not_awaited()


async def test_no_fallback_for_input_failure(fallback, entry):
    entity, backup = fallback

    async def text():
        raise ValueError("LLM stream failed")
        yield

    async def stream(source, *args):
        async for chunk in source:
            yield chunk.encode()

    with patch.object(entry.runtime_data.client, "stream", side_effect=stream):
        with pytest.raises(ValueError, match="LLM stream failed"):
            await entity.async_stream_tts_audio(TTSAudioRequest("en", {}, text()))
    backup.async_internal_get_tts_audio.assert_not_awaited()


async def test_fallback_failure_and_cycles(fallback, entry):
    entity, backup = fallback
    backup.available = False
    with pytest.raises(HomeAssistantError, match="unavailable"):
        await entity._fallback("Hello.", "en")
    backup.available = True
    backup.async_internal_get_tts_audio.return_value = (None, None)
    with pytest.raises(HomeAssistantError, match="no audio"):
        await entity._fallback("Hello.", "en")

    async def recursive(message, language, options):
        return await entity._fallback(message, language)

    backup.async_internal_get_tts_audio.side_effect = recursive
    with pytest.raises(HomeAssistantError, match="Recursive"):
        await entity._fallback("Hello.", "en")
    backup.async_internal_get_tts_audio.side_effect = None
    backup.async_internal_get_tts_audio.return_value = ("wav", b"ok")
    assert await entity._fallback("Hola.", "es") == ("wav", b"ok")


async def test_cancellation_before_audio_does_not_fallback(fallback, entry):
    entity, backup = fallback
    waiting, closed = asyncio.Event(), asyncio.Event()

    async def text():
        try:
            waiting.set()
            await asyncio.Event().wait()
            yield "Hello."
        finally:
            closed.set()

    async def stream(source, *args):
        async for chunk in source:
            yield chunk.encode()

    with patch.object(entry.runtime_data.client, "stream", side_effect=stream):
        task = asyncio.create_task(
            entity.async_stream_tts_audio(TTSAudioRequest("en", {}, text()))
        )
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed.is_set()
    backup.async_internal_get_tts_audio.assert_not_awaited()


async def test_self_fallback_is_rejected(fallback):
    entity, backup = fallback
    entity.entity_id = "tts.backup"
    with pytest.raises(HomeAssistantError, match="Recursive"):
        await entity._fallback("Hello.", "en")
    backup.async_internal_get_tts_audio.assert_not_awaited()


async def test_real_websocket_failure_preserves_unfinished_text(
    fallback, aiohttp_server, socket_enabled, monkeypatch
):
    entity, backup = fallback
    waiting, release, fallback_started, closed = (asyncio.Event() for _ in range(4))

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.receive_json()
        await waiting.wait()
        await ws.send_json({"type": "error", "message": "service unavailable"})
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/ws3", handler)
    server = await aiohttp_server(app)
    monkeypatch.setitem(WS_URLS, "us-west", str(server.make_url("/ws3")))
    original_complete = RecordedText.complete

    async def complete(recorded):
        fallback_started.set()
        return await original_complete(recorded)

    monkeypatch.setattr(RecordedText, "complete", complete)

    async def text():
        try:
            yield "Hello "
            waiting.set()
            await release.wait()
            yield "world."
        finally:
            closed.set()

    task = asyncio.create_task(
        entity.async_stream_tts_audio(TTSAudioRequest("en", {}, text()))
    )
    async with asyncio.timeout(5):
        await fallback_started.wait()
        assert not closed.is_set()
        release.set()
        response = await task
    assert response.extension == "wav"
    assert [chunk async for chunk in response.data_gen] == [b"backup audio"]
    backup.async_internal_get_tts_audio.assert_awaited_once_with(
        "Hello world.", "en-GB", {"voice": "backup"}
    )
    assert closed.is_set()


async def test_options_enable_and_clear_fallback(hass, entry, client):
    entry.add_to_hass(hass)
    for selected in ({"fallback_engine": "tts.backup"}, {}):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"model": "coda", "language": "en", "region": "us-west"} | selected,
        )
        await hass.config_entries.options.async_configure(
            result["flow_id"], {"voice": "astra"}
        )
        assert entry.options.get("fallback_engine") == selected.get("fallback_engine")
