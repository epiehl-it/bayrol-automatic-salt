"""Button entities — Boost, Manual, Pause triggers.

Each button publishes a single MQTT write to its ``s/<topic>`` channel with
the ``trigger_value`` from the topic catalog. The cloud accepts the same
write multiple times, so the button is safe to spam from automations.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BayrolCoordinator
from .entity import BayrolEntity
from .mqtt_client import BayrolMqttError
from .topics import BUTTON_TOPICS, ButtonTopic

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: BayrolCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BayrolButton] = []
    for cid in coordinator.mqtt_clients:
        for item in BUTTON_TOPICS:
            entities.append(BayrolButton(coordinator, cid, item))
    async_add_entities(entities)


class BayrolButton(BayrolEntity, ButtonEntity):
    def __init__(
        self,
        coordinator: BayrolCoordinator,
        cid: str,
        item: ButtonTopic,
    ) -> None:
        super().__init__(coordinator, cid)
        self._item = item
        self._attr_unique_id = f"{cid}_btn_{item.key}"
        self._attr_name = item.name
        if item.icon:
            self._attr_icon = item.icon

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._cid in self.coordinator.mqtt_clients

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_write_enum(
                self._cid, self._item, self._item.trigger_value
            )
        except BayrolMqttError as err:
            raise HomeAssistantError(f"Bayrol MQTT write failed: {err}") from err
        _LOGGER.info("Bayrol button %s pressed (cid=%s)", self._item.key, self._cid)
