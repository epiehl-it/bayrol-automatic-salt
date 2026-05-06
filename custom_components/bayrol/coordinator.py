"""DataUpdateCoordinator for the Bayrol cloud.

The coordinator pulls from two transports:

* HTTP — proven for sensors (pH/Redox/Salt/Temp) on every controller family.
  Polls every ``update_interval``.
* MQTT-over-WebSocket — push-based, used for SPA-driven controllers
  (Automatic SALT firmware ≥ 1.5) where the legacy ``data_json.php`` write
  channel is gone. MQTT also provides setpoints (Number entities) and mode
  triggers (Button entities).

Both feed into the same coordinator data dict keyed by CID, so HA entities
don't need to know which transport delivered a particular value.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BayrolApiError, BayrolAuthError, BayrolClient, BayrolPinError
from .const import DOMAIN
from .mqtt_client import (
    BayrolMqttClient,
    BayrolMqttError,
    extract_iframe_code,
    fetch_access_token,
)
from .parser import Controller, DeviceItem, merge_pool_data
from .topics import (
    ButtonTopic,
    EnumTopic,
    NumTopic,
    parse_enum_value,
)

_LOGGER = logging.getLogger(__name__)

# Stored as ``data[cid]["mqtt"][<topic>]``: the raw last-seen JSON payload
# (e.g. ``{"t": "4.2", "v": 72, "min": 62, "max": 82}``). Entities decode it
# via the topic catalog rather than re-parsing on every state read.
MQTT_KEY = "mqtt"


class BayrolCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polls the cloud once per interval, exposes data keyed by CID."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: BayrolClient,
        controllers: list[Controller],
        update_interval: timedelta,
        settings_pin: str | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self.client = client
        self.controllers: dict[str, Controller] = {c.cid: c for c in controllers}
        self.settings_pin = settings_pin
        # Tracks which controllers have an unlocked legacy-write session.
        self._authorized: set[str] = set()
        # MQTT clients keyed by CID — created lazily during async_start_mqtt.
        self._mqtt: dict[str, BayrolMqttClient] = {}

    @property
    def write_enabled(self) -> bool:
        return self.settings_pin is not None

    @property
    def mqtt_clients(self) -> dict[str, BayrolMqttClient]:
        return self._mqtt

    # ---------------------------------------------------------------- legacy
    # Settings PIN + data_json.php is the older write channel used by Cl-pH /
    # PoolManager. SALT firmware ignores it; we keep this path for parity.
    # ------------------------------------------------------------------------

    async def async_authorize(self, cid: str) -> None:
        if not self.settings_pin:
            raise BayrolPinError("No settings PIN configured")
        await self.client.authorize_settings(cid, self.settings_pin)
        self._authorized.add(cid)

    async def async_set_item(self, cid: str, topic: str, value: int) -> None:
        if not self.settings_pin:
            raise BayrolPinError("No settings PIN configured")
        if cid not in self._authorized:
            await self.async_authorize(cid)
        try:
            await self.client.set_item(cid, topic, value)
        except BayrolPinError:
            self._authorized.discard(cid)
            await self.async_authorize(cid)
            await self.client.set_item(cid, topic, value)
        await self.async_request_refresh()

    # ----------------------------------------------------------------- MQTT

    async def async_start_mqtt(self) -> None:
        """Open MQTT connections for every controller that exposes the SPA.

        Devices without an iframe ``code`` (older Cl-pH/PoolManager firmware)
        are silently skipped — they keep working through the HTTP path alone.
        """
        session = async_get_clientsession(self.hass)
        loop = asyncio.get_running_loop()
        for cid in self.controllers:
            try:
                html = await self.client.get_device_html(cid)
            except (BayrolApiError, BayrolAuthError) as err:
                _LOGGER.warning("device.php fetch failed for %s: %s", cid, err)
                continue
            code = extract_iframe_code(html)
            if not code:
                _LOGGER.info("Controller %s does not expose an iframe code; MQTT skipped", cid)
                continue
            try:
                token, serial = await fetch_access_token(session, code)
            except (aiohttp.ClientError, BayrolMqttError) as err:
                _LOGGER.warning("Token exchange failed for %s: %s", cid, err)
                continue

            mqtt_client = BayrolMqttClient(
                token,
                serial,
                listener=self._make_listener(cid),
                loop=loop,
            )
            try:
                await mqtt_client.connect()
            except BayrolMqttError as err:
                _LOGGER.warning("MQTT connect failed for %s: %s", cid, err)
                continue
            self._mqtt[cid] = mqtt_client
            _LOGGER.info("MQTT online for cid=%s serial=%s", cid, serial)

    async def async_stop_mqtt(self) -> None:
        for client in self._mqtt.values():
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 — best-effort shutdown
                _LOGGER.debug("MQTT disconnect raised", exc_info=True)
        self._mqtt.clear()

    def _make_listener(self, cid: str):
        """Closure over CID so the MQTT client can call us back per controller."""
        async def _listener(topic: str, payload: dict[str, Any]) -> None:
            await self._handle_mqtt_value(cid, topic, payload)
        return _listener

    async def _handle_mqtt_value(
        self, cid: str, topic: str, payload: dict[str, Any]
    ) -> None:
        # Update without forcing a full refresh; entities subscribe via
        # async_added_to_hass and pick up the new state on the next state read.
        data = dict(self.data) if self.data else {}
        per_cid = dict(data.get(cid, {}))
        mqtt_state = dict(per_cid.get(MQTT_KEY, {}))
        mqtt_state[topic] = payload
        per_cid[MQTT_KEY] = mqtt_state
        data[cid] = per_cid
        self.async_set_updated_data(data)

    async def async_write_num(self, cid: str, item: NumTopic, value: float) -> None:
        client = self._mqtt.get(cid)
        if client is None:
            raise BayrolMqttError(f"No MQTT connection for {cid}")
        client.write_num(item, value)

    async def async_write_enum(
        self, cid: str, item: EnumTopic | ButtonTopic, value: int
    ) -> None:
        client = self._mqtt.get(cid)
        if client is None:
            raise BayrolMqttError(f"No MQTT connection for {cid}")
        client.write_enum(item, value)

    # ----------------------------------------------------------------- HTTP

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for cid, controller in self.controllers.items():
            try:
                data = await self.client.get_data(cid)
            except BayrolAuthError as err:
                raise UpdateFailed(f"Authentication failed: {err}") from err
            except BayrolApiError as err:
                _LOGGER.warning("Failed to fetch %s: %s", cid, err)
                if self.data and cid in self.data:
                    out[cid] = self.data[cid]
                continue

            flat = merge_pool_data(controller, data)

            # Legacy device.php items (Cl-pH / PoolManager). On SALT this
            # returns nothing — that's expected, MQTT covers control there.
            try:
                items = await self.client.get_device_items(cid)
            except (BayrolApiError, BayrolAuthError) as err:
                _LOGGER.debug("Item fetch for %s failed: %s", cid, err)
                if self.data and (prev_items := self.data.get(cid, {}).get("items")):
                    flat["items"] = prev_items
            else:
                if items:
                    flat["items"] = {item.topic: item for item in items}

            # Preserve any MQTT state we already have so a slow HTTP cycle
            # doesn't blow away push-delivered values.
            if self.data and (prev_mqtt := self.data.get(cid, {}).get(MQTT_KEY)):
                flat[MQTT_KEY] = prev_mqtt

            out[cid] = flat

        if not out:
            raise UpdateFailed("No controller returned data")
        return out


def get_item(data: dict[str, dict[str, Any]] | None, cid: str, topic: str) -> DeviceItem | None:
    """Helper for entities: pluck a legacy DeviceItem out of coordinator data."""
    if not data:
        return None
    items = data.get(cid, {}).get("items")
    if not items:
        return None
    return items.get(topic)


def get_mqtt_payload(
    data: dict[str, dict[str, Any]] | None, cid: str, topic: str
) -> dict[str, Any] | None:
    """Helper for entities: read the latest MQTT JSON payload for a topic."""
    if not data:
        return None
    return data.get(cid, {}).get(MQTT_KEY, {}).get(topic)


def decode_num(payload: dict[str, Any] | None, item: NumTopic) -> float | None:
    """Convert a Num MQTT payload into the human-scaled value."""
    if not payload or "v" not in payload:
        return None
    try:
        return float(payload["v"]) * item.factor
    except (TypeError, ValueError):
        return None


def decode_enum(payload: dict[str, Any] | None) -> int | None:
    """Pull the enum choice out of a ``"19.<value>"`` payload."""
    if not payload or "v" not in payload:
        return None
    raw = payload["v"]
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        return parse_enum_value(raw)
    return None
