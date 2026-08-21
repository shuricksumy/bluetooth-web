#!/usr/bin/env python3
"""bluetooth-web -- a small pairing panel for the host's bluetoothd.

Replaces the manual `bluetuith` TUI step from the README: scan, pair, trust and
connect an audio sink from a browser, then feed its node name to snapclient's
PIPEWIRE_NODE.

Deliberately small: no database, no build step, no websockets. The browser polls
/api/devices and every action is a POST that returns the refreshed table.
"""

import atexit
import hmac
import logging
import os
import re
import threading
import time

from flask import Flask, jsonify, request, send_from_directory

import players as players_mod
import snapctl
from btctl import Bluetoothctl, BluetoothBusy, BluetoothctlError, StepFailure
from players import PlayerError, SettingsError, Supervisor

# A MAC and nothing else. This is the only user-controlled value that reaches
# bluetoothctl's stdin, and the REPL takes one command per line, so a MAC
# carrying an embedded newline would let a caller append arbitrary bluetoothctl
# commands to the session.
#
# fullmatch(), NOT match(): in Python's re, "$" also matches immediately before
# a trailing newline, so re.match(r"^...$", "AA:BB:CC:DD:EE:FF\nscan on") would
# happily accept exactly the payload this is meant to stop.
MAC_RE = re.compile(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}")

MAX_SCAN_SECONDS = 60
DEFAULT_SCAN_SECONDS = 10

# How often the browser re-reads the device table. This costs four D-Bus
# property reads and touches the radio not at all, so it is safe to leave
# running next to connected audio -- unlike a scan. Tunable anyway.
POLL_SECONDS = max(1.0, float(os.environ.get("POLL_SECONDS", "5")))

# How long /api/devices waits for the REPL lock before giving up and serving the
# last known table. A scan holds the lock for its full duration; without this the
# poller and the Docker healthcheck would both stall behind it.
DEVICES_LOCK_TIMEOUT = 2.0

log = logging.getLogger("bluetooth-web")

app = Flask(__name__, static_folder="static", static_url_path="")
btctl = Bluetoothctl(command=os.environ.get("BLUETOOTHCTL", "bluetoothctl"))

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# Players are snapclient children of this process. connect_bluetooth is injected
# so players.py never has to know about BlueZ: starting a player whose sink is a
# Bluetooth speaker means connecting that speaker first.
def _connect_for_player(mac):
    return btctl.connect(mac)


supervisor = Supervisor(connect_bluetooth=_connect_for_player)

_cache_lock = threading.Lock()
_cache = {"devices": [], "at": 0.0}


def is_valid_mac(value):
    return bool(MAC_RE.fullmatch(value or ""))


# ---- auth -------------------------------------------------------------------


@app.before_request
def require_auth():
    """Gate everything -- API and the page itself -- when ADMIN_PASSWORD is set.

    Unset (the default) means no auth at all, which is why the README is explicit
    that this belongs on a trusted LAN and not on a port-forward.
    """
    if not ADMIN_PASSWORD:
        return None
    auth = request.authorization
    if (
        auth
        and auth.type == "basic"
        and hmac.compare_digest(auth.username or "", ADMIN_USER)
        and hmac.compare_digest(auth.password or "", ADMIN_PASSWORD)
    ):
        return None
    return (
        jsonify(error="authentication required"),
        401,
        {"WWW-Authenticate": 'Basic realm="bluetooth-web"'},
    )


# ---- helpers ----------------------------------------------------------------


def devices_payload(devices, **extra):
    for device in devices:
        # What this speaker will show up as in PipeWire once connected. Saves
        # the `pw-cli ls Node | grep` step when creating a player for it.
        device.setdefault("node", players_mod.node_for_mac(device["mac"]))
    payload = {"devices": devices, "warnings": list(btctl.warnings)}
    payload.update(extra)
    return payload


def cache_devices(devices):
    with _cache_lock:
        _cache["devices"] = devices
        _cache["at"] = time.time()


def cached_devices():
    with _cache_lock:
        return list(_cache["devices"]), _cache["at"]


def mac_or_400(mac):
    """Return a normalised MAC, or a Flask response to return immediately."""
    if not is_valid_mac(mac):
        return None, (jsonify(error="invalid MAC address"), 400)
    return mac.upper(), None


