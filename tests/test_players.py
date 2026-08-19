"""Supervisor tests. No audio hardware, no PipeWire, no snapcast binary."""
import json
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(ROOT, "tests", "fake_snapclient.py")
sys.path.insert(0, ROOT)

import players as players_mod  # noqa: E402
from players import PlayerError, Supervisor, node_for_mac, validate  # noqa: E402


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    monkeypatch.setattr(players_mod, "SNAPCLIENT", FAKE)
    monkeypatch.setattr(players_mod, "RETRY_START", 0.2)
    monkeypatch.setattr(players_mod, "RETRY_MAX", 0.4)
    monkeypatch.setattr(players_mod, "NODE_WAIT_SECONDS", 1.0)
    # No PipeWire in the test environment: report the sink as present so the
    # readiness gate does not block, and skip volume calls.
    monkeypatch.setattr(players_mod, "list_sinks", list)
    monkeypatch.setattr(players_mod, "sink_present", lambda node: True)
    monkeypatch.setattr(players_mod, "set_sink_volume", lambda node, vol: None)

    sup = Supervisor(config_path=str(tmp_path / "players.json"))
    yield sup
    sup.stop_all()


def make(sup, **over):
    config = {
        "name": "BoomBox",
        "mac": "5C:01:3B:63:E7:BA",
        "server": "192.168.111.50",
        "autostart": False,
    }
    config.update(over)
    return sup.create(config)


def wait_for(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


# ---- node derivation --------------------------------------------------------


def test_node_name_is_derived_from_the_mac():
    # Verified against a real host: this DX5 really is bluez_output.00_02_5B_...
    assert node_for_mac("00:02:5B:00:FF:04") == "bluez_output.00_02_5B_00_FF_04.1"
    assert node_for_mac("5c:01:3b:63:e7:ba") == "bluez_output.5C_01_3B_63_E7_BA.1"


def test_creating_from_a_mac_fills_in_the_node(supervisor):
    player = make(supervisor)
    assert player.config["node"] == "bluez_output.5C_01_3B_63_E7_BA.1"


def test_an_explicit_node_wins_over_the_derived_one(supervisor):
    player = make(supervisor, node="alsa_output.usb-Topping_DX3_Pro-00.analog-stereo")
    assert player.config["node"].startswith("alsa_output.")


# ---- validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "bad,message",
    [
        ({"name": ""}, "name"),
        ({"name": "x" * 80}, "name"),
        ({"name": "rm -rf /; echo"}, "name"),
        ({"mac": "nope"}, "MAC"),
        ({"server": "host with spaces"}, "server"),
        ({"port": 0}, "port"),
        ({"port": 99999}, "port"),
        ({"port": "abc"}, "port"),
        ({"latency_ms": 99999}, "latency"),
        ({"volume": 5}, "volume"),
        ({"pipewire_latency": "garbage"}, "PipeWire latency"),
    ],
)
def test_bad_definitions_are_rejected(bad, message):
    config = {"name": "Valid", "server": "127.0.0.1"}
    config.update(bad)
    with pytest.raises(PlayerError) as err:
        validate(config)
    assert message.lower() in str(err.value).lower()


def test_duplicate_names_are_rejected(supervisor):
    make(supervisor)
    with pytest.raises(PlayerError):
        make(supervisor)


def test_instances_are_unique(supervisor):
    a = make(supervisor, name="A")
    b = make(supervisor, name="B")
    assert a.config["instance"] != b.config["instance"]


# ---- launching --------------------------------------------------------------


def test_start_launches_snapclient_with_the_right_arguments(supervisor):
    player = make(supervisor, latency_ms=-150, pipewire_latency="1024/48000")
    player.start()
    assert wait_for(lambda: player.state == "running"), player.state
    assert wait_for(lambda: any("snapclient args" in l for l in player.logs))

    logs = "\n".join(player.logs)
    assert "--hostID BoomBox" in logs
    assert "--player alsa -s default" in logs        # USE_ALSA path
    assert "--latency -150" in logs                  # A2DP compensation
    assert "tcp://192.168.111.50:1704" in logs
    assert "PIPEWIRE_NODE=bluez_output.5C_01_3B_63_E7_BA.1" in logs
    assert "PIPEWIRE_LATENCY=1024/48000" in logs

    player.stop()
    assert player.state == "stopped"


def test_extra_arguments_are_appended(supervisor):
    player = make(supervisor, extra="--sampleformat 48000:16:2")
    player.start()
    assert wait_for(lambda: any("snapclient args" in l for l in player.logs))
    assert "--sampleformat 48000:16:2" in "\n".join(player.logs)
    player.stop()


