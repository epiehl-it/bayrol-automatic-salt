#!/usr/bin/env python3
"""Probe the Bayrol cloud directly — no Home Assistant required.

Set BAYROL_USERNAME and BAYROL_PASSWORD in the environment and run::

    uv run --extra dev python tools/probe.py

Optionally pass ``--dump`` to write the raw HTML of every page to ``fixtures/``
so you can build/extend parser tests against real captures.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "custom_components"))

import aiohttp  # noqa: E402
from bayrol.api import BASE_URL, BayrolClient  # noqa: E402
from bayrol.parser import merge_pool_data  # noqa: E402

FIXTURES = REPO_ROOT / "fixtures"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


async def main(dump: bool) -> int:
    username = os.environ.get("BAYROL_USERNAME")
    password = os.environ.get("BAYROL_PASSWORD")
    if not username or not password:
        print("Set BAYROL_USERNAME and BAYROL_PASSWORD in the environment", file=sys.stderr)
        return 2

    async with aiohttp.ClientSession() as session:
        client = BayrolClient(session, username, password)

        print("→ login")
        await client.login()
        print("✓ logged in")

        print("→ list controllers")
        if dump:
            FIXTURES.mkdir(exist_ok=True)
            async with session.get(
                f"{BASE_URL}/m/plants.php",
                headers=client._headers(),  # noqa: SLF001 — debug-only access
            ) as resp:
                plants_html = await resp.text()
            (FIXTURES / "plants.html").write_text(plants_html, encoding="utf-8")
            print(f"  ↳ wrote fixtures/plants.html ({len(plants_html)} bytes)")

        controllers = await client.get_controllers()
        if not controllers:
            print("✗ no controllers on this account", file=sys.stderr)
            return 1
        for c in controllers:
            print(f"  • {c.name} (cid={c.cid}, model={c.device_model}, fw={c.device_version})")

        for c in controllers:
            print(f"→ getdata.php?cid={c.cid}")
            if dump:
                # Capture the raw HTML for fixture-based parser tests.
                async with session.get(
                    f"{BASE_URL}/getdata.php?cid={c.cid}",
                    headers=client._headers(  # noqa: SLF001 — debug-only access
                        {"Accept": "*/*", "X-Requested-With": "XMLHttpRequest"}
                    ),
                ) as resp:
                    raw = await resp.text()
                fixture = FIXTURES / f"getdata_{c.cid}.html"
                fixture.write_text(raw, encoding="utf-8")
                print(f"  ↳ wrote {fixture.relative_to(REPO_ROOT)} ({len(raw)} bytes)")

                device_html = await client.get_device_html(c.cid)
                device_fixture = FIXTURES / f"device_{c.cid}.html"
                device_fixture.write_text(device_html, encoding="utf-8")
                print(
                    f"  ↳ wrote {device_fixture.relative_to(REPO_ROOT)} "
                    f"({len(device_html)} bytes)"
                )

            data = await client.get_data(c.cid)
            flat = merge_pool_data(c, data)
            print(f"  status={flat.get('status')}")
            for k, v in sorted(flat.items()):
                if k == "status":
                    continue
                print(f"  {k}: {v}")

    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dump",
        action="store_true",
        help="Write raw HTML of getdata responses into fixtures/ for parser tests.",
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(main(args.dump)))
