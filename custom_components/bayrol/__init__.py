"""Bayrol Pool Access integration setup.

HA-specific imports are kept inside the lifecycle functions so the package can
be imported in test environments (or by ``tools/probe.py``) without pulling
``homeassistant`` onto the path.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

PLATFORMS: list[str] = ["sensor", "binary_sensor", "select", "number", "button"]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    import aiohttp
    from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
    from homeassistant.helpers.aiohttp_client import async_create_clientsession

    from .api import BayrolApiError, BayrolAuthError, BayrolClient, BayrolPinError
    from .const import (
        CONF_PASSWORD,
        CONF_REFRESH_INTERVAL,
        CONF_SETTINGS_PIN,
        CONF_USERNAME,
        DEFAULT_REFRESH_INTERVAL,
        DOMAIN,
        MIN_REFRESH_INTERVAL,
    )
    from .coordinator import BayrolCoordinator

    # Dedicated session with its own (dummy) cookie jar. HA's default shared
    # session would mix Bayrol's PHPSESSID with cookies from other integrations
    # and our login flow expects to fully control the session cookie via the
    # explicit ``Cookie`` header.
    session = async_create_clientsession(
        hass, cookie_jar=aiohttp.DummyCookieJar()
    )
    client = BayrolClient(
        session,
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )

    try:
        await client.login()
        controllers = await client.get_controllers()
    except BayrolAuthError as err:
        _LOGGER.error("Bayrol login failed: %s", err)
        raise ConfigEntryAuthFailed(str(err)) from err
    except BayrolApiError as err:
        _LOGGER.error("Bayrol cloud unreachable during setup: %s", err)
        raise ConfigEntryNotReady(str(err)) from err
    except aiohttp.ClientError as err:
        _LOGGER.error("Network error talking to Bayrol cloud: %s", err)
        raise ConfigEntryNotReady(str(err)) from err

    if not controllers:
        raise ConfigEntryNotReady("Bayrol account has no controllers")

    interval_seconds = entry.options.get(CONF_REFRESH_INTERVAL)
    if interval_seconds is None:
        interval = DEFAULT_REFRESH_INTERVAL
    else:
        interval = max(timedelta(seconds=int(interval_seconds)), MIN_REFRESH_INTERVAL)

    settings_pin = entry.data.get(CONF_SETTINGS_PIN) or None
    coordinator = BayrolCoordinator(
        hass, client, controllers, interval, settings_pin=settings_pin
    )

    # Try to unlock legacy data_json.php writes. Newer SALT firmware doesn't
    # expose this endpoint at all (responds with ``access: false`` regardless
    # of the PIN), so a failure here is fine — control still works via MQTT.
    if settings_pin:
        for controller in controllers:
            try:
                await coordinator.async_authorize(controller.cid)
            except BayrolPinError as err:
                _LOGGER.info(
                    "Legacy PIN gateway not available for cid=%s (%s); "
                    "control remains via MQTT.",
                    controller.cid,
                    err,
                )
                coordinator.settings_pin = None
                break
            except BayrolApiError as err:
                _LOGGER.warning("Could not pre-authorize cid=%s: %s", controller.cid, err)

    # Open MQTT-WS for SPA-driven controllers. Failures here don't block setup —
    # number/button entities just won't appear for the affected controller.
    try:
        await coordinator.async_start_mqtt()
    except Exception:  # noqa: BLE001 — MQTT is optional, never fail entry setup
        _LOGGER.exception("Failed to start MQTT for one or more controllers")
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .const import DOMAIN

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        await coordinator.async_stop_mqtt()
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload entry when options (e.g. refresh interval) change."""
    await hass.config_entries.async_reload(entry.entry_id)