def test_native_pipewire_mode(supervisor):
    player = make(supervisor, use_alsa=False)
    player.start()
    assert wait_for(lambda: any("snapclient args" in l for l in player.logs))
    logs = "\n".join(player.logs)
    assert "--player pipewire" in logs
    assert "-s default" not in logs
    player.stop()


def test_multiple_players_run_concurrently(supervisor):
    """The whole point of supervising in-process: one node each, same container."""
    a = make(supervisor, name="JBL", mac="F8:5C:7D:75:D6:06")
    b = make(supervisor, name="RadioTech", mac="20:18:12:00:07:C4")
    a.start()
    b.start()
    assert wait_for(lambda: a.state == "running" and b.state == "running")

    assert "PIPEWIRE_NODE=bluez_output.F8_5C_7D_75_D6_06.1" in "\n".join(a.logs)
    assert "PIPEWIRE_NODE=bluez_output.20_18_12_00_07_C4.1" in "\n".join(b.logs)
    assert a._proc.pid != b._proc.pid

    a.stop()
    assert a.state == "stopped"
    assert b.state == "running"   # stopping one must not disturb the other
    b.stop()


def test_a_crashing_player_is_restarted(supervisor, monkeypatch):
    monkeypatch.setenv("FAKE_SNAPCLIENT_MODE", "crash")
    player = make(supervisor)
    player.start()
    assert wait_for(lambda: player.restarts >= 2, timeout=10), player.restarts
    assert player.last_exit == 3
    player.stop()
    assert player.state == "stopped"


def test_stop_interrupts_the_backoff_promptly(supervisor, monkeypatch):
    """A stop must not wait out the retry delay."""
    monkeypatch.setenv("FAKE_SNAPCLIENT_MODE", "crash")
    monkeypatch.setattr(players_mod, "RETRY_START", 30.0)
    monkeypatch.setattr(players_mod, "RETRY_MAX", 30.0)
    player = make(supervisor)
    player.start()
    assert wait_for(lambda: player.state == "backoff", timeout=10)

    began = time.time()
    player.stop()
    assert time.time() - began < 5.0, "stop waited out the backoff"
    assert player.state == "stopped"


def test_missing_sink_holds_the_player_in_waiting(supervisor, monkeypatch):
    monkeypatch.setattr(players_mod, "sink_present", lambda node: False)
    monkeypatch.setattr(players_mod, "shutil", players_mod.shutil)
    monkeypatch.setattr(players_mod.shutil, "which", lambda name: "/usr/bin/" + name)
    player = make(supervisor)
    player.start()
    assert wait_for(lambda: player.state == "waiting", timeout=10), player.state
    assert "not present" in player.detail
    player.stop()


def test_bluetooth_connect_is_attempted_before_launch(supervisor):
    seen = []
    supervisor.connect_bluetooth = seen.append
    player = make(supervisor)
    player.start()
    assert wait_for(lambda: player.state == "running")
    assert seen == ["5C:01:3B:63:E7:BA"]
    player.stop()


def test_a_failing_bluetooth_connect_does_not_abort_the_player(supervisor):
    def boom(mac):
        raise RuntimeError("device not available")

    supervisor.connect_bluetooth = boom
    player = make(supervisor)
    player.start()
    # The sink may still be there (another route, already connected), so a failed
    # connect is logged and the launch proceeds.
    assert wait_for(lambda: player.state == "running", timeout=10)
    assert any("bluetooth connect failed" in l for l in player.logs)
    player.stop()


# ---- persistence ------------------------------------------------------------


def test_players_survive_a_restart(supervisor, tmp_path, monkeypatch):
    make(supervisor, name="Kitchen")
    saved = json.loads((tmp_path / "players.json").read_text())
    assert [p["name"] for p in saved["players"]] == ["Kitchen"]

    reloaded = Supervisor(config_path=str(tmp_path / "players.json"))
    assert [p["name"] for p in reloaded.list()] == ["Kitchen"]
    assert reloaded.list()[0]["node"] == "bluez_output.5C_01_3B_63_E7_BA.1"


def test_a_corrupt_config_does_not_stop_the_panel_booting(tmp_path):
    path = tmp_path / "players.json"
    path.write_text("{ this is not json")
    assert Supervisor(config_path=str(path)).list() == []


def test_delete_stops_and_forgets(supervisor, tmp_path):
    player = make(supervisor)
    player.start()
    assert wait_for(lambda: player.state == "running")
    supervisor.delete(player.id)
    assert supervisor.list() == []
    assert json.loads((tmp_path / "players.json").read_text())["players"] == []


