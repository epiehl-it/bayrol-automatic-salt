"""Config and options flow for the Bayrol Pool Access integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
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

_LOGGER = logging.getLogger(__name__)


class BayrolConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial credentials step and account discovery."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            # Same reasoning as in __init__.async_setup_entry: a private session
            # with a dummy cookie jar avoids cross-talk with HA's shared
            # aiohttp session.
            session = async_create_clientsession(
                self.hass, cookie_jar=aiohttp.DummyCookieJar()
            )
            client = BayrolClient(
                session,
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            try:
                await client.login()
                controllers = await client.get_controllers()
            except BayrolAuthError as err:
                _LOGGER.warning("Bayrol login rejected: %s", err)
                errors["base"] = "invalid_auth"
            except BayrolApiError as err:
                _LOGGER.warning("Bayrol cloud unreachable: %s", err)
                errors["base"] = "cannot_connect"
            except aiohttp.ClientError as err:
                _LOGGER.warning("Network error talking to Bayrol cloud: %s", err)
                errors["base"] = "cannot_connect"
            else:
                if not controllers:
                    errors["base"] = "no_controllers"
                else:
                    pin = (user_input.get(CONF_SETTINGS_PIN) or "").strip()
                    if pin:
                        # The PIN is account-wide; validating against the first
                        # controller is enough.
                        try:
                            await client.authorize_settings(controllers[0].cid, pin)
                        except BayrolPinError as err:
                            _LOGGER.warning("Bayrol PIN rejected: %s", err)
                            errors["base"] = "invalid_pin"
                        except (BayrolApiError, aiohttp.ClientError) as err:
                            _LOGGER.warning("PIN validation failed: %s", err)
                            errors["base"] = "cannot_connect"
                    if not errors:
                        await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
                        self._abort_if_unique_id_configured()
                        data = {
                            CONF_USERNAME: user_input[CONF_USERNAME],
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                        }
                        if pin:
                            data[CONF_SETTINGS_PIN] = pin
                        return self.async_create_entry(
                            title=f"Bayrol ({user_input[CONF_USERNAME]})",
                            data=data,
                        )

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_SETTINGS_PIN, default=""): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reauth(
        self, _user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry: ConfigEntry | None = self._get_reauth_entry()

        if user_input is not None and entry is not None:
            session = async_create_clientsession(
                self.hass, cookie_jar=aiohttp.DummyCookieJar()
            )
            client = BayrolClient(
                session,
                entry.data[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            try:
                await client.login()
            except BayrolAuthError as err:
                _LOGGER.warning("Bayrol re-login rejected: %s", err)
                errors["base"] = "invalid_auth"
            except (BayrolApiError, aiohttp.ClientError) as err:
                _LOGGER.warning("Bayrol re-login network error: %s", err)
                errors["base"] = "cannot_connect"
            else:
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        schema = vol.Schema({vol.Required(CONF_PASSWORD): str})
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, errors=errors
        )

    def _get_reauth_entry(self) -> ConfigEntry | None:
        entry_id = self.context.get("entry_id")
        if not entry_id:
            return None
        return self.hass.config_entries.async_get_entry(entry_id)

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BayrolOptionsFlow(config_entry)


class BayrolOptionsFlow(OptionsFlow):
    """Refresh-interval option."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(
            CONF_REFRESH_INTERVAL, int(DEFAULT_REFRESH_INTERVAL.total_seconds())
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_REFRESH_INTERVAL, default=current): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=int(MIN_REFRESH_INTERVAL.total_seconds())),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
