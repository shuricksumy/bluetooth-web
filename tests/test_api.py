"""End-to-end tests against a fake bluetoothctl (see mock_bluetoothctl.py).

Nothing here needs a Bluetooth adapter, a D-Bus bus or root, so it runs in CI.
"""
import importlib
import os
import re
import stat
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOCK = os.path.join(ROOT, "tests", "mock_bluetoothctl.py")
sys.path.insert(0, ROOT)


def load_app(mode="ok", start_timeout=None, **env):
    """Import a fresh app bound to the mock in the requested scenario."""
    for key in ("ADMIN_PASSWORD", "ADMIN_USER", "MOCK_MODE",
                "SNAPSERVER_HOST", "SNAPSERVER_PORT", "SNAPSERVER_CONTROL_PORT",
                "SNAPSERVER_WEB_PORT"):
        os.environ.pop(key, None)
    os.environ["MOCK_MODE"] = mode
    os.environ["BLUETOOTHCTL"] = "%s %s" % (sys.executable, MOCK)
    os.environ.update(env)

    # players and snapctl read their defaults at import time, so they have to
    # be reloaded too or env-driven settings silently keep the previous values.
    for name in ("app", "btctl", "players", "snapctl"):
        sys.modules.pop(name, None)
    btctl = importlib.import_module("btctl")
    if start_timeout is not None:
        btctl.START_TIMEOUT = start_timeout
    app_module = importlib.import_module("app")
    return app_module


@pytest.fixture
def client():
    app_module = load_app("ok")
    yield app_module.app.test_client()
    app_module.btctl.close()


# ---- listing ----------------------------------------------------------------


def test_devices_listed_with_state(client):
    body = client.get("/api/devices").get_json()
    by_mac = {d["mac"]: d for d in body["devices"]}
    assert len(by_mac) == 3
    assert by_mac["AA:BB:CC:DD:EE:01"] == {
        "mac": "AA:BB:CC:DD:EE:01",
        "name": "Topping DX5",
        "named": True,
        "paired": True,
        "connected": True,
        "trusted": True,
        # Derived so the Add-player form can prefill PIPEWIRE_NODE.
        "node": "bluez_output.AA_BB_CC_DD_EE_01.1",
    }
    assert by_mac["AA:BB:CC:DD:EE:03"]["paired"] is False
    assert body["warnings"] == []


def test_connected_devices_sort_first(client):
    devices = client.get("/api/devices").get_json()["devices"]
    assert devices[0]["mac"] == "AA:BB:CC:DD:EE:01"


def test_index_page_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"Bluetooth Panel" in res.data


# ---- MAC validation ---------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "AA:BB:CC:DD:EE:01%0Ascan%20on",  # newline + a smuggled second command
        "AA:BB:CC:DD:EE:01%0A",           # bare trailing newline: the re.match trap
        "AA:BB:CC:DD:EE:0G",              # not hex
        "AA:BB:CC:DD:EE",                 # too short
        "AA:BB:CC:DD:EE:01:FF",           # too long
        "%20AA:BB:CC:DD:EE:01",           # leading space
        "AA-BB-CC-DD-EE-01",              # wrong separator
    ],
)
@pytest.mark.parametrize("route", ["connect", "disconnect", "pair", "trust", "remove"])
def test_bad_mac_rejected_before_reaching_the_repl(client, route, payload):
    res = client.post("/api/%s/%s" % (route, payload))
    assert res.status_code == 400
    assert res.get_json()["error"] == "invalid MAC address"


def test_injection_attempt_leaves_state_untouched(client):
    client.post("/api/connect/AA:BB:CC:DD:EE:01%0Aremove%20AA:BB:CC:DD:EE:02")
    macs = {d["mac"] for d in client.get("/api/devices").get_json()["devices"]}
    assert "AA:BB:CC:DD:EE:02" in macs


def test_lowercase_mac_accepted_and_normalised(client):
    body = client.post("/api/connect/aa:bb:cc:dd:ee:02").get_json()
    assert body["ok"] is True
    assert any(d["mac"] == "AA:BB:CC:DD:EE:02" and d["connected"] for d in body["devices"])


# ---- actions ----------------------------------------------------------------


def test_quick_pair_runs_pair_trust_connect(client):
    body = client.post("/api/pair/AA:BB:CC:DD:EE:03").get_json()
    assert [s["step"] for s in body["steps"]] == ["pair", "trust", "connect"]
    assert all(s["ok"] for s in body["steps"])
    assert body["steps"][0]["output"] == "Pairing successful"
    device = next(d for d in body["devices"] if d["mac"] == "AA:BB:CC:DD:EE:03")
    assert (device["paired"], device["trusted"], device["connected"]) == (True, True, True)


def test_disconnect_and_remove(client):
    assert client.post("/api/disconnect/AA:BB:CC:DD:EE:01").get_json()["ok"] is True
    body = client.post("/api/remove/AA:BB:CC:DD:EE:01").get_json()
    assert not any(d["mac"] == "AA:BB:CC:DD:EE:01" for d in body["devices"])


def test_unknown_device_gives_a_clear_error(client):
    res = client.post("/api/connect/11:22:33:44:55:66")
    assert res.status_code == 502
    assert "not available" in res.get_json()["error"]


def test_scan_discovers_new_devices(client):
    body = client.post("/api/scan", json={"duration": 1}).get_json()
    found = {d["mac"]: d["name"] for d in body["devices"]}
    assert found.get("AA:BB:CC:DD:EE:04") == "Sony WH-1000XM4"
    # Async "[NEW] Device ..." lines must not be mistaken for listing output.
    assert all(not name.startswith("[") for name in found.values())


@pytest.mark.parametrize("duration", ["abc", -5, 0, None])
def test_scan_rejects_bad_durations(client, duration):
    assert client.post("/api/scan", json={"duration": duration}).status_code == 400