def test_update_rebinds_a_running_player(supervisor):
    player = make(supervisor)
    player.start()
    assert wait_for(lambda: player.state == "running")

    supervisor.update(player.id, {"mac": "20:18:12:00:07:C4", "node": ""})
    assert wait_for(lambda: player.state == "running", timeout=10)
    assert player.config["node"] == "bluez_output.20_18_12_00_07_C4.1"
    assert "PIPEWIRE_NODE=bluez_output.20_18_12_00_07_C4.1" in "\n".join(player.logs)
    player.stop()


def test_autostart_only_starts_the_flagged_ones(supervisor):
    quiet = make(supervisor, name="Quiet", autostart=False)
    loud = make(supervisor, name="Loud", autostart=True)
    loud.stop()

    supervisor.autostart()
    assert wait_for(lambda: loud.state == "running")
    assert quiet.state == "stopped"


def test_stop_immediately_after_start_leaves_no_orphan(supervisor):
    """stop() racing the launch must not strand a snapclient holding the sink.

    The supervisor thread assigns self._proc a moment after start() returns; a
    stop() that reads it too early used to terminate nothing and leave both the
    child process and its thread running forever.
    """
    player = make(supervisor)
    player.start()
    player.stop()          # no sleep: land inside the launch window on purpose
    assert player.state == "stopped"
    assert player._proc is None
    assert not (player._thread and player._thread.is_alive())

    # And it can still be started again afterwards.
    player.start()
    assert wait_for(lambda: player.state == "running"), player.state
    player.stop()


# ---- the sink watchdog ------------------------------------------------------


def test_player_restarts_when_its_sink_disappears(supervisor, monkeypatch):
    """Switching a Bluetooth speaker off must not leave a green "running" player.

    snapclient keeps running when its output sink vanishes -- confirmed against
    real hardware -- so without the watchdog the player reports healthy forever
    and never reconnects the device.
    """
    monkeypatch.setattr(players_mod, "HEALTH_INTERVAL", 0.2)
    monkeypatch.setattr(players_mod, "SINK_GRACE", 0.6)
    monkeypatch.setattr(players_mod, "NODE_WAIT_SECONDS", 2.0)

    present = {"value": True}
    monkeypatch.setattr(players_mod, "sink_present", lambda node: present["value"])

    reconnects = []
    supervisor.connect_bluetooth = reconnects.append

    player = make(supervisor)
    player.start()
    assert wait_for(lambda: player.state == "running"), player.state
    first_pid = player._proc.pid

    # The speaker goes away.
    present["value"] = False
    assert wait_for(lambda: player.state != "running", timeout=10), player.state
    assert any("has been gone" in l for l in player.logs)

    # ... and comes back.
    present["value"] = True
    assert wait_for(lambda: player.state == "running", timeout=15), player.state
    assert player._proc.pid != first_pid, "should be a fresh snapclient"
    # Recovery goes through the readiness step, which reconnects Bluetooth.
    assert len(reconnects) >= 2
    player.stop()


def test_a_momentary_sink_blip_does_not_restart_the_player(supervisor, monkeypatch):
    """Only a sustained absence counts; sinks flicker during rate switches."""
    monkeypatch.setattr(players_mod, "HEALTH_INTERVAL", 0.1)
    monkeypatch.setattr(players_mod, "SINK_GRACE", 5.0)

    present = {"value": True}
    monkeypatch.setattr(players_mod, "sink_present", lambda node: present["value"])

    player = make(supervisor)
    player.start()
    assert wait_for(lambda: player.state == "running")
    pid = player._proc.pid

    present["value"] = False
    time.sleep(0.5)
    present["value"] = True
    time.sleep(0.5)

    assert player.state == "running"
    assert player._proc.pid == pid, "a brief blip must not restart snapclient"
    assert player.restarts == 0
    player.stop()


def test_a_player_with_no_node_is_not_watchdogged(supervisor, monkeypatch):
    """A player on the default sink has no node to watch; leave it alone."""
    monkeypatch.setattr(players_mod, "HEALTH_INTERVAL", 0.1)
    monkeypatch.setattr(players_mod, "SINK_GRACE", 0.3)
    monkeypatch.setattr(players_mod, "sink_present", lambda node: False)

    player = make(supervisor, name="Default", mac="", node="")
    player.start()
    assert wait_for(lambda: player.state == "running")
    time.sleep(1.0)
    assert player.state == "running"
    assert player.restarts == 0
    player.stop()
