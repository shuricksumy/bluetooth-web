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


def test_names_are_passed_as_one_argument_not_through_a_shell(supervisor):
    """Shell metacharacters in a name are harmless: argv is a list.

    That is why the name pattern only bars control characters -- rejecting
    punctuation would block "BT · Kitchen" and Cyrillic names for no gain.
    """
    player = make(supervisor, name="rm -rf /; echo hi")
    player.start()
    assert wait_for(lambda: player.state == "running")
    argv = player._proc.args
    assert argv[argv.index("--hostID") + 1] == "rm -rf /; echo hi"
    player.stop()


def test_unicode_names_are_accepted(supervisor):
    for name in ("BT · Kitchen", "Кухня", "Küche"):
        cleaned = validate({"name": name, "server": "127.0.0.1"})
        assert cleaned["name"] == name


@pytest.mark.parametrize("bad", ["with\nnewline", "with\x00null", "tab\there"])
def test_control_characters_in_names_are_rejected(bad):
    with pytest.raises(PlayerError):
        validate({"name": bad, "server": "127.0.0.1"})


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

    assert wait_for(
        lambda: "PIPEWIRE_NODE=bluez_output.F8_5C_7D_75_D6_06.1" in "\n".join(a.logs))
    assert wait_for(
        lambda: "PIPEWIRE_NODE=bluez_output.20_18_12_00_07_C4.1" in "\n".join(b.logs))
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
    assert wait_for(
        lambda: "PIPEWIRE_NODE=bluez_output.20_18_12_00_07_C4.1" in "\n".join(player.logs),
        timeout=10,
    )
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


# ---- panel settings ---------------------------------------------------------


def test_bt_name_template_defaults_to_a_marker(supervisor):
    assert supervisor.settings["bt_name_template"] == "{name} (BT)"
    assert supervisor.suggest_name("JBL Charge 5", bluetooth=True) == "JBL Charge 5 (BT)"
    # A wired sink is not marked.
    assert supervisor.suggest_name("Topping DX3", bluetooth=False) == "Topping DX3"


def test_the_marker_is_configurable_not_hardcoded(supervisor):
    supervisor.update_settings({"bt_name_template": "BT · {name}"})
    assert supervisor.suggest_name("JBL", bluetooth=True) == "BT · JBL"

    # ... and can be removed entirely.
    supervisor.update_settings({"bt_name_template": "{name}"})
    assert supervisor.suggest_name("JBL", bluetooth=True) == "JBL"


def test_settings_persist(supervisor, tmp_path):
    supervisor.update_settings({"bt_name_template": "{name} [bt]"})
    reloaded = Supervisor(config_path=str(tmp_path / "players.json"))
    assert reloaded.settings["bt_name_template"] == "{name} [bt]"


@pytest.mark.parametrize("bad,why", [
    ({"bt_name_template": "no placeholder"}, "{name}"),
    ({"bt_name_template": "{name} " + "x" * 100}, "too long"),
    ({"bt_name_template": "{name}\ttab"}, "printable"),
    ({"nonsense": 1}, "unknown setting"),
])
def test_bad_settings_are_rejected(supervisor, bad, why):
    # players_mod, not a fresh `from players import ...`: test_api.py reloads
    # the module, so a re-import here would yield a *different* SettingsError
    # class than the one this supervisor raises, and pytest.raises would miss it.
    with pytest.raises(players_mod.SettingsError) as err:
        supervisor.update_settings(bad)
    assert why.lower() in str(err.value).lower()
    # The rejected value must not have been kept.
    assert supervisor.settings["bt_name_template"] == "{name} (BT)"


def test_markup_in_a_name_is_allowed_and_escaped_on_render(supervisor):
    """"<script>" is not dangerous here: argv is a list and the UI escapes.

    Rejecting it would be security theatre that also blocks legitimate
    punctuation, so it is accepted -- and index.html runs every name through
    esc() before inserting it.
    """
    player = make(supervisor, name="Kitchen <b>")
    assert player.config["name"] == "Kitchen <b>"
    page = open(os.path.join(ROOT, "static", "index.html")).read()
    assert "esc(p.name)" in page


def test_a_corrupt_settings_block_falls_back(tmp_path):
    path = tmp_path / "players.json"
    path.write_text('{"settings": {"bt_name_template": "broken"}, "players": []}')
    assert Supervisor(config_path=str(path)).settings["bt_name_template"] == "{name} (BT)"


