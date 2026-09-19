"""Authoritative usage, UTC boundaries, and failure isolation."""

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.rime_tts.api import RimeClient, RimeError
from custom_components.rime_tts.sensor import RimeUsageCoordinator, RimeUsageSensor


def usage_session(payload):
    session = MagicMock()
    response = session.get.return_value.__aenter__.return_value
    response.raise_for_status = MagicMock()
    response.json = AsyncMock(return_value=payload)
    return session


async def test_usage_api():
    session = usage_session(
        {
            "data": [
                {"day": "2026-09-01", "breakdown": []},
                {
                    "day": "2026-09-19",
                    "breakdown": [
                        {"model": "coda", "environment": "cloud", "charCount": 30},
                        {"model": "mistv3", "environment": "onprem", "charCount": 12},
                    ],
                },
            ]
        }
    )
    assert await RimeClient(session, "test-key").usage(
        date(2026, 9, 1), date(2026, 9, 19)
    ) == {date(2026, 9, 1): 0, date(2026, 9, 19): 42}
    assert session.get.call_args.args == (
        "https://optimize.rime.ai/usage/detailed-history",
    )
    assert session.get.call_args.kwargs["params"] == {
        "startDate": "2026-09-01",
        "endDate": "2026-09-19",
    }
    assert session.get.call_args.kwargs["headers"] == {
        "Authorization": "Bearer test-key"
    }


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"data": None},
        {"data": [None]},
        {"data": [{"day": "not-a-date", "breakdown": []}]},
        {"data": [{"day": "2026-09-19", "breakdown": [{"charCount": True}]}]},
        {"data": [{"day": "2026-09-19", "breakdown": [{"charCount": -1}]}]},
        {"data": [{"day": "2026-09-19", "breakdown": [{"charCount": "10"}]}]},
    ],
)
async def test_invalid_usage(payload):
    with pytest.raises(RimeError):
        await RimeClient(usage_session(payload), "test-key").usage(
            date(2026, 9, 1), date(2026, 9, 19)
        )


async def test_usage_http_failure():
    session = usage_session({})
    session.get.return_value.__aenter__.side_effect = aiohttp.ClientError("offline")
    with pytest.raises(RimeError, match="Unable to load"):
        await RimeClient(session, "test-key").usage(date(2026, 9, 1), date(2026, 9, 19))


async def test_usage_failure_does_not_disable_tts(hass, entry, client):
    entry.add_to_hass(hass)
    with patch.object(RimeClient, "usage", side_effect=RimeError("Usage unavailable")):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert any(
        state.entity_id.startswith("tts.rime") for state in hass.states.async_all()
    )
    states = [
        state
        for state in hass.states.async_all()
        if state.entity_id.startswith("sensor.rime")
    ]
    assert len(states) == 2
    assert all(state.state == "unavailable" for state in states)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_usage_sensors_rollover_and_recovery(hass, entry, client, freezer):
    freezer.move_to("2026-09-30T23:59:00+00:00")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = RimeUsageCoordinator(hass, entry)
    daily = RimeUsageSensor(coordinator, entry, monthly=False)
    monthly = RimeUsageSensor(coordinator, entry, monthly=True)
    with patch.object(
        entry.runtime_data.client,
        "usage",
        return_value={date(2026, 9, 1): 90, date(2026, 9, 30): 10},
    ) as usage:
        await coordinator.async_refresh()
        usage.assert_awaited_once_with(date(2026, 9, 1), date(2026, 9, 30))
    assert daily.available and monthly.available
    assert daily.native_value == 10 and monthly.native_value == 100
    assert daily.last_reset == datetime(2026, 9, 30, tzinfo=UTC)
    assert monthly.last_reset == datetime(2026, 9, 1, tzinfo=UTC)
    freezer.move_to("2026-10-01T00:00:00+00:00")
    assert not daily.available and not monthly.available
    with patch.object(
        entry.runtime_data.client, "usage", side_effect=RimeError("offline")
    ):
        await coordinator.async_refresh()
    assert not daily.available
    with patch.object(
        entry.runtime_data.client, "usage", return_value={date(2026, 10, 1): 3}
    ):
        await coordinator.async_refresh()
    assert daily.available and monthly.available
    assert daily.native_value == monthly.native_value == 3
    assert daily.last_reset == monthly.last_reset == datetime(2026, 10, 1, tzinfo=UTC)
