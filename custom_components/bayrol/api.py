"""Async client for the Bayrol Pool Access cloud (HTTP scraping)."""

from __future__ import annotations

import logging
import re
from typing import Any, Final

import aiohttp

from .const import BASE_URL
from .parser import (
    Controller,
    DeviceItem,
    PoolData,
    is_login_error,
    parse_controllers,
    parse_device_items,
    parse_login_form,
    parse_overview,
    parse_pool_data,
)

_LOGGER = logging.getLogger(__name__)

_USER_AGENT: Final = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0"
)
_BASE_HEADERS: Final[dict[str, str]] = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US;q=0.7,en;q=0.3",
    "Connection": "keep-alive",
}


class BayrolAuthError(Exception):
    """Authentication with the Bayrol Cloud failed."""


class BayrolApiError(Exception):
    """Generic Bayrol Cloud error (transport, parsing, unexpected response)."""


class BayrolPinError(Exception):
    """Controller settings PIN was rejected by the cloud."""


class BayrolClient:
    """Login-once / poll-many client for the Bayrol Pool Access portal.

    Sessions are kept alive via ``PHPSESSID``. The cloud forces re-login on
    expiry; ``get_data`` transparently retries once when that happens.
    """

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._phpsessid: str | None = None

    @property
    def authenticated(self) -> bool:
        return self._phpsessid is not None

    async def login(self) -> None:
        """Authenticate. Raises BayrolAuthError on bad credentials."""
        self._session.cookie_jar.clear()
        self._phpsessid = None

        # Step 1: GET login page to obtain PHPSESSID + form fields.
        init_url = f"{BASE_URL}/m/login.php"
        async with self._session.get(init_url, headers=self._headers()) as resp:
            html = await resp.text()
            self._phpsessid = self._extract_phpsessid(resp)
            if not self._phpsessid:
                raise BayrolApiError("No PHPSESSID cookie returned from login page")

            form = parse_login_form(html)
            if form is None:
                raise BayrolApiError("Login form not present in response")

        form["username"] = self._username
        form["password"] = self._password

        # Step 2: POST credentials.
        login_url = f"{BASE_URL}/m/login.php?r=reg"
        headers = self._headers(
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://www.bayrol-poolaccess.de",
                "Referer": init_url,
            }
        )
        async with self._session.post(login_url, headers=headers, data=form) as resp:
            content = await resp.text()
            if is_login_error(content):
                raise BayrolAuthError("Bayrol rejected the credentials")

    async def get_controllers(self) -> list[Controller]:
        """List controllers and enrich each with model/firmware from getdata.

        ``plants.php`` is a JS-rendered shell — without executing JavaScript we
        only see the CIDs. To populate device info (model, serial, firmware,
        default name) we issue one ``getdata.php`` call per controller during
        setup. This happens once per integration setup, not on every poll.
        """
        await self._ensure_login()
        url = f"{BASE_URL}/m/plants.php"
        async with self._session.get(url, headers=self._headers()) as resp:
            if resp.status != 200:
                raise BayrolApiError(f"plants.php returned HTTP {resp.status}")
            html = await resp.text()
        controllers = parse_controllers(html)

        for c in controllers:
            try:
                data = await self._get_data_direct(c.cid)
            except (BayrolApiError, BayrolAuthError) as err:
                _LOGGER.debug("Skipping enrichment for cid=%s: %s", c.cid, err)
                continue
            model = data.info.get("device_model")
            if model:
                c.device_model = model
            if device_id := data.info.get("device_id"):
                c.device_id = device_id
            if version := data.info.get("device_version"):
                c.device_version = version
            # If the user never labelled the controller in the Bayrol app,
            # the plants page returns a blank <p> and we fall back to a generic
            # name. Now that we know the model, prefer that over the generic.
            if model and c.name in {"", "Bayrol Pool"}:
                c.name = model

        return controllers

    async def get_data(self, cid: str) -> PoolData:
        """Fetch live measurements for a controller, with overview-page fallback."""
        await self._ensure_login()
        try:
            return await self._get_data_direct(cid)
        except BayrolAuthError:
            # Session expired mid-flight: re-auth once and retry.
            _LOGGER.debug("Session expired, re-authenticating")
            await self.login()
            return await self._get_data_direct(cid)

    async def _get_data_direct(self, cid: str) -> PoolData:
        url = f"{BASE_URL}/getdata.php?cid={cid}"
        headers = self._headers(
            {
                "Accept": "*/*",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{BASE_URL}/m/plants.php",
            }
        )
        async with self._session.get(url, headers=headers) as resp:
            if resp.status == 401 or resp.status == 403:
                raise BayrolAuthError("Session expired")
            if resp.status != 200:
                raise BayrolApiError(f"getdata.php returned HTTP {resp.status}")
            html = await resp.text()

        data = parse_pool_data(html)
        if data.measurements or data.status != "online":
            return data

        # Empty payload — try the overview page as a fallback.
        _LOGGER.debug("Empty getdata.php response, falling back to overview")
        overview = await self._get_overview()
        return overview.get(cid, data)

    async def get_device_items(self, cid: str) -> list[DeviceItem]:
        """Fetch and parse the controllable items for a controller."""
        html = await self.get_device_html(cid)
        return parse_device_items(html)

    async def authorize_settings(self, cid: str, pin: str) -> None:
        """Validate the controller's settings PIN, allowing setItems writes.

        Bayrol's flow has two calls:

        * ``setCode`` — registers the PIN with the session (idempotent).
        * ``getAccess`` — validates and unlocks setItems for this CID.

        Both run against ``data_json.php``; we verify the response carries
        ``"access":true`` before considering the controller writable.
        """
        await self._ensure_login()
        # Touch the device page first — Bayrol's portal expects this referer chain.
        device_url = f"{BASE_URL}/p/device.php?c={cid}"
        async with self._session.get(
            device_url, headers=self._headers({"Referer": f"{BASE_URL}/m/plants.php"})
        ) as resp:
            if resp.status != 200:
                raise BayrolApiError(f"device.php returned HTTP {resp.status}")

        json_url = f"{BASE_URL}/data_json.php"
        json_headers = self._headers(
            {
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/json; charset=utf-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": device_url,
            }
        )

        async with self._session.post(
            json_url,
            headers=json_headers,
            json={"device": cid, "action": "setCode", "data": {"code": pin}},
        ) as resp:
            body = await resp.text()
            if resp.status != 200 or '"error":""' not in body:
                raise BayrolApiError(f"setCode failed: HTTP {resp.status} body={body[:200]}")

        async with self._session.post(
            json_url,
            headers=json_headers,
            json={"device": cid, "action": "getAccess", "data": {"code": pin}},
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise BayrolApiError(f"getAccess failed: HTTP {resp.status}")
            if '"access":true' not in body:
                raise BayrolPinError("Controller settings PIN was rejected")

    async def set_item(self, cid: str, topic: str, value: int) -> None:
        """Write a single item value (e.g. select operation mode)."""
        await self.set_items(cid, [{"topic": topic, "value": [int(value)]}])

    async def set_items(self, cid: str, items: list[dict[str, Any]]) -> None:
        """Write one or more items via ``data_json.php`` setItems.

        ``items`` is a list of dicts with at minimum ``topic`` and ``value``;
        the cloud also accepts ``valid`` / ``cmd`` / ``name`` fields and we
        default the missing ones, mirroring the official web UI's payload.
        """
        await self._ensure_login()
        json_url = f"{BASE_URL}/data_json.php"
        json_headers = self._headers(
            {
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/json; charset=utf-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{BASE_URL}/p/device.php?c={cid}",
            }
        )

        normalised = [
            {
                "topic": item["topic"],
                "name": item.get("name", ""),
                "value": item["value"],
                "valid": item.get("valid", 1),
                "cmd": item.get("cmd", 0),
            }
            for item in items
        ]

        payload = {"device": cid, "action": "setItems", "data": {"items": normalised}}
        async with self._session.post(json_url, headers=json_headers, json=payload) as resp:
            body = await resp.text()
            if resp.status == 401 or resp.status == 403:
                raise BayrolAuthError("Session expired during setItems")
            if resp.status != 200:
                raise BayrolApiError(f"setItems failed: HTTP {resp.status}")
            if '"error":""' not in body:
                # Bayrol returns ``"error":"access denied"`` when the PIN was never
                # validated for this session — bubble that up so the coordinator
                # can re-authorize and retry.
                if "access" in body.lower():
                    raise BayrolPinError(f"setItems rejected: {body[:200]}")
                raise BayrolApiError(f"setItems response indicates failure: {body[:200]}")

    async def get_device_html(self, cid: str) -> str:
        """Fetch the raw ``device.php`` page — has the controllable items.

        The page is a full HTML document with `<div class="i_item">` blocks for
        each item Bayrol exposes (selects, status displays, action triggers).
        Used both by the parser (to enumerate entities) and by the probe tool
        (to capture fixtures).
        """
        await self._ensure_login()
        url = f"{BASE_URL}/p/device.php?c={cid}"
        headers = self._headers({"Referer": f"{BASE_URL}/m/plants.php"})
        async with self._session.get(url, headers=headers) as resp:
            if resp.status == 401 or resp.status == 403:
                raise BayrolAuthError("Session expired")
            if resp.status != 200:
                raise BayrolApiError(f"device.php returned HTTP {resp.status}")
            return await resp.text()

    async def _get_overview(self) -> dict[str, PoolData]:
        url = f"{BASE_URL}/p/plants.php"
        async with self._session.get(url, headers=self._headers()) as resp:
            if resp.status != 200:
                raise BayrolApiError(f"plants overview returned HTTP {resp.status}")
            html = await resp.text()
        return parse_overview(html)

    async def _ensure_login(self) -> None:
        if not self._phpsessid:
            await self.login()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(_BASE_HEADERS)
        if self._phpsessid:
            headers["Cookie"] = f"PHPSESSID={self._phpsessid}"
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _extract_phpsessid(resp: aiohttp.ClientResponse) -> str | None:
        for cookie in resp.cookies.values():
            if cookie.key == "PHPSESSID":
                return cookie.value
        # Fallback: parse Set-Cookie header directly (some proxies hide cookies from the jar).
        set_cookie = resp.headers.get("Set-Cookie", "")
        if m := re.search(r"PHPSESSID=([^;]+)", set_cookie):
            return m.group(1)
        return None
