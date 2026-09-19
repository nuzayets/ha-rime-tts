"""Rime TTS entity with incremental text and audio streaming."""

from collections.abc import AsyncGenerator, AsyncIterable, Mapping
from contextlib import aclosing
from typing import Any

from homeassistant.components.tts import (
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
from .api import RimeAuthError, RimeError
from .const import CONF_VOICE, DOMAIN


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
    _attr_supported_options = [CONF_VOICE]

    def __init__(self, entry: RimeConfigEntry) -> None:
        self._entry = entry
        self._client = entry.runtime_data.client
        self._model = entry.options[CONF_MODEL]
        self._voices = entry.runtime_data.voices[self._model]
        self._voice = entry.options[CONF_VOICE]
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
            async with aclosing(
                self._client.stream(text, self._model, language, voice)
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
        """Return audio without waiting for the complete input text."""
        return TTSAudioResponse(
            "mp3", self._audio(request.message_gen, request.language, request.options)
        )

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict[str, Any]
    ) -> TtsAudioType:
        """Use the same transport for non-streaming announcements."""

        async def text() -> AsyncGenerator[str]:
            yield message

        return "mp3", b"".join(
            [chunk async for chunk in self._audio(text(), language, options)]
        )