def test_scan_duration_is_capped(monkeypatch):
    app_module = load_app("ok")
    monkeypatch.setattr(app_module, "MAX_SCAN_SECONDS", 2)
    body = app_module.app.test_client().post(
        "/api/scan", json={"duration": 9999}
    ).get_json()
    assert body["scanned"] == 2


# ---- degraded backends ------------------------------------------------------


def test_missing_bluetoothctl_binary():
    os.environ["BLUETOOTHCTL"] = "/nonexistent/bluetoothctl"
    for name in ("app", "btctl"):
        sys.modules.pop(name, None)
    import app as app_module

    res = app_module.app.test_client().get("/api/devices")
    body = res.get_json()
    assert res.status_code == 503
    assert body["devices"] == []
    assert "cannot start" in body["error"]


def test_bluetoothctl_exits_immediately_points_at_the_dbus_mount(tmp_path):
    missing = str(tmp_path / "no_such_bus_socket")
    app_module = load_app("exits", DBUS_SYSTEM_BUS_ADDRESS="unix:path=%s" % missing)
    res = app_module.app.test_client().get("/api/devices")
    body = res.get_json()
    assert res.status_code == 503
    assert body["devices"] == []
    assert missing in body["error"]
    assert "mount" in body["error"]


def test_bluetoothctl_crash_with_a_healthy_bus_is_reported_plainly(monkeypatch):
    """bluetoothctl 5.82 dumps core on a NULL bus; if the bus is fine, say so."""
    app_module = load_app("exits")
    monkeypatch.setattr(app_module.btctl.__class__.__module__ and
                        sys.modules["btctl"], "diagnose_dbus", lambda: None)
    body = app_module.app.test_client().get("/api/devices").get_json()
    assert "exited immediately" in body["error"]


def test_no_controller_returns_503_not_a_traceback():
    client = load_app("nocontroller").app.test_client()
    res = client.get("/api/devices")
    body = res.get_json()
    assert res.status_code == 503
    assert body["devices"] == []
    assert "no Bluetooth controller available" in body["error"]
    assert "Traceback" not in body["error"]

    res = client.post("/api/connect/AA:BB:CC:DD:EE:01")
    assert res.status_code == 503
    assert "controller" in res.get_json()["error"]


def test_bluetoothd_never_answers(monkeypatch):
    app_module = load_app("hangs", start_timeout=3.0)
    # A healthy bus, so the diagnosis must fall through to "bluetoothd is not
    # answering" rather than blaming the socket.
    monkeypatch.setattr(sys.modules["btctl"], "diagnose_dbus", lambda: None)
    res = app_module.app.test_client().get("/api/devices")
    body = res.get_json()
    assert res.status_code == 503
    assert "bluetoothd" in body["error"]


# ---- D-Bus preflight --------------------------------------------------------


