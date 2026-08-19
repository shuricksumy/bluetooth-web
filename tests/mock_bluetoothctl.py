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
PROMPT = "\x1b[0;94m[bluetooth]\x1b[0m# "

STATE = {
    "AA:BB:CC:DD:EE:01": {"name": "Topping DX5", "paired": True, "trusted": True, "connected": True},
    "AA:BB:CC:DD:EE:02": {"name": "JBL Flip 6", "paired": True, "trusted": False, "connected": False},
    "AA:BB:CC:DD:EE:03": {"name": "FiiO BTR5", "paired": False, "trusted": False, "connected": False},
}
DISCOVERABLE = ("AA:BB:CC:DD:EE:04", "Sony WH-1000XM4")


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
    """Async discovery notification, prompt reprint and all."""
    time.sleep(0.4)
    mac, name = DISCOVERABLE
    STATE.setdefault(mac, {"name": name, "paired": False, "trusted": False, "connected": False})
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
    elif head == "agent":
        line("Agent registered")
    elif head == "default-agent":
        line("Default agent request successful")
    elif head == "power":
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
