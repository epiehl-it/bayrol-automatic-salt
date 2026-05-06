"""Async MQTT-over-WebSocket client for the Bayrol Pool Access cloud.

The Bayrol app drives newer controllers (Automatic SALT firmware ≥ 1.5)
exclusively through MQTT-over-WebSocket. The auth flow we mimic here is::

    GET https://www.bayrol-poolaccess.de/p/device.php?c=<cid>
        → iframe src ?code=<applink_code>
    GET https://www.bayrol-poolaccess.de/api/?code=<applink_code>
        → {"accessToken": "<token>", "deviceSerial": "<serial>"}
    wss://www.bayrol-poolaccess.de:8083/   user=<token>  pass='*'

Topics (mirrored from the SPA's DeviceDriver.js)::

    d02/<serial>/v/<type>.<id>   ← values published by the device
    d02/<serial>/g/<type>.<id>   → request a value (empty payload)
    d02/<serial>/s/<type>.<id>   → write a value (JSON payload)

paho-mqtt is callback-driven. We run its network loop on a background thread
and post incoming messages onto the asyncio event loop with
``call_soon_threadsafe`` so consumers can stay async.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import ssl
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
import paho.mqtt.client as mqtt

from .topics import (
    BUTTON_TOPICS,
    ENUM_TOPICS,
    NUM_TOPICS,
    ButtonTopic,
    EnumTopic,
    NumTopic,
    encode_enum_value,
)

_LOGGER = logging.getLogger(__name__)

BROKER_HOST = "www.bayrol-poolaccess.de"
BROKER_PORT = 8083
TOKEN_API = "https://www.bayrol-poolaccess.de/api/"

_IFRAME_CODE_RE = re.compile(r"index\.html\?code=([A-Za-z0-9\-_]+)")


class BayrolMqttError(Exception):
    """MQTT transport failure (connect rejected, broker unreachable, etc.)."""


# Coordinator publishes on this signature — one update at a time.
ValueListener = Callable[[str, dict[str, Any]], Awaitable[None]]


async def fetch_access_token(session: aiohttp.ClientSession, code: str) -> tuple[str, str]:
    """Exchange the iframe ``code`` for an MQTT accessToken and device serial."""
    async with session.get(TOKEN_API, params={"code": code}) as resp:
        resp.raise_for_status()
        body = await resp.json(content_type=None)
    if "accessToken" not in body or "deviceSerial" not in body:
        raise BayrolMqttError(f"Unexpected /api response: {body}")
    return body["accessToken"], body["deviceSerial"]


def extract_iframe_code(device_html: str) -> str | None:
    """Pull the per-device applink ``code`` from a ``device.php`` page."""
    if m := _IFRAME_CODE_RE.search(device_html):
        return m.group(1)
    return None


class BayrolMqttClient:
    """Single-controller MQTT client.

    Connects to the broker, runs initial g/<topic> requests for every item we
    know about, and forwards incoming v/<topic> updates to ``listener``. Use
    one instance per CID.
    """

    def __init__(
        self,
        access_token: str,
        device_serial: str,
        listener: ValueListener,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._access_token = access_token
        self._serial = device_serial
        self._listener = listener
        self._loop = loop
        self._client: mqtt.Client | None = None
        self._connected = asyncio.Event()
        # Last received raw JSON payload per topic, keyed as "<type>.<id>".
        self._latest: dict[str, dict[str, Any]] = {}

    @property
    def serial(self) -> str:
        return self._serial

    @property
    def latest(self) -> dict[str, dict[str, Any]]:
        return self._latest

    async def connect(self, timeout: float = 15.0) -> None:
        """Open the MQTT-WS connection and wait for CONNACK."""
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            transport="websockets",
            client_id=f"user_{os.urandom(4).hex()}",
        )
        # Username = accessToken; password literal '*' (per SPA DeviceDriver.js).
        client.username_pw_set(self._access_token, "*")
        client.tls_set_context(ssl.create_default_context())
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect

        client.connect_async(BROKER_HOST, BROKER_PORT, keepalive=60)
        client.loop_start()
        self._client = client

        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
        except TimeoutError as err:
            client.loop_stop()
            client.disconnect()
            self._client = None
            raise BayrolMqttError("Timeout waiting for MQTT CONNACK") from err

    async def disconnect(self) -> None:
        if self._client is None:
            return
        # paho's loop_stop is sync but signals the background thread to exit.
        await asyncio.get_running_loop().run_in_executor(None, self._client.loop_stop)
        await asyncio.get_running_loop().run_in_executor(None, self._client.disconnect)
        self._client = None
        self._connected.clear()

    def publish_set(self, topic: str, payload: dict[str, Any]) -> None:
        """Write a value back to the device (publish on s/<type>.<id>)."""
        if self._client is None or not self._connected.is_set():
            raise BayrolMqttError("MQTT client is not connected")
        full_topic = f"d02/{self._serial}/s/{topic}"
        body = json.dumps(payload, separators=(",", ":"))
        info = self._client.publish(full_topic, body, qos=0)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise BayrolMqttError(f"publish to {full_topic} failed rc={info.rc}")
        _LOGGER.debug("MQTT publish %s = %s", full_topic, body)

    def write_num(self, item: NumTopic, scaled_value: float) -> None:
        """Write a numeric value, scaling back to the device's integer space."""
        if not item.writable:
            raise BayrolMqttError(f"{item.topic} is not writable")
        raw_value = int(round(scaled_value / item.factor)) if item.factor else int(scaled_value)
        cached = self._latest.get(item.topic, {})
        payload: dict[str, Any] = {"t": item.topic, "v": raw_value}
        if "min" in cached:
            payload["min"] = cached["min"]
        if "max" in cached:
            payload["max"] = cached["max"]
        self.publish_set(item.topic, payload)

    def write_enum(self, item: EnumTopic | ButtonTopic, value: int) -> None:
        """Write an enum value (the cloud expects ``"19.<int>"`` strings)."""
        payload = {"t": item.topic, "v": encode_enum_value(value)}
        self.publish_set(item.topic, payload)

    # --- paho callbacks ---------------------------------------------------

    def _on_connect(
        self,
        client: mqtt.Client,
        _userdata: Any,
        _flags: Any,
        reason_code: Any,
        _properties: Any = None,
    ) -> None:
        if int(reason_code) != 0:
            _LOGGER.error("Bayrol MQTT broker rejected connection: %s", reason_code)
            self._loop.call_soon_threadsafe(self._connected.set)
            return

        # Subscribe to every item we care about, then ask for its current value.
        items: list[NumTopic | EnumTopic | ButtonTopic] = [
            *NUM_TOPICS,
            *ENUM_TOPICS,
            *BUTTON_TOPICS,
        ]
        for item in items:
            client.subscribe(f"d02/{self._serial}/v/{item.topic}", qos=0)
        for item in items:
            client.publish(f"d02/{self._serial}/g/{item.topic}", "", qos=0)

        self._loop.call_soon_threadsafe(self._connected.set)
        _LOGGER.info(
            "Bayrol MQTT connected serial=%s, primed %d items", self._serial, len(items)
        )

    def _on_message(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        message: mqtt.MQTTMessage,
    ) -> None:
        topic = message.topic
        # Only care about value channels under our serial.
        prefix = f"d02/{self._serial}/v/"
        if not topic.startswith(prefix):
            return
        item_topic = topic[len(prefix):]
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            _LOGGER.debug("Skipping %s: cannot decode payload (%s)", topic, err)
            return
        if not isinstance(payload, dict):
            return

        self._latest[item_topic] = payload
        # Hop back onto the HA event loop before invoking the async listener.
        asyncio.run_coroutine_threadsafe(
            self._listener(item_topic, payload), self._loop
        )

    def _on_disconnect(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        _flags: Any = None,
        reason_code: Any = None,
        _properties: Any = None,
    ) -> None:
        _LOGGER.info(
            "Bayrol MQTT disconnected serial=%s reason=%s", self._serial, reason_code
        )
        # paho will auto-reconnect because we used connect_async; the loop
        # thread keeps running until disconnect() is called explicitly.
