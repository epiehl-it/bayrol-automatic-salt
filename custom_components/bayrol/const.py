"""Constants for the Bayrol Pool Access integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "bayrol"

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_SETTINGS_PIN = "settings_pin"
CONF_REFRESH_INTERVAL = "refresh_interval"

DEFAULT_REFRESH_INTERVAL = timedelta(minutes=5)
MIN_REFRESH_INTERVAL = timedelta(seconds=30)

# Cloud endpoint
BASE_URL = "https://www.bayrol-poolaccess.de/webview"

# Measurement keys we expose. Source labels in the HTML are mapped to these.
KEY_PH = "ph"
KEY_REDOX = "redox"
KEY_TEMPERATURE = "temperature"
KEY_SALT = "salt"
KEY_CHLORINE = "chlorine"

# Device-info keys
KEY_DEVICE_ID = "device_id"
KEY_DEVICE_MODEL = "device_model"
KEY_DEVICE_VERSION = "device_version"
KEY_STATUS = "status"
KEY_NAME = "name"

STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"

# Mapping from raw HTML label (German + English variants) to our canonical key.
# Bayrol's web UI uses the same labels across firmware versions; new variants
# show up over time, hence the open-ended dict.
LABEL_MAP: dict[str, str] = {
    "pH": KEY_PH,
    "Redox": KEY_REDOX,
    "mV": KEY_REDOX,
    "ORP": KEY_REDOX,
    "Temp.": KEY_TEMPERATURE,
    "Temp": KEY_TEMPERATURE,
    "T": KEY_TEMPERATURE,
    "T1": KEY_TEMPERATURE,
    "Salz": KEY_SALT,
    "Salt": KEY_SALT,
    "Cl": KEY_CHLORINE,
}
