"""Bayrol sensor entities (pH, Redox/ORP, Temperature, Salt, Chlorine)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricPotential, UnitOfTemperature
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
from .coordinator import BayrolCoordinator, decode_num, get_mqtt_payload
from .entity import BayrolEntity
from .topics import NUM_TOPICS, NumTopic


@dataclass(frozen=True, kw_only=True)
class BayrolSensorDescription(SensorEntityDescription):
    """Sensor description with the data-dict key it reads from."""

    data_key: str


_DESCRIPTIONS: tuple[BayrolSensorDescription, ...] = (
    BayrolSensorDescription(
        key=KEY_PH,
        data_key=KEY_PH,
        translation_key="ph",
        name="pH",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:ph",
    ),
    BayrolSensorDescription(
        key=KEY_REDOX,
        data_key=KEY_REDOX,
        translation_key="redox",
        name="Redox",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        suggested_display_precision=0,
        icon="mdi:flash",
    ),
    BayrolSensorDescription(
        key=KEY_TEMPERATURE,
        data_key=KEY_TEMPERATURE,
        translation_key="temperature",
        name="Water temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    BayrolSensorDescription(
        key=KEY_SALT,
        data_key=KEY_SALT,
        translation_key="salt",
        name="Salt",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="g/L",
        suggested_display_precision=2,
        icon="mdi:shaker-outline",
    ),
    BayrolSensorDescription(
        key=KEY_CHLORINE,
        data_key=KEY_CHLORINE,
        translation_key="chlorine",
        name="Chlorine",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="mg/L",
        suggested_display_precision=2,
        icon="mdi:water-percent",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: BayrolCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BayrolEntity] = list(_build_http_sensors(coordinator))
    entities.extend(_build_mqtt_sensors(coordinator))
    async_add_entities(entities)


def _build_http_sensors(coordinator: BayrolCoordinator) -> Iterable[BayrolSensor]:
    data = coordinator.data or {}
    for cid in coordinator.controllers:
        controller_data = data.get(cid, {})
        for desc in _DESCRIPTIONS:
            # Only create sensors for measurements this controller actually reports.
            # Re-runs after a software update that exposes new keys would require a
            # reload of the config entry — that's a fair tradeoff vs. spawning empty
            # entities for every device class.
            if desc.data_key in controller_data:
                yield BayrolSensor(coordinator, cid, desc)


# MQTT-only sensors that don't have an HTTP equivalent — chiefly the boost
# countdown. Adding more here is cheap; just list extra read-only NumTopic
# keys you want to surface as native HA sensors.
_MQTT_SENSOR_KEYS: frozenset[str] = frozenset({"boost_remaining_min"})


def _build_mqtt_sensors(coordinator: BayrolCoordinator) -> Iterable[BayrolMqttNumSensor]:
    if not coordinator.mqtt_clients:
        return
    items_by_key = {item.key: item for item in NUM_TOPICS}
    for cid in coordinator.mqtt_clients:
        for key in _MQTT_SENSOR_KEYS:
            if item := items_by_key.get(key):
                yield BayrolMqttNumSensor(coordinator, cid, item)


class BayrolSensor(BayrolEntity, SensorEntity):
    entity_description: BayrolSensorDescription

    def __init__(
        self,
        coordinator: BayrolCoordinator,
        cid: str,
        description: BayrolSensorDescription,
    ) -> None:
        super().__init__(coordinator, cid)
        self.entity_description = description
        self._attr_unique_id = f"{cid}_{description.key}"

    @property
    def native_value(self) -> float | None:
        value = self._data.get(self.entity_description.data_key)
        return float(value) if isinstance(value, (int, float)) else None


class BayrolMqttNumSensor(BayrolEntity, SensorEntity):
    """Read-only sensor backed by an MQTT NumTopic (e.g. boost countdown)."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: BayrolCoordinator,
        cid: str,
        item: NumTopic,
    ) -> None:
        super().__init__(coordinator, cid)
        self._item = item
        self._attr_unique_id = f"{cid}_mqtt_{item.key}"
        self._attr_name = item.name
        if item.unit:
            self._attr_native_unit_of_measurement = item.unit
        if item.icon:
            self._attr_icon = item.icon

    @property
    def native_value(self) -> float | None:
        payload = get_mqtt_payload(self.coordinator.data, self._cid, self._item.topic)
        return decode_num(payload, self._item)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._cid in self.coordinator.mqtt_clients
