"""Account usage reported by Rime, across models and applications."""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from . import RimeConfigEntry
from .api import RimeError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsageData:
    day: date
    today: int
    month: int


class RimeUsageCoordinator(DataUpdateCoordinator[UsageData]):
    """Poll usage independently of speech availability."""

    def __init__(self, hass: HomeAssistant, entry: RimeConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Rime account usage",
            config_entry=entry,
            update_interval=timedelta(minutes=15),
        )
        self.client = entry.runtime_data.client

    async def _async_update_data(self) -> UsageData:
        day = dt_util.utcnow().date()
        try:
            counts = await self.client.usage(day.replace(day=1), day)
        except RimeError as err:
            raise UpdateFailed(str(err)) from err
        return UsageData(day, counts.get(day, 0), sum(counts.values()))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RimeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = RimeUsageCoordinator(hass, entry)
    await coordinator.async_refresh()
    async_add_entities(
        [
            RimeUsageSensor(coordinator, entry, monthly=False),
            RimeUsageSensor(coordinator, entry, monthly=True),
        ]
    )


class RimeUsageSensor(CoordinatorEntity[RimeUsageCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "characters"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:counter"

    def __init__(
        self,
        coordinator: RimeUsageCoordinator,
        entry: RimeConfigEntry,
        *,
        monthly: bool,
    ) -> None:
        super().__init__(coordinator)
        self.monthly = monthly
        period = "month" if monthly else "today"
        self._attr_unique_id = f"{entry.entry_id}_account_characters_{period}"
        self._attr_name = (
            "Account characters this month" if monthly else "Account characters today"
        )
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def available(self) -> bool:
        return (
            super().available and self.coordinator.data.day == dt_util.utcnow().date()
        )

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return (
            self.coordinator.data.month if self.monthly else self.coordinator.data.today
        )

    @property
    def last_reset(self) -> datetime | None:
        if self.coordinator.data is None:
            return None
        day = self.coordinator.data.day
        if self.monthly:
            day = day.replace(day=1)
        return datetime.combine(day, time.min, tzinfo=UTC)
