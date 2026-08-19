"""End-to-end tests against a fake bluetoothctl (see mock_bluetoothctl.py).

Nothing here needs a Bluetooth adapter, a D-Bus bus or root, so it runs in CI.
"""
import importlib
import os
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
    for key in ("ADMIN_PASSWORD", "ADMIN_USER", "MOCK_MODE"):
        os.environ.pop(key, None)
    os.environ["MOCK_MODE"] = mode
    os.environ["BLUETOOTHCTL"] = "%s %s" % (sys.executable, MOCK)
    os.environ.update(env)

    for name in ("app", "btctl"):
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
        "paired": True,
        "connected": True,
        "trusted": True,
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
