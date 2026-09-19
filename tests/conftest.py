"""Fixtures for the custom integration."""

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

CATALOG = {
    "coda": {"en": ["astra", "boulder"], "es": ["alba"]},
    "mistv3": {"en": ["astra"]},
}


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    """Allow Home Assistant to discover this integration."""


@pytest.fixture
def entry():
    return MockConfigEntry(
        domain="rime_tts",
        title="Rime coda",
        data={"api_key": "test-key"},
        options={
            "model": "coda",
            "language": "en",
            "voice": "astra",
            "region": "us-west",
        },
    )


@pytest.fixture
def client():
    with (
        patch(
            "custom_components.rime_tts.api.RimeClient.voices",
            new_callable=AsyncMock,
            return_value=CATALOG,
        ),
        patch(
            "custom_components.rime_tts.api.RimeClient.validate", new_callable=AsyncMock
        ) as validate,
    ):
        yield validate