def action(mac, func, label):
    mac, bad = mac_or_400(mac)
    if bad:
        return bad
    try:
        output = func(mac)
    except BluetoothctlError as exc:
        return jsonify(ok=False, error=str(exc), action=label), exc.status
    try:
        devices = btctl.list_devices(lock_timeout=DEVICES_LOCK_TIMEOUT)
        cache_devices(devices)
    except BluetoothctlError:
        devices, _ = cached_devices()
    return jsonify(devices_payload(devices, ok=True, action=label, output=output.strip()))


@app.errorhandler(snapctl.SnapcastError)
def handle_snapcast_error(exc):
    return jsonify(ok=False, error=str(exc)), exc.status


@app.errorhandler(SettingsError)
def handle_settings_error(exc):
    return jsonify(ok=False, error=str(exc)), exc.status


@app.errorhandler(PlayerError)
def handle_player_error(exc):
    return jsonify(ok=False, error=str(exc)), exc.status


@app.errorhandler(BluetoothctlError)
def handle_bluetooth_error(exc):
    """Last resort: a clear JSON error instead of a pexpect traceback in a 500."""
    return jsonify(ok=False, error=str(exc)), exc.status


# ---- routes -----------------------------------------------------------------


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/config")
def api_config():
    return jsonify(
        poll_seconds=POLL_SECONDS,
        auth=bool(ADMIN_PASSWORD),
        # Seeds the Add-player form. An empty host means the page falls back to
        # whatever it was browsed on.
        # From the stored settings, not the environment: the environment only
        # seeds them, and the web UI can change them afterwards.
        snapserver={
            "host": supervisor.settings["snapserver_host"],
            "port": supervisor.settings["snapserver_port"],
            "control_port": supervisor.settings["snapserver_control_port"],
            "web_port": supervisor.settings["snapserver_web_port"],
        },
    )


@app.get("/api/settings")
def api_get_settings():
    return jsonify(settings=supervisor.settings)


@app.patch("/api/settings")
def api_patch_settings():
    settings = supervisor.update_settings(request.get_json(silent=True) or {})
    return jsonify(ok=True, settings=settings)


@app.get("/api/adapters")
def api_adapters():
    """The controllers on this host (hci0, hci1, ...) and which one is active."""
    try:
        adapters = btctl.list_adapters(lock_timeout=DEVICES_LOCK_TIMEOUT)
    except BluetoothBusy:
        return jsonify(adapters=[], busy=True)
    except BluetoothctlError as exc:
        return jsonify(adapters=[], error=str(exc)), exc.status
    return jsonify(adapters=adapters)


@app.post("/api/adapters/reset")
def api_reset_adapter():
    """Power-cycle the controller: the fix for a radio that finds nothing.

    Deliberately its own route rather than part of adapter selection -- it
    disconnects every connected device, so the UI confirms first.
    """
    try:
        steps = btctl.reset_adapter()
    except StepFailure as exc:
        return jsonify(ok=False, error=str(exc), steps=exc.steps, action="reset"), exc.status
    except BluetoothctlError as exc:
        return jsonify(ok=False, error=str(exc), action="reset"), exc.status
    try:
        devices = btctl.list_devices(lock_timeout=DEVICES_LOCK_TIMEOUT)
        cache_devices(devices)
    except BluetoothctlError:
        devices, _ = cached_devices()
    return jsonify(devices_payload(devices, ok=True, action="reset", steps=steps))


@app.post("/api/adapter/<mac>")
def api_select_adapter(mac):
    """Switch which controller every other route acts on."""
    mac, bad = mac_or_400(mac)
    if bad:
        return bad
    try:
        adapters = btctl.select_adapter(mac)
    except BluetoothctlError as exc:
        return jsonify(ok=False, error=str(exc), action="adapter"), exc.status
    try:
        devices = btctl.list_devices(lock_timeout=DEVICES_LOCK_TIMEOUT)
        cache_devices(devices)
    except BluetoothctlError:
        devices, _ = cached_devices()
    return jsonify(
        devices_payload(devices, ok=True, action="adapter", adapters=adapters)
    )


# ---- players ----------------------------------------------------------------


@app.get("/api/players")
def api_players():
    return jsonify(players=supervisor.list())


@app.get("/api/sinks")
def api_sinks():
    """PipeWire sinks available right now -- what a player can be bound to."""
    return jsonify(sinks=players_mod.list_sinks())


@app.post("/api/players")
def api_create_player():
    player = supervisor.create(request.get_json(silent=True) or {})
    return jsonify(ok=True, player=player.status(), players=supervisor.list()), 201


@app.patch("/api/players/<player_id>")
def api_update_player(player_id):
    player = supervisor.update(player_id, request.get_json(silent=True) or {})
    return jsonify(ok=True, player=player.status(), players=supervisor.list())


