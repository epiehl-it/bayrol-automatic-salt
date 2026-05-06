"""Select entities — one per controllable item discovered on device.php.

Reads are always available; writes require the controller settings PIN to be
configured. Without a PIN the select will still show the current mode but
calling ``async_select_option`` will fail with a clear error in the UI.
"""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import BayrolApiError, BayrolAuthError, BayrolPinError
from .const import DOMAIN
from .coordinator import BayrolCoordinator, get_item
from .entity import BayrolEntity
from .parser import DeviceItem

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: BayrolCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[BayrolItemSelect] = []
    seen: set[tuple[str, str]] = set()
    for cid, payload in (coordinator.data or {}).items():
        for topic, item in (payload.get("items") or {}).items():
            key = (cid, topic)
            if key in seen:
                continue
            seen.add(key)
            entities.append(BayrolItemSelect(coordinator, cid, item))
    async_add_entities(entities)


class BayrolItemSelect(BayrolEntity, SelectEntity):
    """SelectEntity bound to one Bayrol topic (e.g. Filterpumpe Betriebsart)."""

    def __init__(
        self,
        coordinator: BayrolCoordinator,
        cid: str,
        item: DeviceItem,
    ) -> None:
        super().__init__(coordinator, cid)
        self._topic = item.topic
        self._attr_unique_id = f"{cid}_item_{item.slug}"
        # The display name combines device + operation so multi-control devices
        # (e.g. Elektrolyse > Betriebsart vs. Boost) stay distinguishable.
        if item.label and item.label.lower() not in item.device.lower():
            self._attr_name = f"{item.device} {item.label}"
        else:
            self._attr_name = item.device

    @property
    def _item(self) -> DeviceItem | None:
        return get_item(self.coordinator.data, self._cid, self._topic)

    @property
    def options(self) -> list[str]:
        item = self._item
        return [opt.text for opt in item.options] if item else []

    @property
    def current_option(self) -> str | None:
        item = self._item
        return item.current_text if item else None

    async def async_select_option(self, option: str) -> None:
        item = self._item
        if not item:
            raise HomeAssistantError("Item is not yet known to the integration")
        match = next((o for o in item.options if o.text == option), None)
        if match is None:
            raise HomeAssistantError(f"Unknown option {option!r} for {self._attr_name}")
        if not self.coordinator.write_enabled:
            raise HomeAssistantError(
                "No Bayrol settings PIN configured — add it in the integration "
                "options to enable writes."
            )
        try:
            await self.coordinator.async_set_item(self._cid, self._topic, match.value)
        except BayrolPinError as err:
            raise HomeAssistantError(f"Bayrol rejected the settings PIN: {err}") from err
        except (BayrolApiError, BayrolAuthError) as err:
            raise HomeAssistantError(f"Bayrol cloud error: {err}") from err
