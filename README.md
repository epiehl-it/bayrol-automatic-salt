# Bayrol Pool Access — Home Assistant Integration

Custom integration for [Bayrol](https://www.bayrol.com/) pool controllers
(Automatic SALT, Automatic Cl-pH, PoolManager, PoolRelax) that exposes the
live measurements from the Bayrol Pool Access cloud as Home Assistant
sensors.

Built and tested against an **Automatic SALT** (`23ASE2-05502`, FW `v1.53`).

## What it does

- Logs into <https://www.bayrol-poolaccess.de> with your account email/password.
- Discovers every controller registered to that account.
- Polls `getdata.php?cid=<CID>` on a configurable interval (default 5 min).
- Surfaces what the device reports as sensors and matching alarm binary sensors:
  - pH
  - Redox / ORP (mV)
  - Water temperature (°C)
  - Salt (g/L) — Automatic SALT
  - Chlorine (mg/L) — Cl-pH / PoolManager Cl

Sensors are only created for the measurements your specific device reports.

## Installation

1. Copy `custom_components/bayrol/` into your Home Assistant
   `config/custom_components/` directory.
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → "Bayrol Pool Access"**.
4. Enter the email and password you use on `bayrol-poolaccess.de`.

The integration discovers your controllers automatically — no need to find a
controller ID by hand. After setup you can adjust the refresh interval under
the integration's *Configure* button (minimum 30 s).

## Repo layout

```
custom_components/bayrol/    HA integration (drop into config/custom_components/)
  api.py                     async HTTP client (login + getdata + overview fallback)
  parser.py                  pure HTML parser (no I/O — easy to test)
  coordinator.py             DataUpdateCoordinator
  config_flow.py             setup + reauth + options flow
  sensor.py / binary_sensor.py
  const.py
  translations/

tools/probe.py               Run the API client outside HA, optionally dump fixtures
tests/test_parser.py         Pure-python parser tests (no HA required)
fixtures/                    Captured HTML for parser fixture tests (gitignored)
```

## Local development

```bash
# install deps into the existing .venv
uv pip install --python .venv/bin/python "aiohttp>=3.9" "beautifulsoup4>=4.12" \
                                          "pytest>=8.0" "pytest-asyncio>=0.23" "ruff>=0.6"

# parser unit tests (no HA, no network)
.venv/bin/python -m pytest

# run the live cloud probe (needs your real credentials)
export BAYROL_USERNAME=you@example.com
export BAYROL_PASSWORD='…'
.venv/bin/python tools/probe.py            # print live values
.venv/bin/python tools/probe.py --dump     # also save raw HTML to fixtures/

.venv/bin/python -m ruff check custom_components/bayrol tools tests
```

If the cloud's HTML changes shape and a measurement disappears, run
`tools/probe.py --dump` and inspect the captured `fixtures/getdata_<cid>.html`.
Add the new label to `LABEL_MAP` in `custom_components/bayrol/const.py` and
extend `tests/test_parser.py` with a fixture-based regression test.

## Why a polling cloud client and not MQTT?

Bayrol's app actually talks to the same backend over MQTT-over-WebSocket
(`wss://www.bayrol-poolaccess.de:8083/`, topic `d02/<DeviceId>/#`), which would
give push updates instead of 5-minute polling. Credentials for that broker
have to be extracted from the browser's dev-tools the first time, which makes
config-flow setup ugly. The HTTP path uses normal email/password auth and is
robust enough for water chemistry — pH/Redox don't change minute by minute.

The package is structured so an MQTT transport can be added later without
disturbing the parser, sensor descriptions, or coordinator wiring.

## Credits / prior art

- [`razem-io/ha-bayrol-cloud`](https://github.com/razem-io/ha-bayrol-cloud) —
  reference for the HTML scraping flow and login form quirks.
- [`Duntch144/Bayrol-AS-2-MQTT`](https://github.com/Duntch144/Bayrol-AS-2-MQTT) —
  reference for the MQTT-over-WebSocket cloud topic layout.

This integration is an independent rewrite, not a fork; the cloud protocol is
the same one those projects already documented.