@app.delete("/api/players/<player_id>")
def api_delete_player(player_id):
    supervisor.delete(player_id)
    return jsonify(ok=True, players=supervisor.list())


@app.post("/api/players/<player_id>/<action>")
def api_player_action(player_id, action):
    if action not in ("start", "stop", "restart"):
        return jsonify(ok=False, error="unknown action"), 404
    player = supervisor.get(player_id)
    if action in ("stop", "restart"):
        player.stop()
    if action in ("start", "restart"):
        player.start()
    return jsonify(ok=True, players=supervisor.list())


@app.post("/api/players/<player_id>/test/<channel>")
def api_player_test(player_id, channel):
    """Play a test tone into this player's sink. Mixes with whatever is playing."""
    player = supervisor.get(player_id)
    result = players_mod.play_test_tone(player.config.get("node"), channel)
    return jsonify(ok=True, test=result)


@app.post("/api/devices/<mac>/test/<channel>")
def api_device_test(mac, channel):
    """The same tone, aimed at a paired device -- no player needed."""
    mac, bad = mac_or_400(mac)
    if bad:
        return bad
    result = players_mod.play_test_tone(players_mod.node_for_mac(mac), channel)
    return jsonify(ok=True, test=result)


@app.get("/api/devices/<mac>/codec")
def api_device_codec(mac):
    mac, bad = mac_or_400(mac)
    if bad:
        return bad
    return jsonify(ok=True, codec=players_mod.codec_status(mac))


@app.post("/api/devices/<mac>/codec")
def api_set_device_codec(mac):
    """Switch the A2DP codec, stopping and restarting this device's players."""
    mac, bad = mac_or_400(mac)
    if bad:
        return bad
    body = request.get_json(silent=True) or {}
    try:
        index = int(body["index"])
    except (KeyError, TypeError, ValueError):
        return jsonify(ok=False, error="index must be a codec profile number"), 400
    codec = supervisor.switch_codec(mac, index)
    return jsonify(ok=True, codec=codec, players=supervisor.list())


@app.post("/api/players/<player_id>/control/<command>")
def api_player_control(player_id, command):
    """Transport control. Snapcast has no "stop" -- pause is the stop.

    The command acts on the *stream* the player's group is attached to, so it
    affects every client in that group, exactly like pressing pause in Snapweb
    or Music Assistant.
    """
    player = supervisor.get(player_id)
    if command not in snapctl.COMMANDS:
        return jsonify(ok=False, error="unsupported command"), 404

    host = player.config["server"]
    port = player.config.get("control_port", snapctl.DEFAULT_CONTROL_PORT)
    info = snapctl.describe(host, port, player.client_id, use_cache=False)
    if info is None:
        return jsonify(ok=False, error="the snapserver does not know this client"), 404
    if not info["can_control"]:
        return jsonify(
            ok=False,
            error="stream %r does not support transport control" % info["stream_id"],
        ), 409

    snapctl.control(host, port, info["stream_id"], command)
    return jsonify(ok=True, players=supervisor.list())


@app.post("/api/players/<player_id>/volume")
def api_player_volume(player_id):
    player = supervisor.get(player_id)
    body = request.get_json(silent=True) or {}
    percent, muted = body.get("percent"), body.get("muted")
    if percent is None and muted is None:
        return jsonify(ok=False, error="percent or muted is required"), 400
    if percent is not None:
        try:
            percent = int(percent)
        except (TypeError, ValueError):
            return jsonify(ok=False, error="percent must be a number"), 400
        if not 0 <= percent <= 100:
            return jsonify(ok=False, error="percent must be 0-100"), 400

    snapctl.set_volume(
        player.config["server"],
        player.config.get("control_port", snapctl.DEFAULT_CONTROL_PORT),
        player.client_id, percent=percent, muted=muted,
    )
    return jsonify(ok=True, players=supervisor.list())


@app.get("/api/snapcast/stale")
def api_stale_clients():
    """Clients the snapserver still remembers but nothing is using.

    Snapcast keeps a client forever once it has connected, so deleting a player
    here leaves a ghost in Snapweb and Music Assistant until someone removes it.
    """
    players = supervisor.list(with_snapcast=False)
    if not players:
        return jsonify(stale=[])
    first = players[0]
    stale = snapctl.stale_clients(
        first["server"], first.get("control_port", snapctl.DEFAULT_CONTROL_PORT)
    )
    known = {p["client_id"] for p in players}
    return jsonify(stale=[c for c in stale if c["id"] not in known])


