"""Number entities for Bayrol setpoints (pH, Redox, temperature, …).

Backed by MQTT writes — the legacy ``data_json.php`` channel is unsupported
on SALT firmware and Bayrol expects setpoint changes through the cloud's MQTT
broker. Min/max bounds come from the controller itself in each ``v/<topic>``
payload, so we don't have to hard-code device-specific ranges.
"""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BayrolCoordinator, decode_num, get_mqtt_payload
from .entity import BayrolEntity
from .mqtt_client import BayrolMqttError
from .topics import NUM_TOPICS, NumTopic

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: BayrolCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BayrolNumber] = []
    for cid in coordinator.mqtt_clients:
        for item in NUM_TOPICS:
            if item.writable:
                entities.append(BayrolNumber(coordinator, cid, item))
    async_add_entities(entities)


class BayrolNumber(BayrolEntity, NumberEntity):
    """A writable numeric setpoint."""

    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: BayrolCoordinator,
        cid: str,
        item: NumTopic,
    ) -> None:
        super().__init__(coordinator, cid)
        self._item = item
        self._attr_unique_id = f"{cid}_num_{item.key}"
        self._attr_name = item.name
        if item.unit:
            self._attr_native_unit_of_measurement = item.unit
        if item.icon:
            self._attr_icon = item.icon
        # Choose a reasonable step from the scale factor: factor 0.1 → step 0.1.
        if item.factor < 1:
            self._attr_native_step = item.factor

    @property
    def _payload(self) -> dict | None:
        return get_mqtt_payload(self.coordinator.data, self._cid, self._item.topic)

    @property
    def native_value(self) -> float | None:
        return decode_num(self._payload, self._item)

    @property
    def native_min_value(self) -> float | None:
        payload = self._payload
        if payload and "min" in payload:
            try:
                return float(payload["min"]) * self._item.factor
            except (TypeError, ValueError):
                return None
        return None

    @property
    def native_max_value(self) -> float | None:
        payload = self._payload
        if payload and "max" in payload:
            try:
                return float(payload["max"]) * self._item.factor
            except (TypeError, ValueError):
                return None
        return None

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._payload is not None and self._cid in self.coordinator.mqtt_clients

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.async_write_num(self._cid, self._item, value)
        except BayrolMqttError as err:
            raise HomeAssistantError(f"Bayrol MQTT write failed: {err}") from err
