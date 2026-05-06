#!/usr/bin/env python3
"""MQTT-over-WebSocket probe for Bayrol Automatic SALT (and similar SPA-based devices).

Background
----------
Newer Bayrol controllers replaced their HTML control page with a single-page
app (Embedded Wizard) that talks MQTT-over-WebSocket. The auth flow is::

    1. Pull the ``code`` (e.g. ``A-EyA9U2``) from device.php's iframe URL.
    2. GET https://www.bayrol-poolaccess.de/api/?code=<code>
       → {"accessToken": "<32-hex-token>", "deviceSerial": "<serial>"}
    3. Open wss://www.bayrol-poolaccess.de:8083/ with
       username=<accessToken>, password='*'.

Topic shape (from the SPA's DeviceDriver.js)::

    d02/<deviceSerial>/<kind>/<type>.<id>
        kind: 'v' subscribe/values, 's' publish/set, 'g' request
        type: 1=NumParam, 2=NumVar, 3=EnumParam, 4=…
        id:   item id within the type

This script captures the live message stream so we can map types/ids to
human-readable controls (Filterpumpe, Elektrolyse, Boost, pH-Sollwert, …).

Run::

    export BAYROL_USERNAME=…
    export BAYROL_PASSWORD=…
    .venv/bin/python tools/mqtt_probe.py --duration 30 --dump
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import ssl
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "custom_components"))

import aiohttp  # noqa: E402
import paho.mqtt.client as mqtt  # noqa: E402
from bayrol.api import BayrolClient  # noqa: E402
from bayrol.parser import extract_direct_access_code  # noqa: E402

FIXTURES = REPO_ROOT / "fixtures"
BROKER_HOST = "www.bayrol-poolaccess.de"
BROKER_PORT = 8083
TOKEN_API = "https://www.bayrol-poolaccess.de/api/"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mqtt_probe")


def run_subscribe(
    access_token: str,
    device_serial: str,
    duration: int,
    dump_path: Path | None,
) -> None:
    """Open the MQTT-WS connection and capture messages for ``duration`` seconds."""
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        transport="websockets",
        client_id=f"user_{os.urandom(4).hex()}",
    )
    # Username = the token returned by /api/?code=<X>; password is literal '*'
    # (lifted from the SPA's DeviceDriver.js I_Connect function).
    client.username_pw_set(access_token, "*")
    client.tls_set_context(ssl.create_default_context())

    seen_topics: set[str] = set()
    fp = dump_path.open("w", encoding="utf-8") if dump_path else None

    # Topic-type IDs from the SPA's Device.js TopicIDs enum.
    # 4=num, 5=enum, 6=string, 7=code, 11=cond, 12=gui_event, 13=function.
    request_types = [4, 5, 6, 7, 11, 12, 13]

    # Curated list of (type, id) pairs we want to discover. Names taken from
    # the SPA's NumStringsExternal / EnumStringsExternal lookups so we can
    # match results back to human-readable labels.
    items_of_interest: list[tuple[int, int, str]] = [
        # ---- Num (type 4) ----
        (4, 2, "ph_setpoint"),
        (4, 3, "ph_upper_limit"),
        (4, 4, "ph_lower_limit"),
        (4, 28, "mv_setpoint"),
        (4, 26, "mv_upper_limit"),
        (4, 78, "var_ph"),
        (4, 99, "var_t_display"),
        (4, 100, "var_salt"),
        (4, 101, "var_salt_display"),
        (4, 103, "var_cond_display"),
        (4, 137, "var_salt_level_in_pool"),
        (4, 138, "var_salt_to_add"),
        (4, 155, "ram_se_boost_progress_in_min"),
        (4, 204, "var_mv"),
        (4, 220, "var_t_act"),
        (4, 123, "t_set"),
        (4, 124, "t_upper_limit"),
        # ---- Enum (type 5) — modes ----
        (5, 27, "opmode"),
        (5, 41, "se_opmode"),
        (5, 81, "var_se_opmode"),
        (5, 104, "se_activate_boost_gui"),
        (5, 105, "se_activate_manual_gui"),
        (5, 106, "se_activate_pause_gui"),
        (5, 130, "var_se_activate_boost"),
        (5, 142, "var_ph_opmode"),
        (5, 155, "var_mv_opmode"),
        (5, 185, "vsp_opmode"),
        (5, 256, "on_off_filter_pump_opmode"),
        # ---- Function (type 13) — likely the button targets ----
        # (function ids are sparse — we'll probe what comes back from g/13)
    ]

    def on_connect(_client, _userdata, _flags, reason_code, _properties=None):
        log.info("CONNACK reason=%s", reason_code)
        if reason_code != 0 and reason_code != mqtt.MQTT_ERR_SUCCESS:
            log.error("Broker rejected the connection — username/code may be wrong")
            return

        # 1) Wildcard subscribe so any spontaneous push lands in the log.
        wildcard = f"d02/{device_serial}/v/#"
        res, _ = _client.subscribe(wildcard, qos=0)
        log.info("SUBSCRIBE %s -> %s", wildcard, res)

        # 2) Per-item subscribe + request: this is what the SPA's
        #    I_RegisterObject does for every UI-bound value. Empty payload
        #    triggers the device to emit the current value back to us on
        #    v/<type>.<id>. Without this, the device only pushes what
        #    happens to change while we are connected.
        for t, item_id, name in items_of_interest:
            topic = f"d02/{device_serial}/v/{t}.{item_id}"
            _client.subscribe(topic, qos=0)
            log.debug("subscribed %s (%s)", topic, name)

        for t, item_id, name in items_of_interest:
            req = f"d02/{device_serial}/g/{t}.{item_id}"
            res = _client.publish(req, "", qos=0)
            log.debug("request %s rc=%s (%s)", req, res.rc, name)

        # 3) Also probe the type-level get for sparse types we don't enumerate
        #    (functions, codes, conditions, …) — cheap, may reveal useful items.
        for t in request_types:
            req = f"d02/{device_serial}/g/{t}"
            _client.publish(req, "", qos=0)
        log.info("Issued %d per-item requests", len(items_of_interest))

    name_lookup = {(t, i): n for t, i, n in items_of_interest}

    def on_message(_client, _userdata, msg: mqtt.MQTTMessage):
        text = msg.payload.decode("utf-8", errors="replace")
        marker = " " if msg.topic in seen_topics else "*"
        seen_topics.add(msg.topic)
        # Annotate v/<type>.<id> topics with our friendly name when known.
        annotation = ""
        parts = msg.topic.rsplit("/", 1)
        if len(parts) == 2 and "." in parts[1]:
            try:
                t, i = (int(x) for x in parts[1].split(".", 1))
                if name := name_lookup.get((t, i)):
                    annotation = f"  ({name})"
            except ValueError:
                pass
        log.info("%s %s = %s%s", marker, msg.topic, text[:200], annotation)
        if fp:
            fp.write(f"{time.time():.3f}\t{msg.topic}\t{text}\n")
            fp.flush()

    def on_disconnect(_client, _userdata, _flags, reason_code, _properties=None):
        log.info("DISCONNECT reason=%s", reason_code)

    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect

    log.info(
        "Connecting to wss://%s:%d as token=%s… (serial=%s)",
        BROKER_HOST,
        BROKER_PORT,
        access_token[:8],
        device_serial,
    )
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)

    stop_at = time.monotonic() + duration
    client.loop_start()
    try:
        while time.monotonic() < stop_at:
            time.sleep(1)
    finally:
        client.loop_stop()
        client.disconnect()
        if fp:
            fp.close()
        log.info("Captured %d distinct topics for %s", len(seen_topics), device_serial)


async def fetch_access_token(session: aiohttp.ClientSession, code: str) -> tuple[str, str]:
    """Exchange the iframe ``code`` for an MQTT accessToken and device serial."""
    async with session.get(TOKEN_API, params={"code": code}) as resp:
        resp.raise_for_status()
        body = await resp.json(content_type=None)
    if "accessToken" not in body or "deviceSerial" not in body:
        raise RuntimeError(f"Unexpected /api response: {body}")
    return body["accessToken"], body["deviceSerial"]


async def main(duration: int, dump: bool) -> int:
    username = os.environ.get("BAYROL_USERNAME")
    password = os.environ.get("BAYROL_PASSWORD")
    if not username or not password:
        print("Set BAYROL_USERNAME and BAYROL_PASSWORD", file=sys.stderr)
        return 2

    async with aiohttp.ClientSession() as session:
        client = BayrolClient(session, username, password)
        await client.login()
        controllers = await client.get_controllers()
        if not controllers:
            print("No controllers", file=sys.stderr)
            return 1

        for c in controllers:
            log.info("Fetching device.php for cid=%s", c.cid)
            html = await client.get_device_html(c.cid)
            code = extract_direct_access_code(html)
            if not code:
                log.warning(
                    "No iframe code on cid=%s — likely a legacy control page",
                    c.cid,
                )
                continue

            log.info("Direct-access code for cid=%s = %s", c.cid, code)
            access_token, device_serial = await fetch_access_token(session, code)
            log.info(
                "/api/?code=… returned accessToken=%s… serial=%s",
                access_token[:8],
                device_serial,
            )

            dump_path: Path | None = None
            if dump:
                FIXTURES.mkdir(exist_ok=True)
                dump_path = FIXTURES / f"mqtt_{c.cid}.log"

            # Run paho on its own thread because it's not asyncio-native.
            t = threading.Thread(
                target=run_subscribe,
                args=(access_token, device_serial, duration, dump_path),
                daemon=True,
            )
            t.start()
            t.join()

    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--duration", type=int, default=30, help="seconds to capture (default 30)")
    p.add_argument("--dump", action="store_true", help="write captured messages to fixtures/")
    args = p.parse_args()
    raise SystemExit(asyncio.run(main(args.duration, args.dump)))