@app.delete("/api/snapcast/client/<path:client_id>")
def api_delete_stale(client_id):
    players = supervisor.list(with_snapcast=False)
    if not players:
        return jsonify(ok=False, error="no player knows which server to ask"), 400
    if client_id in {p["client_id"] for p in players}:
        return jsonify(ok=False, error="that client belongs to a live player"), 409
    first = players[0]
    snapctl.delete_client(
        first["server"], first.get("control_port", snapctl.DEFAULT_CONTROL_PORT),
        client_id,
    )
    return jsonify(ok=True)


@app.get("/api/players/<player_id>/logs")
def api_player_logs(player_id):
    player = supervisor.get(player_id)
    return jsonify(logs=list(player.logs))


# ---- devices ----------------------------------------------------------------


@app.get("/api/devices")
def api_devices():
    try:
        devices = btctl.list_devices(lock_timeout=DEVICES_LOCK_TIMEOUT)
    except BluetoothBusy:
        # A scan owns the REPL. Serve what we last saw rather than erroring.
        devices, at = cached_devices()
        return jsonify(devices_payload(devices, stale=True, stale_since=at, busy=True))
    except BluetoothctlError as exc:
        devices, at = cached_devices()
        return (
            jsonify(devices_payload(devices, error=str(exc), stale=bool(devices))),
            exc.status,
        )
    cache_devices(devices)
    return jsonify(devices_payload(devices))


@app.post("/api/scan")
def api_scan():
    body = request.get_json(silent=True) or {}
    try:
        duration = float(body.get("duration", DEFAULT_SCAN_SECONDS))
    except (TypeError, ValueError):
        return jsonify(error="duration must be a number"), 400
    if duration != duration or duration <= 0:  # NaN or non-positive
        return jsonify(error="duration must be a positive number"), 400
    duration = min(duration, MAX_SCAN_SECONDS)

    try:
        devices = btctl.scan(duration)
    except BluetoothctlError as exc:
        return jsonify(ok=False, error=str(exc)), exc.status
    cache_devices(devices)
    return jsonify(devices_payload(devices, ok=True, scanned=duration))


@app.post("/api/pair/<mac>")
def api_pair(mac):
    """Quick pair: pair, then trust, then connect."""
    return multi_step(mac, btctl.quick_pair, "pair")


def multi_step(mac, func, label):
    """Shared plumbing for the compound actions (pair / reconnect / repair)."""
    mac, bad = mac_or_400(mac)
    if bad:
        return bad
    try:
        steps = func(mac)
    except StepFailure as exc:
        return (
            jsonify(ok=False, error=str(exc), steps=exc.steps, action=label),
            exc.status,
        )
    except BluetoothctlError as exc:
        return jsonify(ok=False, error=str(exc), action=label), exc.status
    try:
        devices = btctl.list_devices(lock_timeout=DEVICES_LOCK_TIMEOUT)
        cache_devices(devices)
    except BluetoothctlError:
        devices, _ = cached_devices()
    return jsonify(devices_payload(devices, ok=True, action=label, steps=steps))


@app.post("/api/reconnect/<mac>")
def api_reconnect(mac):
    """Disconnect then connect -- the fix for a stale or silent link."""
    return multi_step(mac, btctl.reconnect, "reconnect")


@app.post("/api/repair/<mac>")
def api_repair(mac):
    """Forget the pairing and pair again. The device must be in pairing mode."""
    return multi_step(mac, btctl.repair, "repair")


@app.post("/api/connect/<mac>")
def api_connect(mac):
    return action(mac, btctl.connect, "connect")


@app.post("/api/disconnect/<mac>")
def api_disconnect(mac):
    return action(mac, btctl.disconnect, "disconnect")


@app.post("/api/trust/<mac>")
def api_trust(mac):
    return action(mac, btctl.trust, "trust")


@app.post("/api/remove/<mac>")
def api_remove(mac):
    return action(mac, btctl.remove, "remove")


def main():
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("DEBUG") == "true" else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not ADMIN_PASSWORD:
        log.warning(
            "ADMIN_PASSWORD is not set -- every route is open to anyone who can "
            "reach this port. Intended for a trusted LAN only."
        )
    supervisor.autostart()
    atexit.register(supervisor.stop_all)

    # threaded=True so the 5s poll and the healthcheck are not queued behind a
    # long pair/connect; the REPL itself is still serialised by btctl's lock.
    app.run(
        host=os.environ.get("BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        threaded=True,
    )


if __name__ == "__main__":
    main()
