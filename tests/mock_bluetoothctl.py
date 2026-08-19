#!/usr/bin/env python3
"""A fake `bluetoothctl` for the test suite.

Reproduces the parts of the real REPL that the wrapper actually has to cope
with: the ANSI-coloured prompt, the echoed command line, asynchronous [NEW]
notifications that reprint the prompt mid-stream, and the BlueZ 5.65 filtered
`devices` subcommands.

MOCK_MODE selects the scenario:
  ok            healthy adapter with three known devices (default)
  exits         bluetoothctl dies at startup -- no D-Bus socket
  nocontroller  reaches a prompt but every command says "No default controller"
  hangs         never prints a prompt -- bluetoothd wedged
  oldbluez      BlueZ < 5.65: `devices Paired` is an invalid argument
"""
import os
import sys
import threading
import time

MODE = os.environ.get("MOCK_MODE", "ok")
# BlueZ 5.8x renames the prompt after the active device and terminates it with
# "> "; 5.7x and older used a fixed "[bluetooth]" and "#". MOCK_PROMPT=legacy
# switches back so both shapes stay covered.
if os.environ.get("MOCK_PROMPT") == "legacy":
    PROMPT = "\x1b[0;94m[bluetooth]\x1b[0m# "
else:
    PROMPT = "\x1b[0;94m[DX5]> \x1b[0m"

STATE = {
    "AA:BB:CC:DD:EE:01": {"name": "Topping DX5", "paired": True, "trusted": True, "connected": True},
    "AA:BB:CC:DD:EE:02": {"name": "JBL Flip 6", "paired": True, "trusted": False, "connected": False},
    "AA:BB:CC:DD:EE:03": {"name": "FiiO BTR5", "paired": False, "trusted": False, "connected": False},
}
# Physically in range and advertising. `remove` drops a device from the known
# list (STATE) but not from the air, so a later scan finds it again -- which is
# exactly what re-pairing depends on.
IN_RANGE = {
    "AA:BB:CC:DD:EE:01": "Topping DX5",
    "AA:BB:CC:DD:EE:02": "JBL Flip 6",
    "AA:BB:CC:DD:EE:03": "FiiO BTR5",
    "AA:BB:CC:DD:EE:04": "Sony WH-1000XM4",
}

# Devices that are paired but physically absent -- powered off, or carried out of
# range. They stay in STATE but a scan never finds them again.
for _absent in os.environ.get("MOCK_OUT_OF_RANGE", "").split(","):
    IN_RANGE.pop(_absent.strip().upper(), None)

# Two controllers so the adapter picker has something to pick between.
CONTROLLERS = [
    {"mac": "C8:8A:D8:05:65:0B", "name": "DOCK", "powered": True},
    {"mac": "00:1A:7D:DA:71:13", "name": "USB-Dongle", "powered": False},
]
SELECTED = {"mac": CONTROLLERS[0]["mac"]}


def w(text):
    sys.stdout.write(text)
    sys.stdout.flush()


def line(text):
    w(text + "\r\n")


def prompt():
    w(PROMPT)


def listing(kind):
    for mac, dev in STATE.items():
        if kind is None or dev[kind.lower()]:
            line("Device %s %s" % (mac, dev["name"]))


def announce():
    """Async discovery notifications for everything in range, prompt reprints and all."""
    time.sleep(0.4)
    for mac, name in IN_RANGE.items():
        if mac in STATE:
            continue
        STATE[mac] = {"name": name, "paired": False, "trusted": False,
                      "connected": False}
        w("\r\x1b[K[\x1b[0;92mNEW\x1b[0m] Device %s %s\r\n%s" % (mac, name, PROMPT))


if MODE == "exits":
    print("Failed to connect to D-Bus: Could not connect: No such file or directory", file=sys.stderr)
    sys.exit(1)

if MODE == "hangs":
    line("Waiting to connect to bluetoothd...")
    time.sleep(300)
    sys.exit(0)

if MODE == "nocontroller":
    line("Waiting to connect to bluetoothd...")
    prompt()
    for raw in sys.stdin:
        if raw.strip() == "quit":
            break
        line("No default controller available")
        prompt()
    sys.exit(0)

line("Agent registered")
prompt()

for raw in sys.stdin:
    parts = raw.split()
    if not parts:
        prompt()
        continue
    head, args = parts[0], parts[1:]

    if head == "quit":
        break
    elif head == "devices":
        if args and MODE == "oldbluez":
            line("Invalid argument %s" % args[0])
        else:
            listing(args[0] if args else None)
    elif head == "list":
        for c in CONTROLLERS:
            line("Controller %s %s%s" % (
                c["mac"], c["name"],
                " [default]" if c["mac"] == SELECTED["mac"] else ""))
    elif head == "select":
        if args and any(c["mac"] == args[0] for c in CONTROLLERS):
            SELECTED["mac"] = args[0]
        else:
            line("Invalid argument %s" % (args[0] if args else ""))
    elif head == "show":
        mac = args[0] if args else SELECTED["mac"]
        c = next((c for c in CONTROLLERS if c["mac"] == mac), None)
        if c is None:
            line("No default controller available")
        else:
            line("Controller %s (public)" % c["mac"])
            line("\tAlias: %s" % c["name"])
            line("\tPowered: %s" % ("yes" if c["powered"] else "no"))
    elif head == "agent":
        line("Agent registered")
    elif head == "default-agent":
        line("Default agent request successful")
    elif head == "power":
        on = (args[0] if args else "on") == "on"
        for c in CONTROLLERS:
            if c["mac"] == SELECTED["mac"]:
                c["powered"] = on
        line("Changing power %s succeeded" % (args[0] if args else "on"))
    elif head == "scan":
        if args and args[0] == "on":
            line("Discovery started")
            threading.Thread(target=announce, daemon=True).start()
        else:
            line("Discovery stopped")
    elif head in ("pair", "trust", "connect", "disconnect", "remove") and args:
        mac = args[0]
        dev = STATE.get(mac)
        if dev is None:
            line("Device %s not available" % mac)
        elif head == "pair":
            dev["paired"] = True
            line("Pairing successful")
        elif head == "trust":
            dev["trusted"] = True
            line("Changing %s trust succeeded" % mac)
        elif head == "connect":
            dev["connected"] = True
            line("Connection successful")
        elif head == "disconnect":
            dev["connected"] = False
            line("Successful disconnected")
        elif head == "remove":
            STATE.pop(mac)
            line("Device has been removed")
    else:
        line("Invalid command in menu main: %s" % head)
    prompt()
