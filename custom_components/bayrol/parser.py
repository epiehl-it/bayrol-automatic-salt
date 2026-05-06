"""HTML parsing for the Bayrol Pool Access cloud responses.

The cloud delivers everything as HTML fragments. Three pages matter:

* ``/m/login.php`` — initial login form, contains hidden tokens we must echo back.
* ``/p/plants.php`` (or ``/m/plants.php``) — overview of all controllers on the account.
* ``/getdata.php?cid=<CID>`` — small HTML fragment with the live measurement boxes.

This module is pure (no I/O) so it stays easy to unit-test against captured fixtures.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

from .const import (
    KEY_DEVICE_ID,
    KEY_DEVICE_MODEL,
    KEY_DEVICE_VERSION,
    KEY_NAME,
    KEY_STATUS,
    LABEL_MAP,
    STATUS_OFFLINE,
    STATUS_ONLINE,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Controller:
    """A single controller as listed on the plants page."""

    cid: str
    name: str
    device_model: str | None = None
    device_id: str | None = None
    device_version: str | None = None


@dataclass(slots=True)
class Measurement:
    """One measurement value with its alarm flag."""

    value: float
    alarm: bool = False


@dataclass(slots=True)
class PoolData:
    """Parsed data for a single controller."""

    status: str = STATUS_ONLINE
    measurements: dict[str, Measurement] = field(default_factory=dict)
    info: dict[str, str] = field(default_factory=dict)
    last_seen: str | None = None


@dataclass(slots=True)
class DeviceItemOption:
    """A single selectable value for a controllable item."""

    value: int
    text: str


@dataclass(slots=True)
class DeviceItem:
    """A controllable item on the device page (a select tied to a device label).

    ``topic`` is the dotted form Bayrol uses to identify the item in setItems
    requests, e.g. ``"3.153"`` — derived from the HTML class ``item3_153``.
    ``device`` is the human label of the *device* this control belongs to
    (e.g. "Filterpumpe", "Elektrolyse"); ``label`` is the operation name on
    the control itself (e.g. "Betriebsart").
    """

    topic: str
    device: str
    label: str
    options: list[DeviceItemOption] = field(default_factory=list)
    current_value: int | None = None
    current_text: str | None = None

    @property
    def slug(self) -> str:
        """Stable identifier for HA unique_ids — based on topic, not labels."""
        return self.topic.replace(".", "_")


def parse_login_form(html: str) -> dict[str, str] | None:
    """Extract hidden field name/value pairs from the login form."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", {"id": "form_login"})
    if not form:
        _LOGGER.error("Login form not found in HTML")
        return None
    fields: dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            fields[name] = inp.get("value", "")
    return fields


def is_login_error(html: str) -> bool:
    """Return True if the response indicates a failed login."""
    if "Fehler" not in html and "Zeit abgelaufen" not in html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    err = soup.find("div", class_="error_text")
    if err:
        _LOGGER.error("Bayrol login error: %s", err.get_text(strip=True))
    return True


_CID_FROM_TAB_RE = re.compile(r"tab_data(\d+)")
_CID_FROM_ONCLICK_RE = re.compile(r"c=(\d+)")
_LAST_SEEN_RE = re.compile(r"since (\d{2}\.\d{2}\.\d{2}, \d{2}:\d{2}) UTC")


def parse_controllers(html: str) -> list[Controller]:
    """Parse the controller list from the plants page."""
    soup = BeautifulSoup(html, "html.parser")
    controllers: list[Controller] = []

    for row in soup.find_all("div", class_="tab_row"):
        cid = _extract_cid(row)
        if not cid:
            continue

        info = _extract_device_info(row)

        # Preferred name: the user-set label in the plants list.
        # Some accounts leave the <p> blank — fall back to the device model,
        # then a generic placeholder, so we never produce an empty string.
        name = ""
        tab_1 = row.find("div", class_="tab_1")
        if tab_1 and (p := tab_1.find("p")):
            name = p.get_text(strip=True)
        if not name:
            name = info.get(KEY_DEVICE_MODEL) or "Bayrol Pool"

        controllers.append(
            Controller(
                cid=cid,
                name=name,
                device_id=info.get(KEY_DEVICE_ID),
                device_model=info.get(KEY_DEVICE_MODEL),
                device_version=info.get(KEY_DEVICE_VERSION),
            )
        )

    return controllers