def test_snapserver_settings_are_web_editable(supervisor):
    """Env only seeds them; the stored value wins afterwards."""
    assert supervisor.new_player_defaults()["port"] == 1704

    supervisor.update_settings({
        "snapserver_host": "10.0.0.5", "snapserver_port": 1804,
        "snapserver_control_port": 1805, "snapserver_web_port": 1880,
    })
    defaults = supervisor.new_player_defaults()
    assert defaults == {"server": "10.0.0.5", "port": 1804, "control_port": 1805}

    player = supervisor.create({"name": "Inherits", "mac": "5C:01:3B:63:E7:BA",
                                "autostart": False})
    assert player.config["server"] == "10.0.0.5"
    assert player.config["control_port"] == 1805
    # An explicit value still overrides the default.
    other = supervisor.create({"name": "Explicit", "server": "10.0.0.9",
                               "autostart": False})
    assert other.config["server"] == "10.0.0.9"


@pytest.mark.parametrize("bad,why", [
    ({"snapserver_host": "not a host"}, "invalid snapserver"),
    ({"snapserver_port": 0}, "port"),
    ({"snapserver_control_port": 99999}, "control port"),
    ({"snapserver_web_port": "abc"}, "web port"),
])
def test_bad_snapserver_settings_are_rejected(supervisor, bad, why):
    with pytest.raises(players_mod.SettingsError) as err:
        supervisor.update_settings(bad)
    assert why.lower() in str(err.value).lower()


def test_an_empty_snapserver_host_is_allowed(supervisor):
    """Empty means "the host you browsed the panel on"."""
    supervisor.update_settings({"snapserver_host": ""})
    assert supervisor.settings["snapserver_host"] == ""
    assert supervisor.new_player_defaults()["server"] == "127.0.0.1"


# ---- the misroute watchdog --------------------------------------------------


def test_a_player_moved_to_another_sink_is_restarted(supervisor, monkeypatch):
    """The failure that looks healthy from every other angle.

    When a sink disappears and comes back -- a codec switch, a speaker
    power-cycled -- WirePlumber moves the stream to the default sink and leaves
    it there. Verified on real hardware: the process is up, the node is back,
    the panel says "running", and the room is silent.
    """
    monkeypatch.setattr(players_mod, "HEALTH_INTERVAL", 0.1)
    monkeypatch.setattr(players_mod, "MISROUTE_GRACE", 0.4)

    linked = {"to": None}
    monkeypatch.setattr(players_mod, "stream_sink_for", lambda node: linked["to"])

    player = make(supervisor)
    player.start()
    assert wait_for(lambda: player.state == "running"), player.state
    first_pid = player._proc.pid

    linked["to"] = "auto_null"
    assert wait_for(lambda: player.state != "running", timeout=10), player.state
    assert any("instead of" in line for line in player.logs)

    linked["to"] = player.config["node"]
    assert wait_for(lambda: player.state == "running", timeout=15), player.state
    assert player._proc.pid != first_pid, "should be a fresh snapclient"
    player.stop()


def test_an_unknown_stream_target_is_not_a_misroute(supervisor, monkeypatch):
    """No PipeWire, or no links yet, means "don't know" -- not "wrong sink"."""
    monkeypatch.setattr(players_mod, "HEALTH_INTERVAL", 0.1)
    monkeypatch.setattr(players_mod, "MISROUTE_GRACE", 0.2)
    monkeypatch.setattr(players_mod, "stream_sink_for", lambda node: None)

    player = make(supervisor)
    player.start()
    assert wait_for(lambda: player.state == "running")
    pid = player._proc.pid
    time.sleep(0.8)

    assert player.state == "running"
    assert player._proc.pid == pid
    assert player.restarts == 0
    player.stop()


def test_a_momentary_misroute_does_not_restart_the_player(supervisor, monkeypatch):
    monkeypatch.setattr(players_mod, "HEALTH_INTERVAL", 0.1)
    monkeypatch.setattr(players_mod, "MISROUTE_GRACE", 5.0)

    linked = {"to": None}
    monkeypatch.setattr(players_mod, "stream_sink_for", lambda node: linked["to"])

    player = make(supervisor)
    player.start()
    assert wait_for(lambda: player.state == "running")
    pid = player._proc.pid

    linked["to"] = "auto_null"
    time.sleep(0.4)
    linked["to"] = player.config["node"]
    time.sleep(0.4)

    assert player.state == "running"
    assert player._proc.pid == pid
    assert player.restarts == 0
    player.stop()


def test_stream_sink_for_follows_the_links_not_the_request(monkeypatch):
    """target.object stays correct after a stream is moved; the links do not."""
    node = "bluez_output.00_02_5B_00_FF_04.1"
    objects = [
        {"id": 68, "info": {"props": {"node.name": node, "media.class": "Audio/Sink"}}},
        {"id": 65, "info": {"props": {"node.name": "auto_null", "media.class": "Audio/Sink"}}},
        {"id": 87, "info": {"props": {"node.name": "Snapcast",
                                      "media.class": "Stream/Output/Audio",
                                      "target.object": node}}},
        {"id": 90, "info": {"output-node-id": 87, "input-node-id": 65}},
    ]
    monkeypatch.setattr(players_mod, "_pw_dump", lambda: objects)
    assert players_mod.stream_sink_for(node) == "auto_null"

    objects[-1] = {"id": 90, "info": {"output-node-id": 87, "input-node-id": 68}}
    assert players_mod.stream_sink_for(node) == node


