"""Thin wrapper around one long-lived, interactive `bluetoothctl` process.

Why drive a REPL instead of speaking D-Bus directly: pairing needs a registered
BlueZ *agent* to answer the pairing request, and `bluetoothctl agent
NoInputNoOutput` gives us a working one for free. Reimplementing that over raw
D-Bus (exporting an org.bluez.Agent1 object, handling RequestConfirmation, ...)
is a lot of surface for a panel whose whole job is "click pair".

Everything still goes to the host's bluetoothd over the system D-Bus socket --
bluetoothctl is just a D-Bus client, so this container never touches the radio
itself.

The process is a single REPL shared by every HTTP worker thread, so all access
is serialised through one RLock. RLock rather than Lock because the compound
operations (quick-pair, scan-then-list) call the primitives while already
holding it.
"""

import os
import re
import threading
import time

import pexpect

# bluetoothctl's prompt, e.g. "[bluetooth]# " or "[Speaker]# ", wrapped in the
# colour escapes bt_shell emits when stdout is a tty (pexpect always gives it
# one). The optional escape run sits between "]" and "#" because BlueZ closes
# the colour right after the bracket: "\x1b[0;94m[bluetooth]\x1b[0m# ".
PROMPT = re.compile(r"(?:\x1b\[[0-9;]*m)*\[[^\]\n]*\](?:\x1b\[[0-9;]*m)*#\s")

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# Lines of a `devices` listing: "Device AA:BB:CC:DD:EE:FF Living Room Speaker".
# Anchored at the start so the asynchronous "[NEW] Device ..." / "[CHG] Device
# ..." notifications that bluetoothctl interleaves are not mistaken for listing
# output.
DEVICE_LINE = re.compile(r"^Device\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s*(.*)$")

# Substrings that mean "bluetoothctl accepted the command but BlueZ refused it".
FAILURE_MARKERS = (
    "Failed to",
    "org.bluez.Error",
    "No default controller available",
    "not available",
)

DEFAULT_TIMEOUT = 15.0
START_TIMEOUT = 15.0
# Pairing waits on the peer device (and possibly a human pressing a button).
PAIR_TIMEOUT = 60.0
CONNECT_TIMEOUT = 45.0


class BluetoothctlError(RuntimeError):
    """A bluetoothctl command ran but BlueZ refused it."""

    status = 502


class BluetoothUnavailable(BluetoothctlError):
    """bluetoothctl could not be started, or there is no controller at all."""

    status = 503


class BluetoothBusy(BluetoothctlError):
    """Another request holds the REPL (almost always an in-flight scan)."""

    status = 409


def strip_ansi(text):
    return ANSI.sub("", text or "").replace("\r", "").replace("\x1b", "")


