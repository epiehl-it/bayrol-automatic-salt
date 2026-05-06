"""Curated Bayrol MQTT topic catalog.

Each entry maps a Bayrol cloud topic ``<type>.<id>`` to the metadata Home
Assistant needs to expose it as the right entity. Names and ids come from the
SPA's ``NumStringsExternal`` / ``EnumStringsExternal`` and verified against a
live capture of an Automatic SALT (firmware ≥ 1.5).

Topic types of interest:

* ``4`` — Num (numeric values: setpoints, sensor readings, progress counters)
* ``5`` — Enum (modes: Auto/Off/Boost/Manual, opmode flags)

Values come back as JSON. For Num topics the ``v`` field is an integer that
needs scaling by ``factor`` to render the human number (e.g. pH is stored as
``72`` and displayed as ``7.2``). For Enum topics the ``v`` field is the string
``"19.<choice>"`` — Bayrol stamps every enum payload with the
``e_data_type_enum_value`` type tag (``19``); the trailing integer is the
actual selection.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Topic-type IDs from the SPA's Device.js TopicIDs enum.
TOPIC_TYPE_NUM = 4
TOPIC_TYPE_ENUM = 5
TOPIC_TYPE_STRING = 6
TOPIC_TYPE_CODE = 7
TOPIC_TYPE_FUNCTION = 13

# Enum payloads carry this tag prefix (e_data_type_enum_value=19).
ENUM_VALUE_TAG = 19


@dataclass(frozen=True, slots=True)
class NumTopic:
    """A numeric Bayrol item (sensor reading, setpoint, or progress counter)."""

    type_id: int
    item_id: int
    key: str
    name: str
    factor: float = 1.0
    unit: str | None = None
    writable: bool = False
    icon: str | None = None
    device_class: str | None = None
    state_class: str | None = "measurement"

    @property
    def topic(self) -> str:
        return f"{self.type_id}.{self.item_id}"


@dataclass(frozen=True, slots=True)
class EnumChoice:
    value: int
    label: str


@dataclass(frozen=True, slots=True)
class EnumTopic:
    """An enum item (mode select)."""

    type_id: int
    item_id: int
    key: str
    name: str
    choices: tuple[EnumChoice, ...] = field(default_factory=tuple)
    writable: bool = False
    icon: str | None = None

    @property
    def topic(self) -> str:
        return f"{self.type_id}.{self.item_id}"


@dataclass(frozen=True, slots=True)
class ButtonTopic:
    """A one-shot trigger backed by an enum write.

    The Bayrol GUI exposes actions like 'Start Boost' as a write to a
    ``*_activate_*_gui`` enum. Sending ``trigger_value`` to that topic
    is what the on-device button does.
    """

    type_id: int
    item_id: int
    key: str
    name: str
    trigger_value: int
    icon: str | None = None

    @property
    def topic(self) -> str:
        return f"{self.type_id}.{self.item_id}"


# ---- Sensor / setpoint definitions ---------------------------------------

# Stored as (e.g.) 72 → display 7.2 → factor 0.1.
_PH_FACTOR = 0.1
# Temperature stored as ×10 (155 → 15.5 °C).
_T_FACTOR = 0.1
# Salt stored as ×0.1 (50 → 5.0 g/L).
_SALT_FACTOR = 0.1

NUM_TOPICS: tuple[NumTopic, ...] = (
    # ---- Sensor readings (read-only) ----
    NumTopic(4, 78, "ph_value", "pH", _PH_FACTOR),
    NumTopic(4, 99, "temperature_value", "Water temperature", _T_FACTOR, "°C"),
    NumTopic(4, 100, "salt_value", "Salt", _SALT_FACTOR, "g/L"),
    NumTopic(4, 101, "salt_display", "Salt display", _SALT_FACTOR, "g/L"),
    NumTopic(4, 137, "salt_pool_level", "Salt pool level", _SALT_FACTOR, "g/L"),
    NumTopic(4, 138, "salt_to_add", "Salt to add", 1.0, "g"),
    NumTopic(4, 103, "conductivity", "Conductivity"),
    NumTopic(4, 204, "redox_value", "Redox", 1.0, "mV"),
    NumTopic(
        4,
        155,
        "boost_remaining_min",
        "Boost remaining",
        1.0,
        "min",
        icon="mdi:timer-outline",
    ),
    # ---- Setpoints (writable) ----
    NumTopic(4, 2, "ph_setpoint", "pH setpoint", _PH_FACTOR, writable=True, icon="mdi:ph"),
    NumTopic(
        4,
        3,
        "ph_upper_limit",
        "pH upper alarm",
        _PH_FACTOR,
        writable=True,
        icon="mdi:alert-outline",
    ),
    NumTopic(
        4,
        4,
        "ph_lower_limit",
        "pH lower alarm",
        _PH_FACTOR,
        writable=True,
        icon="mdi:alert-outline",
    ),
    NumTopic(
        4,
        28,
        "redox_setpoint",
        "Redox setpoint",
        1.0,
        "mV",
        writable=True,
        icon="mdi:flash",
    ),
    NumTopic(
        4,
        26,
        "redox_upper_limit",
        "Redox upper alarm",
        1.0,
        "mV",
        writable=True,
        icon="mdi:alert-outline",
    ),
    NumTopic(
        4,
        123,
        "temperature_setpoint",
        "Temperature setpoint",
        _T_FACTOR,
        "°C",
        writable=True,
    ),
    NumTopic(
        4,
        124,
        "temperature_upper_limit",
        "Temperature upper alarm",
        _T_FACTOR,
        "°C",
        writable=True,
        icon="mdi:alert-outline",
    ),
)


ENUM_TOPICS: tuple[EnumTopic, ...] = (
    # Operating mode containers — read-only status mirrors of the device's UI.
    EnumTopic(5, 27, "opmode", "Operating mode"),
    EnumTopic(5, 41, "se_opmode", "Electrolysis mode"),
    EnumTopic(5, 81, "var_se_opmode", "Electrolysis status"),
    EnumTopic(5, 130, "boost_active", "Boost active"),
    EnumTopic(5, 142, "ph_opmode", "pH dosing mode"),
    EnumTopic(5, 155, "redox_opmode", "Redox dosing mode"),
)


BUTTON_TOPICS: tuple[ButtonTopic, ...] = (
    # The activate-*-gui enums are write-only triggers in the Bayrol UI:
    # writing the corresponding "on" value tells the controller to enter the
    # mode. Trigger values come from the SPA's e_enum_se_activate_boost_gui
    # definition — verified empirically: idle state reports value 18, the GUI
    # write toggles to 1 to start a boost cycle.
    ButtonTopic(5, 104, "boost", "Start Boost", trigger_value=1, icon="mdi:rocket-launch"),
    ButtonTopic(
        5,
        105,
        "manual",
        "Activate manual",
        trigger_value=1,
        icon="mdi:hand-back-right",
    ),
    ButtonTopic(
        5,
        106,
        "pause",
        "Activate pause",
        trigger_value=1,
        icon="mdi:pause-circle-outline",
    ),
)


def all_subscribe_topics() -> tuple[str, ...]:
    """Topics we need to subscribe to in order to render every entity."""
    return tuple(
        f"{t.type_id}.{t.item_id}"
        for t in (*NUM_TOPICS, *ENUM_TOPICS, *BUTTON_TOPICS)
    )


def parse_enum_value(raw: str) -> int | None:
    """Parse the ``"19.<value>"`` enum payload into the integer choice."""
    if not raw or "." not in raw:
        return None
    tag, _, val = raw.partition(".")
    try:
        if int(tag) != ENUM_VALUE_TAG:
            return None
        return int(val)
    except ValueError:
        return None


def encode_enum_value(value: int) -> str:
    """Build the ``"19.<value>"`` string the cloud expects on writes."""
    return f"{ENUM_VALUE_TAG}.{int(value)}"
