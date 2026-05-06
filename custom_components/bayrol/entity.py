"""Shared entity base for Bayrol coordinated entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    KEY_DEVICE_ID,
    KEY_DEVICE_MODEL,
    KEY_DEVICE_VERSION,
    KEY_NAME,
    KEY_STATUS,
    STATUS_ONLINE,
)
from .coordinator import BayrolCoordinator


class BayrolEntity(CoordinatorEntity[BayrolCoordinator]):
    """Base class — provides device_info and an availability check tied to status."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: BayrolCoordinator, cid: str) -> None:
        super().__init__(coordinator)
        self._cid = cid

    @property
    def _data(self) -> dict[str, object]:
        return self.coordinator.data.get(self._cid, {}) if self.coordinator.data else {}

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._data.get(KEY_STATUS) == STATUS_ONLINE

    @property
    def device_info(self) -> DeviceInfo:
        controller = self.coordinator.controllers.get(self._cid)
        name = controller.name if controller else self._data.get(KEY_NAME, "Bayrol")
        return DeviceInfo(
            identifiers={(DOMAIN, self._cid)},
            name=name,
            manufacturer="Bayrol",
            model=self._data.get(KEY_DEVICE_MODEL)
            or (controller.device_model if controller else None),
            sw_version=self._data.get(KEY_DEVICE_VERSION)
            or (controller.device_version if controller else None),
            serial_number=self._data.get(KEY_DEVICE_ID)
            or (controller.device_id if controller else None),
        )