def test_stream_sink_for_says_nothing_when_there_is_no_stream(monkeypatch):
    monkeypatch.setattr(players_mod, "_pw_dump", list)
    assert players_mod.stream_sink_for("bluez_output.X.1") is None


# ---- Bluetooth codecs -------------------------------------------------------


CARD_DUMP = [
    {"id": 68, "info": {"props": {
        "node.name": "bluez_output.00_02_5B_00_FF_04.1",
        "media.class": "Audio/Sink",
        "api.bluez5.codec": "ldac"}}},
    {"id": 66, "info": {
        "props": {"device.name": "bluez_card.00_02_5B_00_FF_04"},
        "params": {
            "Profile": [{"index": 11, "name": "a2dp-sink"}],
            "EnumProfile": [
                {"index": 0, "name": "off", "description": "Off"},
                {"index": 5, "name": "a2dp-sink-sbc",
                 "description": "High Fidelity Playback (A2DP Sink, codec SBC)"},
                {"index": 6, "name": "a2dp-sink-sbc_xq",
                 "description": "High Fidelity Playback (A2DP Sink, codec SBC-XQ)"},
                {"index": 11, "name": "a2dp-sink",
                 "description": "High Fidelity Playback (A2DP Sink, codec LDAC)"},
            ]}}},
]


def test_codec_status_lists_what_the_speaker_and_host_share(monkeypatch):
    monkeypatch.setattr(players_mod, "_pw_dump", lambda: CARD_DUMP)
    status = players_mod.codec_status("00:02:5B:00:FF:04")

    assert status["available"] is True
    assert status["active"] == "ldac"
    assert status["headset"] is False
    codecs = {profile["codec"]: profile for profile in status["profiles"]}
    assert set(codecs) == {"SBC", "SBC-XQ", "LDAC"}
    # "off" is not a codec, and the profile for the host's preferred codec is
    # named plain a2dp-sink -- so the name cannot be used to identify it.
    assert codecs["LDAC"]["name"] == "a2dp-sink"
    assert codecs["LDAC"]["current"] is True
    assert codecs["SBC"]["current"] is False


def test_codec_status_on_a_device_pipewire_does_not_know(monkeypatch):
    monkeypatch.setattr(players_mod, "_pw_dump", list)
    status = players_mod.codec_status("00:02:5B:00:FF:04")
    assert status["available"] is False
    assert status["profiles"] == []


def test_set_codec_refuses_a_profile_that_is_not_on_offer(monkeypatch):
    monkeypatch.setattr(players_mod, "_pw_dump", lambda: CARD_DUMP)
    with pytest.raises(PlayerError) as err:
        players_mod.set_codec("00:02:5B:00:FF:04", 99)
    assert "no such codec" in str(err.value)


def test_set_codec_refuses_when_the_device_is_not_connected(monkeypatch):
    monkeypatch.setattr(players_mod, "_pw_dump", list)
    with pytest.raises(PlayerError) as err:
        players_mod.set_codec("00:02:5B:00:FF:04", 5)
    assert "connected" in str(err.value)


def test_switching_codec_carries_the_players_across(supervisor, monkeypatch):
    """A running snapclient must not be left behind by the renegotiation."""
    monkeypatch.setattr(players_mod, "CODEC_SETTLE_SECONDS", 0.5)
    switched = []
    monkeypatch.setattr(players_mod, "set_codec",
                        lambda mac, index: switched.append((mac, index)))
    monkeypatch.setattr(players_mod, "codec_status", lambda mac: {"active": "sbc_xq"})

    player = make(supervisor, mac="00:02:5B:00:FF:04",
                  node=node_for_mac("00:02:5B:00:FF:04"))
    player.start()
    assert wait_for(lambda: player.state == "running"), player.state
    first_pid = player._proc.pid

    result = supervisor.switch_codec("00:02:5B:00:FF:04", 6)

    assert switched == [("00:02:5B:00:FF:04", 6)]
    assert result["active"] == "sbc_xq"
    assert wait_for(lambda: player.state == "running"), player.state
    assert player._proc.pid != first_pid, "the player should have been restarted"
    player.stop()


def test_a_failed_codec_switch_still_brings_the_players_back(supervisor, monkeypatch):
    monkeypatch.setattr(players_mod, "CODEC_SETTLE_SECONDS", 0.5)

    def boom(mac, index):
        raise PlayerError("wpctl refused the profile")

    monkeypatch.setattr(players_mod, "set_codec", boom)

    player = make(supervisor, mac="00:02:5B:00:FF:04",
                  node=node_for_mac("00:02:5B:00:FF:04"))
    player.start()
    assert wait_for(lambda: player.state == "running"), player.state

    with pytest.raises(PlayerError):
        supervisor.switch_codec("00:02:5B:00:FF:04", 6)

    assert wait_for(lambda: player.state == "running", timeout=10), player.state
    player.stop()


