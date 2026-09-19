"""UI setup, options, and reauthentication for Rime."""

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_API_KEY, CONF_LANGUAGE, CONF_MODEL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import RimeConfigEntry
from .api import RimeAuthError, RimeClient, RimeError, VoiceCatalog
from .const import (
    CONF_FALLBACK_ENGINE,
    CONF_REGION,
    CONF_SPEED,
    CONF_VOICE,
    DEFAULT_MODEL,
    DEFAULT_REGION,
    DEFAULT_SPEED,
    DOMAIN,
    MAX_SPEED,
    MIN_SPEED,
    WS_URLS,
)


def selection_schema(
    catalog: VoiceCatalog, defaults: dict[str, Any], *, api_key: bool = False
) -> vol.Schema:
    """Select a model and language before displaying the matching voices."""
    fields = {
        vol.Required(
            CONF_REGION, default=defaults.get(CONF_REGION, DEFAULT_REGION)
        ): SelectSelector(SelectSelectorConfig(options=list(WS_URLS))),
        vol.Required(
            CONF_MODEL, default=defaults.get(CONF_MODEL, DEFAULT_MODEL)
        ): SelectSelector(SelectSelectorConfig(options=list(catalog))),
        vol.Required(
            CONF_LANGUAGE, default=defaults.get(CONF_LANGUAGE, "en")
        ): SelectSelector(
            SelectSelectorConfig(
                options=sorted({lang for langs in catalog.values() for lang in langs})
            )
        ),
        vol.Required(
            CONF_SPEED, default=defaults.get(CONF_SPEED, DEFAULT_SPEED)
        ): NumberSelector(
            NumberSelectorConfig(
                min=MIN_SPEED,
                max=MAX_SPEED,
                step=0.05,
                mode=NumberSelectorMode.SLIDER,
                unit_of_measurement="×",
            )
        ),
        vol.Optional(
            CONF_FALLBACK_ENGINE,
            description={"suggested_value": defaults.get(CONF_FALLBACK_ENGINE)},
        ): EntitySelector(EntitySelectorConfig(domain="tts")),
    }
    if api_key:
        fields[vol.Required(CONF_API_KEY)] = TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
    return vol.Schema(fields)


def voice_schema(voices: list[str], default: str | None = None) -> vol.Schema:
    """Show voices for the selected language and model."""
    return vol.Schema(
        {
            vol.Required(
                CONF_VOICE, default=default if default in voices else voices[0]
            ): SelectSelector(SelectSelectorConfig(options=voices))
        }
    )


class RimeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a Rime account and preferred voice."""

    VERSION = 1

    def __init__(self) -> None:
        self.catalog: VoiceCatalog = {}
        self.settings: dict[str, Any] = {}
        self.api_key = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if not self.catalog:
            try:
                self.catalog = await RimeClient(
                    async_get_clientsession(self.hass), ""
                ).voices()
            except RimeError:
                return self.async_abort(reason="cannot_connect")
        if user_input is not None:
            model, language = user_input[CONF_MODEL], user_input[CONF_LANGUAGE]
            if language not in self.catalog.get(model, {}):
                errors[CONF_LANGUAGE] = "invalid_language"
            else:
                self.api_key = user_input[CONF_API_KEY].strip()
                self.settings = {
                    CONF_MODEL: model,
                    CONF_LANGUAGE: language,
                    CONF_REGION: user_input[CONF_REGION],
                    CONF_SPEED: user_input.get(CONF_SPEED, DEFAULT_SPEED),
                }
                if user_input.get(CONF_FALLBACK_ENGINE):
                    self.settings[CONF_FALLBACK_ENGINE] = user_input[
                        CONF_FALLBACK_ENGINE
                    ]
                voices = self.catalog[model][language]
                try:
                    await RimeClient(
                        async_get_clientsession(self.hass),
                        self.api_key,
                        self.settings[CONF_REGION],
                    ).validate(model, language, voices[0])
                except RimeAuthError:
                    errors["base"] = "invalid_auth"
                except RimeError:
                    errors["base"] = "cannot_connect"
                else:
                    return await self.async_step_voice()
        return self.async_show_form(
            step_id="user",
            data_schema=selection_schema(self.catalog, self.settings, api_key=True),
            errors=errors,
        )

    async def async_step_voice(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        voices = self.catalog[self.settings[CONF_MODEL]][self.settings[CONF_LANGUAGE]]
        errors = {}
        if user_input is not None:
            if user_input[CONF_VOICE] not in voices:
                errors[CONF_VOICE] = "invalid_voice"
            else:
                return self.async_create_entry(
                    title=f"Rime {self.settings[CONF_MODEL]}",
                    data={CONF_API_KEY: self.api_key},
                    options=self.settings | {CONF_VOICE: user_input[CONF_VOICE]},
                )
        return self.async_show_form(
            step_id="voice", data_schema=voice_schema(voices, "astra"), errors=errors
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            client = RimeClient(
                async_get_clientsession(self.hass), api_key, entry.options[CONF_REGION]
            )
            try:
                await client.validate(
                    entry.options[CONF_MODEL],
                    entry.options[CONF_LANGUAGE],
                    entry.options[CONF_VOICE],
                )
            except RimeAuthError:
                errors["base"] = "invalid_auth"
            except RimeError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: api_key}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: RimeConfigEntry) -> OptionsFlow:
        return RimeOptionsFlow()


class RimeOptionsFlow(OptionsFlow):
    """Change the default model, language, and voice."""

    def __init__(self) -> None:
        self.catalog: VoiceCatalog = {}
        self.settings: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if not self.catalog:
            try:
                self.catalog = await RimeClient(
                    async_get_clientsession(self.hass),
                    self.config_entry.data[CONF_API_KEY],
                    self.config_entry.options[CONF_REGION],
                ).voices()
            except RimeError:
                return self.async_abort(reason="cannot_connect")
        if user_input is not None:
            if user_input[CONF_LANGUAGE] not in self.catalog.get(
                user_input[CONF_MODEL], {}
            ):
                errors[CONF_LANGUAGE] = "invalid_language"
            else:
                self.settings = user_input
                return await self.async_step_voice()
        return self.async_show_form(
            step_id="init",
            data_schema=selection_schema(self.catalog, dict(self.config_entry.options)),
            errors=errors,
        )

    async def async_step_voice(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        voices = self.catalog[self.settings[CONF_MODEL]][self.settings[CONF_LANGUAGE]]
        errors = {}
        if user_input is not None:
            if user_input[CONF_VOICE] not in voices:
                errors[CONF_VOICE] = "invalid_voice"
            else:
                return self.async_create_entry(
                    title="", data=self.settings | {CONF_VOICE: user_input[CONF_VOICE]}
                )
        return self.async_show_form(
            step_id="voice",
            data_schema=voice_schema(voices, self.config_entry.options.get(CONF_VOICE)),
            errors=errors,
        )