class Bluetoothctl:
    def __init__(self, command="bluetoothctl"):
        self._command = command
        self._lock = threading.RLock()
        self._child = None
        # Non-fatal notes from the last startup, surfaced in /api/devices so a
        # missing controller or an ancient BlueZ is visible in the UI instead of
        # showing up as a mysteriously empty table.
        self.warnings = []

    # ---- process lifecycle -------------------------------------------------

    def _spawn(self):
        env = dict(os.environ)
        # libdbus looks here for the system bus; the compose file mounts the
        # host's socket at exactly this path.
        env.setdefault(
            "DBUS_SYSTEM_BUS_ADDRESS", "unix:path=/run/dbus/system_bus_socket"
        )
        try:
            child = pexpect.spawn(
                self._command,
                env=env,
                encoding="utf-8",
                codec_errors="replace",
                timeout=DEFAULT_TIMEOUT,
                dimensions=(24, 200),  # keep readline from wrapping long lines
            )
        except pexpect.ExceptionPexpect as exc:
            raise BluetoothUnavailable(f"cannot start {self._command}: {exc}") from exc

        try:
            child.expect(PROMPT, timeout=START_TIMEOUT)
        except pexpect.EOF:
            raise BluetoothUnavailable(
                "bluetoothctl exited immediately -- is the host's D-Bus socket "
                "mounted at /run/dbus/system_bus_socket?"
            ) from None
        except pexpect.TIMEOUT:
            child.terminate(force=True)
            raise BluetoothUnavailable(
                "bluetoothctl never reached its prompt -- bluetoothd is probably "
                "not running on the host"
            ) from None

        self._child = child
        self.warnings = []

        # Register an agent so pairing requests get answered. NoInputNoOutput
        # means "just-works" pairing: no PIN prompt to relay through the web UI.
        # These are best-effort -- with no controller present they all fail, and
        # the clearer complaint comes from the first real command.
        for cmd in ("agent NoInputNoOutput", "default-agent", "power on"):
            try:
                out = self._send(cmd)
            except BluetoothctlError as exc:
                self._warn(f"{cmd}: {exc}")
                continue
            if any(marker in out for marker in FAILURE_MARKERS):
                self._warn(f"{cmd}: {_first_failure_line(out) or 'failed'}")

        return child

    def _warn(self, message):
        """Record a note for the UI, once -- these are re-hit on every poll."""
        if message not in self.warnings:
            self.warnings.append(message)

    def _ensure_started(self):
        if self._child is not None and self._child.isalive():
            return self._child
        return self._spawn()

    def close(self):
        with self._lock:
            if self._child is not None:
                try:
                    self._child.sendline("quit")
                    self._child.expect(pexpect.EOF, timeout=3)
                except (pexpect.ExceptionPexpect, OSError):
                    pass
                finally:
                    self._child.terminate(force=True)
                    self._child = None

    # ---- locking -----------------------------------------------------------

    class _Locked:
        def __init__(self, lock, timeout):
            self._lock = lock
            self._timeout = timeout

        def __enter__(self):
            if not self._lock.acquire(timeout=self._timeout):
                raise BluetoothBusy(
                    "bluetoothctl is busy (a scan is probably running) -- try again"
                )
            return self

        def __exit__(self, *exc):
            self._lock.release()
            return False

    def _locked(self, timeout=-1):
        return self._Locked(self._lock, timeout)

    # ---- command plumbing --------------------------------------------------

    def _drain(self, child):
        """Discard anything already buffered.

        bluetoothctl reprints its prompt every time it emits an asynchronous
        [NEW]/[CHG]/[DEL] notification. Without this, a stale prompt left over
        from one of those would satisfy the next expect() instantly and we would
        read the *previous* command's tail as this command's output.
        """
        while True:
            try:
                child.read_nonblocking(size=4096, timeout=0.1)
            except (pexpect.TIMEOUT, pexpect.EOF, OSError):
                return

    def _send(self, cmd, timeout=DEFAULT_TIMEOUT):
        """Run one command in the REPL and return its output, ANSI stripped.

        Callers must already hold the lock. `cmd` is only ever a literal from
        this module plus a MAC that app.py has validated -- see the note on
        is_valid_mac() about why that matters.
        """
        child = self._ensure_started()
        self._drain(child)
        child.sendline(cmd)
        try:
            child.expect(PROMPT, timeout=timeout)
        except pexpect.EOF:
            self._child = None
            raise BluetoothUnavailable(
                "bluetoothctl exited while running %r" % cmd
            ) from None
        except pexpect.TIMEOUT:
            raise BluetoothctlError("timed out waiting for %r" % cmd) from None
        out = strip_ansi(child.before)
        # The pty echoes the command back; drop it so the UI shows BlueZ's
        # answer rather than "pair AA:BB:.. / Pairing successful".
        lines = out.splitlines()
        if lines and lines[0].strip() == cmd:
            out = "\n".join(lines[1:])
        return out

    def _send_checked(self, cmd, timeout=DEFAULT_TIMEOUT):
        out = self._send(cmd, timeout=timeout)
        failure = _first_failure_line(out)
        if failure:
            if "No default controller available" in failure:
                raise BluetoothUnavailable(failure)
            raise BluetoothctlError(failure)
        return out

    # ---- device listing ----------------------------------------------------

    def _device_macs(self, kind=None):
        """MACs from `devices [Paired|Connected|Trusted]`, plus their names.

        The filtered subcommands are BlueZ 5.65+. On anything older bluetoothctl
        answers "Invalid argument", which we report as a warning rather than an
        error: the table still lists devices, only the badges go blank.
        """
        cmd = "devices" if kind is None else "devices %s" % kind
        out = self._send(cmd)
        if "Invalid argument" in out or "Invalid command" in out:
            self._warn(
                "`devices <filter>` not supported -- BlueZ 5.65+ is needed for "
                "the Paired/Connected/Trusted badges"
            )
            return {}
        if "No default controller available" in out:
            raise BluetoothUnavailable(
                "no Bluetooth controller available -- check that the host has an "
                "adapter and that bluetoothd is running"
            )

        found = {}
        for line in out.splitlines():
            match = DEVICE_LINE.match(line.strip())
            if match:
                mac = match.group(1).upper()
                found[mac] = match.group(2).strip()
        return found

    def list_devices(self, lock_timeout=-1):
        """Build the whole device table in four calls, not one `info` per device.

        `devices` alone gives names but no state; the three filtered variants
        give state but are cheap set lookups. For 20 known devices that is 4
        round trips instead of 21.
        """
        with self._locked(lock_timeout):
            self.warnings = [w for w in self.warnings if "not supported" not in w]
            known = self._device_macs()
            paired = self._device_macs("Paired")
            connected = self._device_macs("Connected")
            trusted = self._device_macs("Trusted")

            names = {}
            for source in (known, paired, connected, trusted):
                for mac, name in source.items():
                    if name and not names.get(mac):
                        names[mac] = name

            devices = []
            for mac in sorted(set(names) | set(known) | set(connected)):
                devices.append(
                    {
                        "mac": mac,
                        "name": names.get(mac) or mac,
                        "paired": mac in paired,
                        "connected": mac in connected,
                        "trusted": mac in trusted,
                    }
                )
            # Connected first, then paired, then by name -- the rows you are
            # likely to act on stay at the top while a scan floods the tail.
            devices.sort(
                key=lambda d: (not d["connected"], not d["paired"], d["name"].lower())
            )
            return devices

    # ---- actions -----------------------------------------------------------

    def scan(self, duration):
        with self._locked(-1):
            self._send_checked("scan on")
            try:
                time.sleep(duration)
            finally:
                # Always stop the radio scanning, even if the sleep is cut short.
                try:
                    self._send("scan off")
                except BluetoothctlError:
                    pass
            return self.list_devices()

    def pair(self, mac):
        out = self._send("pair %s" % mac, timeout=PAIR_TIMEOUT)
        failure = _first_failure_line(out)
        if failure and "AlreadyExists" not in failure:
            raise BluetoothctlError(failure)
        return out

    def trust(self, mac):
        return self._send_checked("trust %s" % mac)

    def connect(self, mac):
        return self._send_checked("connect %s" % mac, timeout=CONNECT_TIMEOUT)

    def disconnect(self, mac):
        return self._send_checked("disconnect %s" % mac, timeout=CONNECT_TIMEOUT)

    def remove(self, mac):
        return self._send_checked("remove %s" % mac)

    def quick_pair(self, mac):
        """pair -> trust -> connect, the sequence the README used to spell out.

        Trust matters for audio sinks: without it BlueZ refuses the device's own
        reconnect attempt when you power the speaker on again.
        """
        steps = []
        with self._locked(-1):
            for name, action in (
                ("pair", self.pair),
                ("trust", self.trust),
                ("connect", self.connect),
            ):
                try:
                    output = action(mac)
                except BluetoothctlError as exc:
                    steps.append({"step": name, "ok": False, "output": str(exc)})
                    raise StepFailure(str(exc), steps) from exc
                steps.append({"step": name, "ok": True, "output": _tail(output)})
        return steps


class StepFailure(BluetoothctlError):
    """A multi-step action failed partway; carries what did succeed."""

    def __init__(self, message, steps):
        super().__init__(message)
        self.steps = steps


def _first_failure_line(out):
    for line in strip_ansi(out).splitlines():
        line = line.strip()
        if any(marker in line for marker in FAILURE_MARKERS):
            return line
    return None


def _tail(out, limit=3):
    lines = [l.strip() for l in strip_ansi(out).splitlines() if l.strip()]
    return " / ".join(lines[-limit:])
