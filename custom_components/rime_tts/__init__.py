"""Rime text-to-speech integration."""

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_LANGUAGE, CONF_MODEL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import RimeAuthError, RimeClient, RimeError, VoiceCatalog
from .const import CONF_REGION, CONF_VOICE


@dataclass
class RimeData:
    """Runtime resources for one configured model."""

    client: RimeClient
    voices: VoiceCatalog


type RimeConfigEntry = ConfigEntry[RimeData]


async def async_setup_entry(hass: HomeAssistant, entry: RimeConfigEntry) -> bool:
    """Load voices and verify credentials before setting up TTS."""
    client = RimeClient(
        async_get_clientsession(hass),
        entry.data[CONF_API_KEY],
        entry.options[CONF_REGION],
    )
    try:
        voices = await client.voices()
        model, language, voice = (
            entry.options[k] for k in (CONF_MODEL, CONF_LANGUAGE, CONF_VOICE)
        )
        if voice not in voices.get(model, {}).get(language, []):
            raise RimeError(
                "Configured voice is no longer available; update integration options"
            )
        await client.validate(model, language, voice)
    except RimeAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except RimeError as err:
        raise ConfigEntryNotReady(str(err)) from err
    entry.runtime_data = RimeData(client, voices)
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.TTS])
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_reload_entry(hass: HomeAssistant, entry: RimeConfigEntry) -> None:
    """Apply updated options."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: RimeConfigEntry) -> bool:
    """Unload the TTS entity."""
    return await hass.config_entries.async_unload_platforms(entry, [Platform.TTS])
