"""Rime TTS entity with incremental text and audio streaming."""

import logging
from collections.abc import AsyncGenerator, AsyncIterable, Mapping
from contextlib import AsyncExitStack, aclosing
from contextvars import ContextVar
from typing import Any

from homeassistant.components.tts import (
    DATA_COMPONENT,
    TextToSpeechEntity,
    TTSAudioRequest,
    TTSAudioResponse,
    TtsAudioType,
    Voice,
)
from homeassistant.const import CONF_LANGUAGE, CONF_MODEL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RimeConfigEntry
from .api import RimeAuthError, RimeError, validate_speed
from .const import CONF_FALLBACK_ENGINE, CONF_SPEED, CONF_VOICE, DEFAULT_SPEED, DOMAIN
from .fallback import RecordedText

_LOGGER = logging.getLogger(__name__)
_FALLBACK_ACTIVE: ContextVar[bool] = ContextVar("rime_fallback_active", default=False)


async def single_chunk(data: bytes) -> AsyncGenerator[bytes]:
    if data:
        yield data


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RimeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a TTS entity for this configuration."""
    async_add_entities([RimeTTSEntity(entry)])


class RimeTTSEntity(TextToSpeechEntity):
    """Expose Rime voices to Assist and tts.speak."""

    _attr_has_entity_name = True
    _attr_name = "Text-to-speech"
    _attr_supported_options = [CONF_VOICE, CONF_SPEED]

    def __init__(self, entry: RimeConfigEntry) -> None:
        self._entry = entry
        self._client = entry.runtime_data.client
        self._model = entry.options[CONF_MODEL]
        self._voices = entry.runtime_data.voices[self._model]
        self._voice = entry.options[CONF_VOICE]
        self._speed = entry.options.get(CONF_SPEED, DEFAULT_SPEED)
        self._attr_default_options = {CONF_SPEED: self._speed}
        self._attr_unique_id = entry.entry_id
        self._attr_default_language = entry.options[CONF_LANGUAGE]
        self._attr_supported_languages = list(self._voices)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Rime",
            model=self._model,
            name=f"Rime {self._model}",
            entry_type=DeviceEntryType.SERVICE,
        )

    def async_get_supported_voices(self, language: str) -> list[Voice]:
        """Default voice first, followed by the live catalog."""
        return [
            Voice(v, v.title())
            for v in sorted(
                self._voices.get(language, []), key=lambda v: (v != self._voice, v)
            )
        ]

    def _select_voice(self, language: str, options: Mapping[str, Any]) -> str:
        voices = self._voices.get(language, [])
        voice = options.get(CONF_VOICE)
        if voice is None:
            voice = self._voice if self._voice in voices else next(iter(voices), None)
        if not isinstance(voice, str) or voice not in voices:
            raise HomeAssistantError("Select a Rime voice available for this language")
        return voice

    async def _audio(
        self, text: AsyncIterable[str], language: str, options: Mapping[str, Any]
    ) -> AsyncGenerator[bytes]:
        voice = self._select_voice(language, options)
        try:
            speed = validate_speed(options.get(CONF_SPEED, self._speed))
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        try:
            async with aclosing(
                self._client.stream(text, self._model, language, voice, speed)
            ) as audio:
                async for chunk in audio:
                    yield chunk
        except RimeAuthError as err:
            self._entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                "Rime authentication failed; reauthenticate the integration"
            ) from err
        except RimeError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_stream_tts_audio(
        self, request: TTSAudioRequest
    ) -> TTSAudioResponse:
        """Choose the output format before handing audio to Home Assistant."""
        if not self._entry.options.get(CONF_FALLBACK_ENGINE):
            return TTSAudioResponse(
                "mp3",
                self._audio(request.message_gen, request.language, request.options),
            )
        self._select_voice(request.language, request.options)
        async with AsyncExitStack() as cleanup:
            recorded = RecordedText(request.message_gen)
            cleanup.push_async_callback(recorded.aclose)
            audio = await cleanup.enter_async_context(
                aclosing(self._audio(recorded, request.language, request.options))
            )
            try:
                first = await anext(audio)
            except StopAsyncIteration:
                return TTSAudioResponse("mp3", single_chunk(b""))
            except HomeAssistantError as err:
                if not isinstance(err.__cause__, RimeError):
                    raise
                message = await recorded.complete()
                extension, data = await self._fallback(message, request.language)
                return TTSAudioResponse(extension, single_chunk(data))
            recorded.stop_recording()
            stream_cleanup = cleanup.pop_all()

            async def output() -> AsyncGenerator[bytes]:
                async with stream_cleanup:
                    yield b""
                    yield first
                    async for chunk in audio:
                        yield chunk

            data_gen = output()
            # Prime cleanup so closing an unread response also releases the stream.
            await anext(data_gen)
            return TTSAudioResponse("mp3", data_gen)

    async def _fallback(self, message: str, language: str) -> tuple[str, bytes]:
        engine_id = self._entry.options[CONF_FALLBACK_ENGINE]
        if engine_id == self.entity_id or _FALLBACK_ACTIVE.get():
            raise HomeAssistantError("Recursive TTS fallback is not allowed")
        component = self.hass.data.get(DATA_COMPONENT)
        engine = component.get_entity(engine_id) if component else None
        if engine is None or not engine.available:
            raise HomeAssistantError("The fallback TTS engine is unavailable")
        token = _FALLBACK_ACTIVE.set(True)
        try:
            extension, data = await engine.async_internal_get_tts_audio(
                message,
                language
                if language in engine.supported_languages
                else engine.default_language,
                dict(engine.default_options or {}),
            )
        finally:
            _FALLBACK_ACTIVE.reset(token)
        if not extension or not data:
            raise HomeAssistantError("The fallback TTS engine returned no audio")
        _LOGGER.warning(
            "Rime failed before audio; used fallback TTS entity %s", engine_id
        )
        return extension, data

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict[str, Any]
    ) -> TtsAudioType:
        """Use the same transport for non-streaming announcements."""

        async def text() -> AsyncGenerator[str]:
            yield message

        response = await self.async_stream_tts_audio(
            TTSAudioRequest(language, options, text())
        )
        async with aclosing(response.data_gen):
            return response.extension, b"".join(
                [chunk async for chunk in response.data_gen]
            )
