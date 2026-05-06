"""Unit tests for the parser, exercising the shapes the Bayrol cloud actually returns."""

from __future__ import annotations

from pathlib import Path

from bayrol.parser import (
    is_login_error,
    merge_pool_data,
    parse_controllers,
    parse_device_items,
    parse_login_form,
    parse_overview,
    parse_pool_data,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

# A representative getdata.php fragment for an Automatic SALT.
SALT_GETDATA_HTML = """
<div class="tab_box stat_ok">
  <span>pH&nbsp;[ ]</span>
  <h1>7,12</h1>
</div>
<div class="tab_box stat_ok">
  <span>Redox&nbsp;[mV]</span>
  <h1>702</h1>
</div>
<div class="tab_box stat_ok">
  <span>T&nbsp;[°C]</span>
  <h1>26.4</h1>
</div>
<div class="tab_box stat_warning">
  <span>Salz&nbsp;[g/L]</span>
  <h1>3.20</h1>
</div>
"""


def test_pool_data_parses_salt_payload() -> None:
    data = parse_pool_data(SALT_GETDATA_HTML)
    assert data.status == "online"
    assert data.measurements["ph"].value == 7.12
    assert data.measurements["ph"].alarm is False
    assert data.measurements["redox"].value == 702.0
    assert data.measurements["temperature"].value == 26.4
    assert data.measurements["salt"].value == 3.20
    # stat_warning maps to alarm True.
    assert data.measurements["salt"].alarm is True


def test_pool_data_extracts_tab_info_from_getdata_response() -> None:
    """Real getdata.php payload from an Automatic SALT — the device-info block
    sits next to the measurement boxes, not in a separate request.
    """
    html = (
        '<div><div class="gapp_ase" onclick="gotoapp(28043)"><span>App Link<span></div>'
        '<div class="tab_data_link">'
        '<div class="gstat_ok"></div>'
        '<div class="tab_box stat_ok"><span>pH&nbsp;[pH]</span><h1>7.0</h1></div>'
        '<div class="tab_box stat_ok"><span>Redox&nbsp;[mV]</span><h1>853</h1></div>'
        '<div class="tab_box stat_ok"><span>Temp.&nbsp;[°C]</span><h1>15.5</h1></div>'
        '<div class="tab_box stat_ok"><span>Salt&nbsp;[g\\l]</span><h1>5.0</h1></div>'
        '<div class="tab_info">'
        "<span>23ASE2-05502</span></br>"
        "<span>Automatic SALT</span></br>"
        "<span>v1.53 (230524)</span></br>"
        '<span><a href="device.php?c=28043">Direct access</a></span>'
        "</div></div></div>"
    )
    data = parse_pool_data(html)
    assert data.status == "online"
    assert data.measurements["ph"].value == 7.0
    assert data.measurements["redox"].value == 853.0
    assert data.measurements["temperature"].value == 15.5
    assert data.measurements["salt"].value == 5.0
    assert data.info == {
        "device_id": "23ASE2-05502",
        "device_model": "Automatic SALT",
        "device_version": "v1.53 (230524)",
    }


def test_pool_data_with_real_fixture_if_present() -> None:
    """Parse a captured response if probe.py was run with --dump."""
    fixture = next(FIXTURES.glob("getdata_*.html"), None)
    if fixture is None:
        return
    data = parse_pool_data(fixture.read_text(encoding="utf-8"))
    assert data.status in {"online", "offline"}
    if data.status == "online":
        assert data.measurements, "online response must contain at least one measurement"


def test_parse_device_items_pairs_label_with_select() -> None:
    """Each i_x16 device label propagates to the next i_x7 select row."""
    html = """
    <div id="content_m">
      <div class="i_item item0_1"><div class="i_x16">Filterpumpe</div></div>
      <div class="i_item item3_153">
        <div class="i_x9">Betriebsart</div>
        <select class="i_x7">
          <option value="0">Aus</option>
          <option value="1" selected>Auto</option>
          <option value="2">Ein</option>
        </select>
      </div>
      <div class="i_item item0_2"><div class="i_x16">Elektrolyse</div></div>
      <div class="i_item item3_201">
        <div class="i_x9">Betriebsart</div>
        <select class="i_x7">
          <option value="0">Aus</option>
          <option value="1" selected>Auto</option>
          <option value="2">Boost</option>
        </select>
      </div>
    </div>
    """
    items = parse_device_items(html)
    assert [(i.topic, i.device, i.current_text) for i in items] == [
        ("3.153", "Filterpumpe", "Auto"),
        ("3.201", "Elektrolyse", "Auto"),
    ]
    assert items[1].options[2].text == "Boost"
    assert items[1].options[2].value == 2


def test_parse_device_items_skips_empty_selects() -> None:
    html = """
    <div id="content_m">
      <div class="i_item item0_1"><div class="i_x16">Empty</div></div>
      <div class="i_item item3_999">
        <div class="i_x9">Betriebsart</div>
        <select class="i_x7"></select>
      </div>
    </div>
    """
    assert parse_device_items(html) == []


def test_parse_device_items_with_real_fixture_if_present() -> None:
    fixture = next(FIXTURES.glob("device_*.html"), None)
    if fixture is None:
        return
    items = parse_device_items(fixture.read_text(encoding="utf-8"))
    # Don't assert specific topics — the point is to make sure the real layout
    # doesn't make the parser crash and yields *something* for write-capable
    # accounts. An empty list is acceptable for accounts that don't expose
    # any selects to begin with, but the topics we do find must be well-formed.
    for item in items:
        assert "." in item.topic
        assert item.options


def test_plants_html_yields_cid_even_when_shell_is_empty() -> None:
    """Real plants.php is a JS-rendered shell with empty tab_2 divs.

    parse_controllers must still recover the CID so the enrichment step in
    BayrolClient.get_controllers can fill in the rest.
    """
    fixture = FIXTURES / "plants.html"
    if not fixture.exists():
        return
    controllers = parse_controllers(fixture.read_text(encoding="utf-8"))
    assert controllers, "plants page must yield at least one controller"
    for c in controllers:
        assert c.cid.isdigit()


def test_pool_data_offline_marker() -> None:
    html = """
    <div class="tab_error">
      No connection to the controller since 04.05.26, 10:13 UTC
    </div>
    """
    data = parse_pool_data(html)
    assert data.status == "offline"
    assert data.last_seen == "04.05.26, 10:13"


def test_login_form_extracts_hidden_fields() -> None:
    html = """
    <form id="form_login" method="post">
      <input name="csrf" value="abc123">
      <input name="username" value="">
      <input name="password" value="">
      <input name="login" value="Login" type="submit">
    </form>
    """
    fields = parse_login_form(html)
    assert fields == {"csrf": "abc123", "username": "", "password": "", "login": "Login"}


def test_login_error_detection() -> None:
    assert is_login_error("<div class='error_text'>Fehler beim Login</div>")
    assert not is_login_error("<html><body>Welcome</body></html>")


def test_parse_controllers_extracts_id_and_model() -> None:
    html = """
    <div class="tab_row">
      <div class="tab_1"><p>Mein Pool</p>
        <div onclick="location.href='plant_settings.php?c=12345'"></div>
      </div>
      <div class="tab_2" id="tab_data12345">
        <div class="tab_info">
          <span>23ASE2-05502</span>
          <span>Automatic SALT</span>
          <span>v1.53 (230524)</span>
        </div>
      </div>
    </div>
    """
    controllers = parse_controllers(html)
    assert len(controllers) == 1
    c = controllers[0]
    assert c.cid == "12345"
    assert c.name == "Mein Pool"
    assert c.device_model == "Automatic SALT"
    assert c.device_id == "23ASE2-05502"
    assert c.device_version == "v1.53 (230524)"


def test_parse_controllers_falls_back_when_name_blank() -> None:
    """If the user never set a controller label, use the device model."""
    html = """
    <div class="tab_row">
      <div class="tab_1"><p></p>
        <div onclick="location.href='plant_settings.php?c=28043'"></div>
      </div>
      <div class="tab_2" id="tab_data28043">
        <div class="tab_info">
          <span>23ASE2-05502</span>
          <span>Automatic SALT</span>
          <span>v1.53</span>
        </div>
      </div>
    </div>
    """
    [c] = parse_controllers(html)
    assert c.cid == "28043"
    assert c.name == "Automatic SALT"


def test_merge_pool_data_uses_getdata_info_when_plants_lacks_it() -> None:
    """merge_pool_data must surface device info even when only getdata has it."""
    from bayrol.parser import Controller

    controller = Controller(cid="28043", name="Bayrol Pool")
    pool = parse_pool_data(SALT_GETDATA_HTML)
    pool.info = {
        "device_id": "23ASE2-05502",
        "device_model": "Automatic SALT",
        "device_version": "v1.53 (230524)",
    }
    flat = merge_pool_data(controller, pool)
    assert flat["device_id"] == "23ASE2-05502"
    assert flat["device_model"] == "Automatic SALT"
    assert flat["device_version"] == "v1.53 (230524)"
    assert flat["name"] == "Bayrol Pool"


def test_parse_overview_combines_info_and_measurements() -> None:
    html = f"""
    <div class="tab_row">
      <div class="tab_1"><p>Pool</p>
        <div onclick="location.href='plant_settings.php?c=12345'"></div>
      </div>
      <div class="tab_2" id="tab_data12345">
        <div class="tab_info">
          <span>23ASE2-05502</span>
          <span>Automatic SALT</span>
          <span>v1.53</span>
        </div>
        {SALT_GETDATA_HTML}
      </div>
    </div>
    """
    by_cid = parse_overview(html)
    assert "12345" in by_cid
    pool = by_cid["12345"]
    assert pool.status == "online"
    assert pool.measurements["ph"].value == 7.12
    assert pool.info["device_model"] == "Automatic SALT"
