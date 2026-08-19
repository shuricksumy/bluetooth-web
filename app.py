#!/usr/bin/env python3
"""bluetooth-web -- a small pairing panel for the host's bluetoothd.

Replaces the manual `bluetuith` TUI step from the README: scan, pair, trust and
connect an audio sink from a browser, then feed its node name to snapclient's
PIPEWIRE_NODE.

Deliberately small: no database, no build step, no websockets. The browser polls
/api/devices and every action is a POST that returns the refreshed table.
"""

import hmac
import logging
import os
import re
import threading
import time

from flask import Flask, jsonify, request, send_from_directory

from btctl import Bluetoothctl, BluetoothBusy, BluetoothctlError, StepFailure

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
    return jsonify(poll_seconds=POLL_SECONDS, auth=bool(ADMIN_PASSWORD))


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
    # threaded=True so the 5s poll and the healthcheck are not queued behind a
    # long pair/connect; the REPL itself is still serialised by btctl's lock.
    app.run(
        host=os.environ.get("BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        threaded=True,
    )


if __name__ == "__main__":
    main()