# ---- the test tone ----------------------------------------------------------


FAKE_PW_PLAY = os.path.join(ROOT, "tests", "fake_pw_play.py")
TONE_NODE = "bluez_output.00_02_5B_00_FF_04.1"


@pytest.fixture
def tone(tmp_path, monkeypatch):
    """play_test_tone wired to a pw-play that describes the WAV instead."""
    monkeypatch.setattr(players_mod, "PW_PLAY", FAKE_PW_PLAY)
    monkeypatch.setattr(players_mod, "list_sinks",
                        lambda: [{"id": 68, "node": TONE_NODE, "muted": False,
                                  "description": "DX5", "bluetooth": True}])
    out = tmp_path / "tone.json"
    monkeypatch.setenv("FAKE_PW_PLAY_OUT", str(out))
    return out


@pytest.mark.parametrize("channel,left,right", [
    ("left", True, False),
    ("right", False, True),
    ("both", True, True),
])
def test_the_tone_sounds_in_the_channel_you_asked_for(tone, channel, left, right):
    result = players_mod.play_test_tone(TONE_NODE, channel, seconds=0.1)
    assert result["channel"] == channel
    assert result["muted"] is False

    played = json.loads(tone.read_text())
    assert played["channels"] == 2
    assert played["node"] == TONE_NODE, "the tone must be aimed at the sink"
    assert (played["peak_left"] > 1000) is left
    assert (played["peak_right"] > 1000) is right


def test_the_tone_refuses_a_sink_that_is_not_there(tone, monkeypatch):
    """pw-play exits 0 after playing to the *default* sink when its target is
    missing -- verified on real hardware -- so an unchecked test would sound
    from the wrong speaker and still report success."""
    monkeypatch.setattr(players_mod, "list_sinks", list)
    with pytest.raises(PlayerError) as err:
        players_mod.play_test_tone(TONE_NODE, "both", seconds=0.1)
    assert "is not present" in str(err.value)
    assert not tone.exists(), "nothing should have been played"


def test_the_tone_reports_a_muted_sink(tone, monkeypatch):
    monkeypatch.setattr(players_mod, "list_sinks",
                        lambda: [{"id": 68, "node": TONE_NODE, "muted": True}])
    result = players_mod.play_test_tone(TONE_NODE, "both", seconds=0.1)
    assert result["muted"] is True


def test_the_tone_rejects_a_channel_it_does_not_know(tone):
    with pytest.raises(PlayerError):
        players_mod.play_test_tone(TONE_NODE, "middle", seconds=0.1)


def test_a_failing_pw_play_is_reported(tone, monkeypatch):
    monkeypatch.setenv("FAKE_PW_PLAY_RC", "1")
    monkeypatch.setenv("FAKE_PW_PLAY_STDERR", "cannot connect to PipeWire\n")
    with pytest.raises(PlayerError) as err:
        players_mod.play_test_tone(TONE_NODE, "both", seconds=0.1)
    assert "cannot connect to PipeWire" in str(err.value)


def test_the_player_list_carries_the_negotiated_codec(supervisor, monkeypatch):
    """The badge reports what the link settled on, not what was asked for."""
    mac = "00:02:5B:00:FF:04"
    node = node_for_mac(mac)
    monkeypatch.setattr(players_mod, "list_sinks", lambda: [
        {"id": 68, "node": node, "description": "DX5", "bluetooth": True,
         "codec": "sbc_xq", "muted": False},
    ])
    make(supervisor, mac=mac, node=node)
    listed = supervisor.list(with_snapcast=False)[0]
    assert listed["codec"] == "SBC-XQ"
    assert listed["node_present"] is True


def test_a_player_with_no_sink_has_no_codec(supervisor, monkeypatch):
    monkeypatch.setattr(players_mod, "list_sinks", list)
    make(supervisor, mac="00:02:5B:00:FF:04",
         node=node_for_mac("00:02:5B:00:FF:04"))
    listed = supervisor.list(with_snapcast=False)[0]
    assert listed["codec"] is None
    assert listed["node_present"] is False


@pytest.mark.parametrize("raw,shown", [
    ("ldac", "LDAC"),
    ("sbc_xq", "SBC-XQ"),
    ("aptx_hd", "aptX HD"),
    ("msbc", "mSBC"),          # a headset codec: seeing it is the diagnosis
    ("something_new", "SOMETHING_NEW"),
    (None, None),
])
def test_codec_labels(raw, shown):
    assert players_mod.codec_label(raw) == shown