def parse_pool_data(html: str) -> PoolData:
    """Parse the small HTML fragment returned by ``/getdata.php?cid=<CID>``.

    Real responses include a ``tab_info`` block with device id / model / firmware
    next to the measurement boxes — we lift that out so the integration doesn't
    need a second request to populate Home Assistant's device registry.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Offline marker — Bayrol returns a tab_error div when the controller is unreachable.
    error_div = soup.find("div", class_="tab_error")
    if error_div and "No connection" in error_div.get_text():
        last_seen_match = _LAST_SEEN_RE.search(error_div.get_text())
        return PoolData(
            status=STATUS_OFFLINE,
            last_seen=last_seen_match.group(1) if last_seen_match else None,
            info=_parse_tab_info(soup),
        )

    return PoolData(
        status=STATUS_ONLINE,
        measurements=_parse_measurements(soup),
        info=_parse_tab_info(soup),
    )


def parse_overview(html: str) -> dict[str, PoolData]:
    """Parse measurements for every controller from the plants overview page.

    Used as a fallback when the lightweight ``getdata.php`` endpoint fails or is
    blocked (some Bayrol accounts only render values on the overview page).
    """
    soup = BeautifulSoup(html, "html.parser")
    results: dict[str, PoolData] = {}

    for row in soup.find_all("div", class_="tab_row"):
        cid = _extract_cid(row)
        if not cid:
            continue

        tab_2 = row.find("div", class_="tab_2")
        if not tab_2:
            continue

        # Controller-level offline marker on the overview page.
        error_div = tab_2.find("div", class_="tab_error")
        if error_div and "No connection" in error_div.get_text():
            last_seen_match = _LAST_SEEN_RE.search(error_div.get_text())
            results[cid] = PoolData(
                status=STATUS_OFFLINE,
                last_seen=last_seen_match.group(1) if last_seen_match else None,
                info=_extract_device_info(row),
            )
            continue

        results[cid] = PoolData(
            status=STATUS_ONLINE,
            measurements=_parse_measurements(tab_2),
            info=_extract_device_info(row),
        )

    return results


def _extract_cid(row: Any) -> str | None:
    tab_2 = row.find("div", class_="tab_2")
    tab_id = tab_2.get("id") if tab_2 else None
    if tab_id and (m := _CID_FROM_TAB_RE.search(tab_id)):
        return m.group(1)

    tab_1 = row.find("div", class_="tab_1")
    if tab_1:
        clickable = tab_1.find("div", onclick=re.compile(r"plant_settings\.php\?c=\d+"))
        if clickable and (m := _CID_FROM_ONCLICK_RE.search(clickable.get("onclick", ""))):
            return m.group(1)
    return None


def _extract_device_info(row: Any) -> dict[str, str]:
    """Pull device id/model/firmware from the ``tab_info`` block under a tab_row."""
    return _parse_tab_info(row.find("div", class_="tab_2") or row)


def _parse_tab_info(scope: Any) -> dict[str, str]:
    """Read the first ``tab_info`` block found in ``scope``.

    Layout: three meaningful ``<span>`` siblings — device id, model, firmware.
    A trailing fourth ``<span>`` wraps the "Direct access" link and is ignored
    (we detect it by the presence of an ``<a>`` child).
    """
    info: dict[str, str] = {}
    tab_info = scope.find("div", class_="tab_info") if scope is not None else None
    if not tab_info:
        return info
    text_spans: list[str] = []
    for span in tab_info.find_all("span"):
        if span.find("a"):
            continue
        text = span.get_text(strip=True)
        if text:
            text_spans.append(text)
    if len(text_spans) >= 1:
        info[KEY_DEVICE_ID] = text_spans[0]
    if len(text_spans) >= 2:
        info[KEY_DEVICE_MODEL] = text_spans[1]
    if len(text_spans) >= 3:
        info[KEY_DEVICE_VERSION] = text_spans[2]
    return info


def _parse_measurements(scope: Any) -> dict[str, Measurement]:
    """Read every ``tab_box`` measurement tile in ``scope``."""
    out: dict[str, Measurement] = {}
    for box in scope.find_all("div", class_="tab_box"):
        span = box.find("span")
        h1 = box.find("h1")
        if not span or not h1:
            continue
        raw_label = re.match(r"^([^[]+)", span.get_text(strip=True))
        if not raw_label:
            continue
        label = raw_label.group(1).replace("\xa0", " ").strip()
        key = LABEL_MAP.get(label)
        if not key:
            _LOGGER.debug("Unknown measurement label: %r", label)
            continue
        try:
            value = float(h1.get_text(strip=True).replace(",", "."))
        except ValueError:
            _LOGGER.warning("Could not parse value for %s: %r", label, h1.get_text(strip=True))
            continue
        classes = box.get("class", [])
        alarm = "stat_alarm" in classes or "stat_warning" in classes
        out[key] = Measurement(value=value, alarm=alarm)
    return out


_ITEM_CLASS_RE = re.compile(r"^item(\d+)_(\d+)$")
_IFRAME_CODE_RE = re.compile(r"index\.html\?code=([A-Za-z0-9\-_]+)")


def extract_direct_access_code(html: str) -> str | None:
    """Pull the per-device MQTT auth token out of ``device.php``.

    Newer Bayrol controllers (Automatic SALT firmware ≥ 1.5x) replaced the
    ``i_item``-based control HTML with a single iframe that loads the SPA::

        <iframe src="../../app/index.html?code=A-EyA9U2&direct" …>

    The ``code`` parameter is the MQTT username the SPA uses to talk to
    ``wss://www.bayrol-poolaccess.de:8083``. We extract it so the integration
    can open that same channel for live readings and control.
    """
    if m := _IFRAME_CODE_RE.search(html):
        return m.group(1)
    return None


def parse_device_items(html: str) -> list[DeviceItem]:
    """Extract every controllable ``<select>`` item from ``device.php`` HTML.

    Bayrol's UI lays each item out as a pair of ``i_item`` divs:

    1. Device label — contains a ``<div class="i_x16">`` with the device name
       (Filterpumpe / Elektrolyse / Boost / …).
    2. Control row — ``class="i_item itemX_Y"`` carrying:
       * a ``<div class="i_x9">`` with the operation name ("Betriebsart"…),
       * a ``<select class="i_x7">`` whose ``<option>``s are the modes.

    The ``itemX_Y`` class becomes Bayrol's topic ``X.Y`` for setItems writes.
    Devices with multiple controls produce one DeviceItem per control row;
    the device label propagates from the most recent ``i_x16`` we saw.
    """
    soup = BeautifulSoup(html, "html.parser")
    items: list[DeviceItem] = []

    # The device page wraps controls inside #content_m on most layouts; fall
    # back to the whole document if that container isn't present.
    scope = soup.find("div", id="content_m") or soup

    current_device: str | None = None
    for div in scope.find_all("div", class_="i_item"):
        # A device-label row holds an i_x16 child and no select.
        device_div = div.find("div", class_="i_x16")
        if device_div and not div.find("select"):
            current_device = device_div.get_text(strip=True)
            continue

        select = div.find("select", class_="i_x7")
        if not select:
            continue

        topic = _topic_from_classes(div.get("class", []))
        if not topic:
            continue

        op_div = div.find("div", class_="i_x9")
        op_label = op_div.get_text(strip=True) if op_div else ""

        options, current_value, current_text = _parse_select_options(select)
        if not options:
            continue

        items.append(
            DeviceItem(
                topic=topic,
                device=current_device or "Bayrol",
                label=op_label,
                options=options,
                current_value=current_value,
                current_text=current_text,
            )
        )

    return items


def _topic_from_classes(classes: list[str]) -> str | None:
    for cls in classes:
        if m := _ITEM_CLASS_RE.match(cls):
            return f"{m.group(1)}.{m.group(2)}"
    return None


def _parse_select_options(
    select: Any,
) -> tuple[list[DeviceItemOption], int | None, str | None]:
    options: list[DeviceItemOption] = []
    current_value: int | None = None
    current_text: str | None = None
    for option in select.find_all("option"):
        raw_value = option.get("value")
        if raw_value is None:
            continue
        try:
            value = int(raw_value)
        except ValueError:
            continue
        text = option.get_text(strip=True)
        options.append(DeviceItemOption(value=value, text=text))
        # ``selected`` may be present without a value — use ``has_attr``.
        if option.has_attr("selected"):
            current_value = value
            current_text = text
    options.sort(key=lambda o: o.value)
    return options, current_value, current_text


def merge_pool_data(controller: Controller, data: PoolData) -> dict[str, Any]:
    """Flatten a Controller + PoolData into a coordinator-friendly dict."""
    flat: dict[str, Any] = {
        KEY_STATUS: data.status,
        KEY_NAME: controller.name,
    }
    if controller.device_id:
        flat[KEY_DEVICE_ID] = controller.device_id
    if controller.device_model:
        flat[KEY_DEVICE_MODEL] = controller.device_model
    if controller.device_version:
        flat[KEY_DEVICE_VERSION] = controller.device_version
    for k, v in data.info.items():
        flat.setdefault(k, v)
    if data.last_seen:
        flat["last_seen"] = data.last_seen
    for key, m in data.measurements.items():
        flat[key] = m.value
        flat[f"{key}_alarm"] = m.alarm
    return flat