def _fake_dbus_send(tmp_path, exit_code, stderr):
    """Put a stub `dbus-send` at the front of PATH."""
    binary = tmp_path / "dbus-send"
    binary.write_text(
        "#!/bin/sh\ncat >&2 <<'EOM'\n%s\nEOM\nexit %d\n" % (stderr, exit_code)
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return str(tmp_path)


def _present_socket(tmp_path):
    """A stand-in bus socket: diagnose_dbus() only checks that the path exists.

    A real AF_UNIX socket would be more faithful but macOS caps those paths at
    104 characters, which pytest's tmp_path routinely exceeds.
    """
    path = tmp_path / "system_bus_socket"
    path.touch()
    return str(path)


def test_diagnose_reports_a_missing_socket(tmp_path, monkeypatch):
    import btctl

    missing = str(tmp_path / "absent")
    monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", "unix:path=%s" % missing)
    message = btctl.diagnose_dbus()
    assert missing in message
    assert "mount" in message


def test_diagnose_names_apparmor_rather_than_the_mount(tmp_path, monkeypatch):
    """The Ubuntu 24.04 failure: socket mounted fine, host policy denies Hello."""
    import btctl

    path = _present_socket(tmp_path)
    monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", "unix:path=%s" % path)
    monkeypatch.setenv(
        "PATH",
        _fake_dbus_send(
            tmp_path,
            1,
            'Failed to open connection to "system" message bus: An AppArmor '
            'policy prevents this sender from sending this message to this '
            'recipient; member="Hello"',
        )
        + os.pathsep
        + os.environ["PATH"],
    )
    message = btctl.diagnose_dbus()
    assert "AppArmor" in message
    assert "apparmor=unconfined" in message
    assert "mounted correctly" in message


def test_diagnose_stays_quiet_when_the_bus_is_healthy(tmp_path, monkeypatch):
    import btctl

    path = _present_socket(tmp_path)
    monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", "unix:path=%s" % path)
    monkeypatch.setenv(
        "PATH", _fake_dbus_send(tmp_path, 0, "") + os.pathsep + os.environ["PATH"]
    )
    assert btctl.diagnose_dbus() is None


def test_old_bluez_still_lists_devices_but_warns_once():
    body = load_app("oldbluez").app.test_client().get("/api/devices").get_json()
    # Unfiltered `devices` still works, so the table is populated ...
    assert len(body["devices"]) == 3
    # ... but every badge goes blank, and that is called out exactly once
    # rather than once per filtered call.
    assert all(not d["paired"] and not d["connected"] and not d["trusted"] for d in body["devices"])
    assert len(body["warnings"]) == 1
    assert "5.65" in body["warnings"][0]


# ---- auth -------------------------------------------------------------------


def test_no_password_means_no_auth(client):
    assert client.get("/api/devices").status_code == 200


def test_admin_password_gates_every_route():
    client = load_app("ok", ADMIN_PASSWORD="s3cret").app.test_client()
    assert client.get("/").status_code == 401
    assert client.get("/api/devices").status_code == 401
    assert client.post("/api/scan", json={"duration": 1}).status_code == 401
    assert "Basic" in client.get("/api/devices").headers["WWW-Authenticate"]

    assert client.get("/api/devices", auth=("admin", "wrong")).status_code == 401
    assert client.get("/api/devices", auth=("root", "s3cret")).status_code == 401
    assert client.get("/api/devices", auth=("admin", "s3cret")).status_code == 200


def test_admin_user_is_configurable():
    client = load_app("ok", ADMIN_PASSWORD="pw", ADMIN_USER="alice").app.test_client()
    assert client.get("/api/devices", auth=("admin", "pw")).status_code == 401
    assert client.get("/api/devices", auth=("alice", "pw")).status_code == 200


# ---- concurrency ------------------------------------------------------------


def test_polling_is_not_blocked_by_a_running_scan(client):
    client.get("/api/devices")  # prime the cache

    done = {}

    def scan():
        done["status"] = client.post("/api/scan", json={"duration": 6}).status_code

    thread = threading.Thread(target=scan)
    thread.start()
    time.sleep(1.0)  # let the scan take the REPL lock

    started = time.time()
    res = client.get("/api/devices")
    elapsed = time.time() - started
    body = res.get_json()

    assert res.status_code == 200
    assert elapsed < 4.0, "poll waited %.1fs behind the scan" % elapsed
    assert body["busy"] is True
    assert body["stale"] is True
    assert len(body["devices"]) == 3  # served from cache

    thread.join()
    assert done["status"] == 200


# ---- adapters (controllers) -------------------------------------------------


def test_adapters_are_listed_with_the_selected_one_marked(client):
    body = client.get("/api/adapters").get_json()
    by_mac = {a["mac"]: a for a in body["adapters"]}
    assert set(by_mac) == {"C8:8A:D8:05:65:0B", "00:1A:7D:DA:71:13"}
    assert by_mac["C8:8A:D8:05:65:0B"]["selected"] is True
    assert by_mac["00:1A:7D:DA:71:13"]["selected"] is False
    assert by_mac["C8:8A:D8:05:65:0B"]["powered"] is True
    assert by_mac["00:1A:7D:DA:71:13"]["powered"] is False


def test_selecting_an_adapter_switches_and_powers_it(client):
    body = client.post("/api/adapter/00:1A:7D:DA:71:13").get_json()
    assert body["ok"] is True
    by_mac = {a["mac"]: a for a in body["adapters"]}
    assert by_mac["00:1A:7D:DA:71:13"]["selected"] is True
    assert by_mac["C8:8A:D8:05:65:0B"]["selected"] is False
    # select_adapter() powers the newly chosen controller on.
    assert client.get("/api/adapters").get_json()["adapters"]

    powered = {a["mac"]: a["powered"] for a in
               client.get("/api/adapters").get_json()["adapters"]}
    assert powered["00:1A:7D:DA:71:13"] is True


def test_selecting_an_unknown_adapter_fails_cleanly(client):
    res = client.post("/api/adapter/11:22:33:44:55:66")
    assert res.status_code == 502
    assert "11:22:33:44:55:66" in res.get_json()["error"]


@pytest.mark.parametrize("payload", ["not-a-mac", "C8:8A:D8:05:65:0B%0Apower%20off"])
def test_adapter_route_validates_the_mac(client, payload):
    assert client.post("/api/adapter/" + payload).status_code == 400


def test_adapter_hardware_reads_sysfs(tmp_path, monkeypatch):
    """The hciN / bus label comes from the host's /sys, shared with the container.

    The MAC -> hciN mapping comes from BlueZ's object manager, because current
    kernels no longer publish /sys/class/bluetooth/hciN/address.
    """
    import btctl

    root = tmp_path / "bluetooth"
    (root / "hci0").mkdir(parents=True)
    (root / "hci0" / "address").write_text("C8:8A:D8:05:65:0B\n")

    # The bus is read as basename(realpath(device/subsystem)), mirroring
    # /sys/bus/usb on a real host.
    (tmp_path / "bus" / "usb").mkdir(parents=True)
    usb = tmp_path / "usbdev"
    usb.mkdir()
    (usb / "subsystem").symlink_to(tmp_path / "bus" / "usb")
    (usb / "product").write_text("Wireless-AC 9260 Bluetooth\n")
    (usb / "manufacturer").write_text("Intel Corp.\n")
    (root / "hci0" / "device").symlink_to(usb)
    # A connection node, which must be ignored.
    (root / "hci0:7").mkdir()

    monkeypatch.setattr(btctl, "SYS_BLUETOOTH", str(root))
    monkeypatch.setattr(btctl, "adapter_paths", lambda: {"C8:8A:D8:05:65:0B": "hci0"})
    hardware = btctl.adapter_hardware()
    assert list(hardware) == ["C8:8A:D8:05:65:0B"]
    entry = hardware["C8:8A:D8:05:65:0B"]
    assert entry["hci"] == "hci0"
    assert entry["bus"] == "USB"
    assert "Intel Corp." in entry["product"]


def test_adapter_hardware_survives_a_missing_sysfs(monkeypatch):
    import btctl

    monkeypatch.setattr(btctl, "SYS_BLUETOOTH", "/nonexistent/bluetooth")
    monkeypatch.setattr(btctl, "adapter_paths", dict)
    assert btctl.adapter_hardware() == {}
    assert btctl.adapter_hardware(["C8:8A:D8:05:65:0B"]) == {}


def test_adapter_paths_parses_the_object_manager(monkeypatch):
    """One adapter object plus a device under it; only the adapter counts."""
    import btctl

    reply = (
        'dict entry( object path "/org/bluez/hci0" array [ dict entry( '
        'string "org.bluez.Adapter1" array [ dict entry( string "Address" '
        'variant string "C8:8A:D8:05:65:0B" ) ] ) ] ) '
        'dict entry( object path "/org/bluez/hci0/dev_00_02_5B_00_FF_04" array [ '
        'dict entry( string "org.bluez.Device1" array [ dict entry( '
        'string "Address" variant string "00:02:5B:00:FF:04" ) ] ) ] )'
    )
    monkeypatch.setattr(btctl, "_dbus_send", lambda *a, **k: (True, reply))
    assert btctl.adapter_paths() == {"C8:8A:D8:05:65:0B": "hci0"}


def test_adapter_paths_is_empty_when_the_bus_is_unreachable(monkeypatch):
    import btctl

    monkeypatch.setattr(btctl, "_dbus_send", lambda *a, **k: (False, "no bus"))
    assert btctl.adapter_paths() == {}


def test_single_adapter_falls_back_to_the_only_hci(tmp_path, monkeypatch):
    """No object manager, one controller, one hciN -- the pairing is unambiguous."""
    import btctl

    root = tmp_path / "bluetooth"
    (root / "hci0" / "device").mkdir(parents=True)
    (tmp_path / "bus" / "usb").mkdir(parents=True)
    (root / "hci0" / "device" / "subsystem").symlink_to(tmp_path / "bus" / "usb")
    (root / "hci0:7").mkdir()

    monkeypatch.setattr(btctl, "SYS_BLUETOOTH", str(root))
    monkeypatch.setattr(btctl, "adapter_paths", dict)
    hardware = btctl.adapter_hardware(["C8:8A:D8:05:65:0B"])
    assert hardware["C8:8A:D8:05:65:0B"]["hci"] == "hci0"
    assert hardware["C8:8A:D8:05:65:0B"]["bus"] == "USB"


# ---- reconnect / re-pair ----------------------------------------------------


def test_reconnect_cycles_the_link(client):
    body = client.post("/api/reconnect/AA:BB:CC:DD:EE:01").get_json()
    assert body["ok"] is True
    assert [s["step"] for s in body["steps"]] == ["disconnect", "connect"]
    device = next(d for d in body["devices"] if d["mac"] == "AA:BB:CC:DD:EE:01")
    assert device["connected"] is True


def test_reconnect_tolerates_a_device_that_was_not_connected(client):
    client.post("/api/disconnect/AA:BB:CC:DD:EE:01")
    body = client.post("/api/reconnect/AA:BB:CC:DD:EE:01").get_json()
    assert body["ok"] is True
    assert next(d for d in body["devices"]
                if d["mac"] == "AA:BB:CC:DD:EE:01")["connected"] is True


def test_repair_forgets_then_pairs_again(client):
    body = client.post("/api/repair/AA:BB:CC:DD:EE:02").get_json()
    assert body["ok"] is True
    # `remove` deletes BlueZ's device object, so re-pairing has to rediscover
    # the device before it can pair with it again.
    assert [s["step"] for s in body["steps"]] == [
        "forget", "rediscover", "pair", "trust", "connect"]
    device = next(d for d in body["devices"] if d["mac"] == "AA:BB:CC:DD:EE:02")
    assert (device["paired"], device["trusted"], device["connected"]) == (True, True, True)


def test_repair_on_a_device_out_of_range_says_so_and_stops(monkeypatch):
    """The footgun case: forgotten, then never seen again."""
    app_module = load_app("ok", MOCK_OUT_OF_RANGE="AA:BB:CC:DD:EE:01")
    monkeypatch.setattr(sys.modules["btctl"], "REDISCOVER_TIMEOUT", 2.0)
    client = app_module.app.test_client()
    res = client.post("/api/repair/AA:BB:CC:DD:EE:01")
    body = res.get_json()
    assert res.status_code == 502
    assert body["ok"] is False
    steps = {s["step"]: s for s in body["steps"]}
    # It must report that the forget already happened -- that is the part the
    # user needs to know about.
    assert steps["forget"]["ok"] is True
    assert steps["rediscover"]["ok"] is False
    assert "pairing mode" in body["error"]
    assert "pair" not in steps


@pytest.mark.parametrize("route", ["reconnect", "repair"])
def test_compound_routes_validate_the_mac(client, route):
    assert client.post("/api/%s/AA:BB:CC:DD:EE:01%%0Ascan%%20on" % route).status_code == 400


# ---- config -----------------------------------------------------------------


def test_config_exposes_the_poll_interval(client):
    body = client.get("/api/config").get_json()
    assert body["poll_seconds"] > 0
    assert body["auth"] is False


# ---- prompt shapes ----------------------------------------------------------


@pytest.mark.parametrize("prompt", ["modern", "legacy"])
def test_both_bluez_prompt_styles_are_understood(prompt):
    """BlueZ 5.8x uses "[DX5]> ", 5.7x used "[bluetooth]# ". Both must work.

    Getting this wrong hangs the panel on modern distros while a one-shot
    `bluetoothctl show` still succeeds -- a genuinely confusing failure, and one
    a mock that only ever emitted "#" happily hid.
    """
    app_module = load_app("ok", MOCK_PROMPT=prompt)
    body = app_module.app.test_client().get("/api/devices").get_json()
    assert len(body["devices"]) == 3
    assert body.get("error") is None


def test_unnamed_devices_are_flagged(client):
    """BlueZ names a nameless device after its address; the UI hides those."""
    import btctl

    assert btctl._has_name("JBL Flip 6", "AA:BB:CC:DD:EE:02") is True
    assert btctl._has_name("48-B4-23-F5-28-85", "48:B4:23:F5:28:85") is False
    assert btctl._has_name("48:B4:23:F5:28:85", "48:B4:23:F5:28:85") is False
    assert all(d["named"] for d in client.get("/api/devices").get_json()["devices"])


# ---- the static page --------------------------------------------------------


def test_index_html_is_well_formed():
    """An unterminated <script> is silently discarded by the browser.

    Dropping the closing tag once produced a page that loaded, rendered its
    chrome, and then simply never called the API -- no console error, nothing.
    Cheap to assert, miserable to debug.
    """
    page = open(os.path.join(ROOT, "static", "index.html")).read()
    assert page.count("<script>") == page.count("</script>") == 1
    assert page.count("<style>") == page.count("</style>") == 1
    # Every element the script reaches for by id must exist in the markup.
    for element_id in (
        "notes", "status", "live", "scan", "duration", "adapter",
        "pairedRows", "pairedEmpty", "pairedCount", "fBtTemplate", "playerSettings",
        "pageTitle", "themeToggle", "snapwebLink",
        "fSnapHost", "fSnapPort", "fSnapControl", "fSnapWeb",
        "dialogDelete", "addPlayer", "snapwebLink",
        "foundRows", "foundEmpty", "foundCount", "showUnnamed",
    ):
        assert 'id="%s"' % element_id in page, element_id


def test_index_html_only_calls_routes_that_exist():
    """Catch a typo'd endpoint in the frontend before a human does."""
    page = open(os.path.join(ROOT, "static", "index.html")).read()
    referenced = set(re.findall(r'"/api/([a-z]+)', page))
    referenced |= set(re.findall(r'`/api/\$\{[^}]+\}/', page)) and set()
    import app as app_module

    served = {
        str(rule).split("/")[2]
        for rule in app_module.app.url_map.iter_rules()
        if str(rule).startswith("/api/")
    }
    assert referenced <= served, referenced - served


# ---- player routes ----------------------------------------------------------


@pytest.fixture
def player_client(tmp_path, monkeypatch):
    """App wired to a fake snapclient and an isolated players.json."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    app_module = load_app("ok")
    players_mod = sys.modules["players"]
    monkeypatch.setattr(
        players_mod, "SNAPCLIENT", os.path.join(ROOT, "tests", "fake_snapclient.py")
    )
    monkeypatch.setattr(players_mod, "list_sinks", list)
    monkeypatch.setattr(players_mod, "sink_present", lambda node: True)
    monkeypatch.setattr(players_mod, "set_sink_volume", lambda node, vol: None)
    app_module.supervisor.config_path = str(tmp_path / "players.json")
    yield app_module.app.test_client()
    app_module.supervisor.stop_all()


def test_players_start_empty(player_client):
    assert player_client.get("/api/players").get_json()["players"] == []


def test_create_player_from_a_paired_device(player_client):
    res = player_client.post("/api/players", json={
        "name": "BoomBox", "mac": "5C:01:3B:63:E7:BA",
        "server": "192.168.111.50", "autostart": False,
    })
    assert res.status_code == 201
    player = res.get_json()["player"]
    assert player["node"] == "bluez_output.5C_01_3B_63_E7_BA.1"
    assert player["state"] == "stopped"


def test_devices_carry_the_node_they_would_expose(player_client):
    devices = player_client.get("/api/devices").get_json()["devices"]
    target = next(d for d in devices if d["mac"] == "AA:BB:CC:DD:EE:01")
    assert target["node"] == "bluez_output.AA_BB_CC_DD_EE_01.1"


def test_player_lifecycle_over_http(player_client):
    created = player_client.post("/api/players", json={
        "name": "Kitchen", "mac": "5C:01:3B:63:E7:BA", "autostart": False,
    }).get_json()["player"]
    pid = created["id"]

    assert player_client.post("/api/players/%s/start" % pid).status_code == 200
    deadline = time.time() + 8
    while time.time() < deadline:
        state = player_client.get("/api/players").get_json()["players"][0]["state"]
        if state == "running":
            break
        time.sleep(0.1)
    assert state == "running"

    logs = player_client.get("/api/players/%s/logs" % pid).get_json()["logs"]
    assert any("--hostID Kitchen" in line for line in logs)

    assert player_client.post("/api/players/%s/stop" % pid).status_code == 200
    assert player_client.get("/api/players").get_json()["players"][0]["state"] == "stopped"

    assert player_client.delete("/api/players/%s" % pid).status_code == 200
    assert player_client.get("/api/players").get_json()["players"] == []


def test_invalid_player_is_rejected_with_a_message(player_client):
    res = player_client.post("/api/players", json={"name": "", "server": "x"})
    assert res.status_code == 400
    assert "name" in res.get_json()["error"]


def test_unknown_player_and_action(player_client):
    assert player_client.post("/api/players/nope/start").status_code == 400
    created = player_client.post("/api/players", json={
        "name": "X", "mac": "5C:01:3B:63:E7:BA", "autostart": False,
    }).get_json()["player"]
    assert player_client.post(
        "/api/players/%s/explode" % created["id"]).status_code == 404


def test_patch_updates_a_player(player_client):
    created = player_client.post("/api/players", json={
        "name": "Old", "mac": "5C:01:3B:63:E7:BA", "autostart": False,
    }).get_json()["player"]
    res = player_client.patch("/api/players/%s" % created["id"],
                              json={"name": "New", "latency_ms": -180})
    assert res.status_code == 200
    player = res.get_json()["player"]
    assert player["name"] == "New"
    assert player["latency_ms"] == -180


def test_sinks_endpoint_is_available(player_client):
    assert player_client.get("/api/sinks").get_json() == {"sinks": []}


# ---- packaging --------------------------------------------------------------


def test_dockerfile_copies_every_module_the_app_imports():
    """A module missing from COPY crashes the image on boot, not in CI.

    snapctl.py shipped exactly that way once: every test passed, the container
    died with ModuleNotFoundError the moment it started.
    """
    dockerfile = open(os.path.join(ROOT, "Dockerfile")).read()
    copied = set()
    for line in dockerfile.splitlines():
        if line.startswith("COPY") and "/app/" in line:
            copied.update(w for w in line.split() if w.endswith(".py"))

    modules = {
        os.path.basename(p)
        for p in os.listdir(ROOT)
        if p.endswith(".py") and not p.startswith("test_")
    }
    assert modules <= copied, "not copied into the image: %s" % (modules - copied)


def test_every_copied_module_actually_imports():
    import importlib
    import py_compile

    for name in ("app", "btctl", "players", "snapctl"):
        assert importlib.import_module(name)
    # healthcheck.py is a script: importing it would run the probe and exit.
    py_compile.compile(os.path.join(ROOT, "healthcheck.py"), doraise=True)


# ---- snapserver defaults from the environment -------------------------------


def test_snapserver_defaults_come_from_env(monkeypatch):
    app_module = load_app(
        "ok", SNAPSERVER_HOST="10.0.0.9", SNAPSERVER_PORT="1799",
        SNAPSERVER_CONTROL_PORT="1800",
    )
    cfg = app_module.app.test_client().get("/api/config").get_json()
    assert cfg["snapserver"]["host"] == "10.0.0.9"
    assert cfg["snapserver"]["port"] == 1799
    assert cfg["snapserver"]["control_port"] == 1800

    players_mod = sys.modules["players"]
    assert players_mod.DEFAULTS["server"] == "10.0.0.9"
    assert players_mod.DEFAULTS["port"] == 1799
    assert players_mod.DEFAULTS["control_port"] == 1800


def test_a_player_created_with_no_server_uses_those_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    app_module = load_app(
        "ok", SNAPSERVER_HOST="10.0.0.9", SNAPSERVER_CONTROL_PORT="1800",
        CONFIG_DIR=str(tmp_path),
    )
    players_mod = sys.modules["players"]
    monkeypatch.setattr(players_mod, "list_sinks", list)
    app_module.supervisor.config_path = str(tmp_path / "p.json")

    created = app_module.app.test_client().post("/api/players", json={
        "name": "Defaults", "mac": "5C:01:3B:63:E7:BA", "autostart": False,
    }).get_json()["player"]
    assert created["server"] == "10.0.0.9"
    assert created["control_port"] == 1800
    app_module.supervisor.stop_all()


def test_garbage_env_falls_back_instead_of_crashing():
    app_module = load_app("ok", SNAPSERVER_PORT="not-a-number")
    assert app_module.app.test_client().get("/api/config").get_json()["snapserver"]["port"] == 1704


def test_snapweb_port_is_configurable():
    app_module = load_app("ok", SNAPSERVER_WEB_PORT="1999")
    cfg = app_module.app.test_client().get("/api/config").get_json()
    assert cfg["snapserver"]["web_port"] == 1999


def test_snapweb_port_defaults_to_1780():
    app_module = load_app("ok")
    cfg = app_module.app.test_client().get("/api/config").get_json()
    assert cfg["snapserver"]["web_port"] == 1780


def test_settings_round_trip_over_http(player_client):
    body = player_client.get("/api/settings").get_json()
    assert body["settings"]["bt_name_template"] == "{name} (BT)"

    res = player_client.patch("/api/settings", json={"bt_name_template": "{name} ~BT"})
    assert res.status_code == 200
    assert res.get_json()["settings"]["bt_name_template"] == "{name} ~BT"
    assert player_client.get("/api/settings").get_json()["settings"][
        "bt_name_template"] == "{name} ~BT"


def test_bad_settings_over_http_are_400(player_client):
    res = player_client.patch("/api/settings", json={"bt_name_template": "nope"})
    assert res.status_code == 400
    assert "{name}" in res.get_json()["error"]


# ---- Home Assistant Ingress compatibility -----------------------------------


def test_frontend_resolves_api_urls_relative_to_the_document():
    """Ingress serves the page under /api/hassio_ingress/<token>/.

    Home Assistant strips that prefix before proxying, so the browser has to
    send requests relative to the document. An absolute "/api/devices" would
    escape the prefix and hit Home Assistant itself, which is why the page is
    routed through api() rather than fetching literal absolute paths.
    """
    page = open(os.path.join(ROOT, "static", "index.html")).read()
    assert 'const api = (path) =>' in page
    assert 'url = api(url);' in page
    # No fetch() should bypass the helper with a root-absolute path.
    assert 'fetch("/api' not in page
    assert "fetch('/api" not in page
    assert "fetch(`/api" not in page


def test_a_stopped_player_shows_no_transport_or_track():
    """A stopped player has no snapclient, so the row must not imply playback.

    The snapserver still remembers the client and the stream it last used, so
    without an explicit reset the row kept showing "paused · <track>" and live
    transport buttons for a player that was plainly stopped.
    """
    page = open(os.path.join(ROOT, "static", "index.html")).read()
    assert "if (!p.running) {" in page
    assert "player stopped" in page
    assert 'not connected to the snapserver' in page
    # The reset has to come before the transport controls are built.
    assert page.index("if (!p.running) {") < page.index('s.can_control')


# ---- sound check: test tone and codec ---------------------------------------


def test_the_test_tone_route_reaches_the_sink(player_client, monkeypatch):
    players_mod = sys.modules["players"]
    played = []
    monkeypatch.setattr(
        players_mod, "play_test_tone",
        lambda node, channel: played.append((node, channel))
        or {"node": node, "channel": channel, "seconds": 1.2, "muted": False},
    )
    res = player_client.post("/api/devices/AA:BB:CC:DD:EE:01/test/left")
    assert res.status_code == 200
    assert res.get_json()["test"]["channel"] == "left"
    assert played == [("bluez_output.AA_BB_CC_DD_EE_01.1", "left")]


def test_the_test_tone_route_validates_the_mac(player_client):
    res = player_client.post("/api/devices/not-a-mac/test/left")
    assert res.status_code == 400
    assert "invalid MAC" in res.get_json()["error"]


def test_a_tone_on_an_absent_sink_is_a_clean_error(player_client):
    """list_sinks is empty in this fixture, so nothing is present."""
    res = player_client.post("/api/devices/AA:BB:CC:DD:EE:01/test/both")
    assert res.status_code == 400
    assert "is not present" in res.get_json()["error"]


def test_a_player_can_be_tested_by_id(player_client, monkeypatch):
    players_mod = sys.modules["players"]
    monkeypatch.setattr(
        players_mod, "play_test_tone",
        lambda node, channel: {"node": node, "channel": channel,
                               "seconds": 1.2, "muted": None},
    )
    player = player_client.post("/api/players", json={
        "name": "Kitchen", "mac": "5C:01:3B:63:E7:BA", "autostart": False,
    }).get_json()["player"]

    res = player_client.post("/api/players/%s/test/right" % player["id"])
    assert res.status_code == 200
    assert res.get_json()["test"]["node"] == "bluez_output.5C_01_3B_63_E7_BA.1"


def test_start_is_still_an_action_not_a_channel(player_client):
    """/api/players/<id>/test/<channel> must not shadow the action route."""
    player = player_client.post("/api/players", json={
        "name": "Routing", "mac": "5C:01:3B:63:E7:BA", "autostart": False,
    }).get_json()["player"]
    assert player_client.post("/api/players/%s/stop" % player["id"]).status_code == 200
    assert player_client.post("/api/players/%s/nonsense" % player["id"]).status_code == 404


def test_codec_status_over_http(player_client, monkeypatch):
    players_mod = sys.modules["players"]
    monkeypatch.setattr(players_mod, "codec_status", lambda mac: {
        "available": True, "active": "ldac", "headset": False,
        "profiles": [{"index": 11, "name": "a2dp-sink", "codec": "LDAC",
                      "current": True}],
    })
    body = player_client.get("/api/devices/AA:BB:CC:DD:EE:01/codec").get_json()
    assert body["codec"]["active"] == "ldac"
    assert body["codec"]["profiles"][0]["codec"] == "LDAC"


def test_switching_codec_needs_a_profile_number(player_client):
    res = player_client.post("/api/devices/AA:BB:CC:DD:EE:01/codec", json={})
    assert res.status_code == 400
    assert "index" in res.get_json()["error"]


def test_switching_codec_reports_what_it_did(player_client, monkeypatch):
    app_module = sys.modules["app"]
    calls = []
    monkeypatch.setattr(
        app_module.supervisor, "switch_codec",
        lambda mac, index: calls.append((mac, index)) or {"active": "sbc_xq"},
    )
    res = player_client.post("/api/devices/AA:BB:CC:DD:EE:01/codec",
                             json={"index": 6})
    assert res.status_code == 200
    assert res.get_json()["codec"]["active"] == "sbc_xq"
    assert calls == [("AA:BB:CC:DD:EE:01", 6)]


# ---- pairing tells the truth -------------------------------------------------


def test_a_refused_pairing_is_reported_as_a_failure():
    """The verdict arrives after the prompt, so it used to be missed entirely.

    bluetoothctl answers `pair` with "Attempting to pair with ..." and reprints
    its prompt; "Failed to pair: org.bluez.Error.AuthenticationFailed" lands
    seconds later. Reading only up to the prompt reported pair ok / trust ok /
    connect ok while BlueZ still said `Paired: no` -- seen on real hardware.
    """
    app_module = load_app("pairfails")
    try:
        client = app_module.app.test_client()
        res = client.post("/api/pair/AA:BB:CC:DD:EE:03")
        body = res.get_json()
        assert res.status_code >= 400
        assert body["ok"] is False
        assert "AuthenticationFailed" in body["error"]
        assert "pairing mode" in body["error"]
        assert body["steps"][0]["step"] == "pair"
        assert body["steps"][0]["ok"] is False
        listed = client.get("/api/devices").get_json()["devices"]
        target = next(d for d in listed if d["mac"] == "AA:BB:CC:DD:EE:03")
        assert target["paired"] is False, "a refused bond must not look paired"
    finally:
        app_module.btctl.close()


def test_a_successful_pairing_still_reports_success(client):
    body = client.post("/api/pair/AA:BB:CC:DD:EE:03").get_json()
    assert body["ok"] is True
    assert [s["step"] for s in body["steps"]] == ["pair", "trust", "connect"]
    target = next(d for d in body["devices"] if d["mac"] == "AA:BB:CC:DD:EE:03")
    assert target["paired"] is True


# ---- recovering a wedged controller -----------------------------------------


@pytest.fixture
def quick_reset(monkeypatch):
    """Shorten the reset's own waits; the behaviour under test is the sequence."""
    btctl_mod = sys.modules["btctl"]
    monkeypatch.setattr(btctl_mod, "POWER_SETTLE", 0.05)
    monkeypatch.setattr(btctl_mod, "PROBE_SECONDS", 0.4)
    return btctl_mod


def test_a_scan_that_finds_nothing_warns_about_the_radio():
    """The symptom of a stuck controller is silence, which looks like calm."""
    app_module = load_app("stuckradio")
    try:
        sys.modules["btctl"].STUCK_SCAN_SECONDS = 0.1
        client = app_module.app.test_client()
        body = client.post("/api/scan", json={"duration": 0.2}).get_json()
        assert body["ok"] is True
        assert any("stuck" in w for w in body["warnings"]), body["warnings"]
    finally:
        app_module.btctl.close()


def test_reset_recovers_a_wedged_radio(quick_reset):
    """power off, power on -- retried, because the first attempts fail -- then
    a discovery probe, because "powered on" is not the same as "working"."""
    app_module = load_app("stuckradio")
    try:
        sys.modules["btctl"].POWER_SETTLE = 0.05
        sys.modules["btctl"].PROBE_SECONDS = 0.4
        client = app_module.app.test_client()
        body = client.post("/api/adapters/reset").get_json()
        assert body["ok"] is True, body
        steps = {s["step"]: s for s in body["steps"]}
        assert steps["power on"]["ok"] is True
        assert "attempts" in steps["power on"]["output"]
        assert steps["discovery probe"]["ok"] is True
        assert "seen in" in steps["discovery probe"]["output"]
    finally:
        app_module.btctl.close()


def test_a_scan_that_finds_something_does_not_warn(client):
    """The hint must not nag on a healthy radio."""
    sys.modules["btctl"].STUCK_SCAN_SECONDS = 0.1
    body = client.post("/api/scan", json={"duration": 1}).get_json()
    assert body["ok"] is True
    assert not any("stuck" in w for w in body["warnings"]), body["warnings"]


def test_the_stuck_hint_clears_once_the_radio_hears_again():
    """It is re-evaluated per scan, not latched for the life of the process."""
    app_module = load_app("stuckradio")
    try:
        btctl_mod = sys.modules["btctl"]
        btctl_mod.STUCK_SCAN_SECONDS = 0.1
        client = app_module.app.test_client()
        first = client.post("/api/scan", json={"duration": 0.3}).get_json()
        assert any("stuck" in w for w in first["warnings"]), first["warnings"]

        # Power on is what unwedges the mock, the way it did the real dongle.
        btctl_mod.POWER_SETTLE = 0.05
        btctl_mod.PROBE_SECONDS = 0.4
        client.post("/api/adapters/reset")
        # The reset's own probe already discovered what was in the air, so give
        # the next scan something genuinely new to find: the mock keeps a
        # removed device advertising, exactly as a real one would.
        client.post("/api/remove/AA:BB:CC:DD:EE:04")
        again = client.post("/api/scan", json={"duration": 0.3}).get_json()
        assert not any("stuck" in w for w in again["warnings"]), again["warnings"]
    finally:
        app_module.btctl.close()


def test_reset_says_so_when_the_controller_will_not_come_back(quick_reset):
    app_module = load_app("stuckradio", MOCK_POWER_FAILURES="99")
    try:
        sys.modules["btctl"].POWER_SETTLE = 0.05
        sys.modules["btctl"].PROBE_SECONDS = 0.4
        client = app_module.app.test_client()
        res = client.post("/api/adapters/reset")
        body = res.get_json()
        assert res.status_code >= 400
        assert body["ok"] is False
        assert "would not power back on" in body["error"]
        assert "hciconfig" in body["error"], "the error must name the escape hatch"
    finally:
        app_module.btctl.close()


def test_reset_says_so_when_the_radio_powers_on_but_stays_deaf(quick_reset):
    """The nastier case: BlueZ is happy, and still nothing is ever discovered."""
    app_module = load_app("stuckradio", MOCK_POWER_FAILURES="0", MOCK_STAYS_STUCK="1")
    try:
        sys.modules["btctl"].POWER_SETTLE = 0.05
        sys.modules["btctl"].PROBE_SECONDS = 0.4
        client = app_module.app.test_client()
        body = client.post("/api/adapters/reset").get_json()
        assert body["ok"] is False
        assert "discovered nothing" in body["error"]
        steps = {s["step"]: s for s in body["steps"]}
        assert steps["power on"]["ok"] is True
        assert steps["discovery probe"]["ok"] is False
    finally:
        app_module.btctl.close()


def test_reset_escalates_to_an_hci_reset_when_it_is_allowed(quick_reset, tmp_path):
    """With host networking and CAP_NET_ADMIN the panel can reset the chip itself.

    Measured on a real host: on the default bridge network the HCI socket does
    not exist at all -- bluetooth sockets are namespaced, so no capability helps
    -- and with the host namespace but no CAP_NET_ADMIN the ioctl is EPERM. When
    both are given, this is the rung above asking bluetoothd.
    """
    fake = tmp_path / "hciconfig"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)

    app_module = load_app("stuckradio", MOCK_POWER_FAILURES="99", HCICONFIG=str(fake))
    try:
        btctl_mod = sys.modules["btctl"]
        btctl_mod.POWER_SETTLE = 0.05
        btctl_mod.PROBE_SECONDS = 0.4
        btctl_mod.HCICONFIG = str(fake)
        client = app_module.app.test_client()
        body = client.post("/api/adapters/reset").get_json()
        steps = {s["step"]: s for s in body["steps"]}
        assert "hci reset" in steps, body["steps"]
        assert steps["hci reset"]["ok"] is True
    finally:
        app_module.btctl.close()


def test_reset_stays_quiet_about_an_hci_reset_it_cannot_do(quick_reset, tmp_path):
    """The normal case: no host networking, so the tool refuses. Say what to run."""
    fake = tmp_path / "hciconfig"
    fake.write_text(
        "#!/bin/sh\necho \"Can't open HCI socket.: Address family not supported\" >&2\nexit 1\n")
    fake.chmod(0o755)

    app_module = load_app("stuckradio", MOCK_POWER_FAILURES="99", HCICONFIG=str(fake))
    try:
        btctl_mod = sys.modules["btctl"]
        btctl_mod.POWER_SETTLE = 0.05
        btctl_mod.PROBE_SECONDS = 0.4
        btctl_mod.HCICONFIG = str(fake)
        client = app_module.app.test_client()
        body = client.post("/api/adapters/reset").get_json()
        assert body["ok"] is False
        assert "hci reset" not in {s["step"] for s in body["steps"]}
        assert "CAP_NET_ADMIN" in body["error"]
        assert "hciconfig" in body["error"]
    finally:
        app_module.btctl.close()
