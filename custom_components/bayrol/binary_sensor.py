"""Alarm binary sensors (one per measurement that reports an alarm flag)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    KEY_CHLORINE,
    KEY_PH,
    KEY_REDOX,
    KEY_SALT,
    KEY_TEMPERATURE,
)
from .coordinator import BayrolCoordinator
from .entity import BayrolEntity


@dataclass(frozen=True, kw_only=True)
class BayrolAlarmDescription(BinarySensorEntityDescription):
    measurement_key: str


_DESCRIPTIONS: tuple[BayrolAlarmDescription, ...] = (
    BayrolAlarmDescription(
        key="ph_alarm",
        measurement_key=KEY_PH,
        translation_key="ph_alarm",
        name="pH alarm",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    BayrolAlarmDescription(
        key="redox_alarm",
        measurement_key=KEY_REDOX,
        translation_key="redox_alarm",
        name="Redox alarm",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    BayrolAlarmDescription(
        key="temperature_alarm",
        measurement_key=KEY_TEMPERATURE,
        translation_key="temperature_alarm",
        name="Temperature alarm",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    BayrolAlarmDescription(
        key="salt_alarm",
        measurement_key=KEY_SALT,
        translation_key="salt_alarm",
        name="Salt alarm",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    BayrolAlarmDescription(
        key="chlorine_alarm",
        measurement_key=KEY_CHLORINE,
        translation_key="chlorine_alarm",
        name="Chlorine alarm",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: BayrolCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(_build_entities(coordinator))


def _build_entities(coordinator: BayrolCoordinator) -> Iterable[BayrolAlarmSensor]:
    data = coordinator.data or {}
    for cid in coordinator.controllers:
        controller_data = data.get(cid, {})
        for desc in _DESCRIPTIONS:
            # Only emit an alarm entity when the matching measurement is actually present.
            if desc.measurement_key in controller_data:
                yield BayrolAlarmSensor(coordinator, cid, desc)


class BayrolAlarmSensor(BayrolEntity, BinarySensorEntity):
    entity_description: BayrolAlarmDescription

    def __init__(
        self,
        coordinator: BayrolCoordinator,
        cid: str,
        description: BayrolAlarmDescription,
    ) -> None:
        super().__init__(coordinator, cid)
        self.entity_description = description
        self._attr_unique_id = f"{cid}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        value = self._data.get(f"{self.entity_description.measurement_key}_alarm")
        if value is None:
            return None
        return bool(value)
