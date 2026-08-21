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
import subprocess
import threading
import time

import pexpect

# bluetoothctl's prompt, wrapped in the colour escapes bt_shell emits when
# stdout is a tty (pexpect always gives it one).
#
# BOTH terminators matter. BlueZ <= 5.7x ends the prompt with "#":
#     \x1b[0;94m[bluetooth]\x1b[0m#
# BlueZ 5.8x switched to "> " and renames the prompt after whatever is currently
# selected, so on a box with a connected DX5 it reads:
#     \x1b[0;94m[bluetoothctl]> \x1b[0m   ... then ...   \x1b[0;94m[DX5]> \x1b[0m
# Matching only "#" makes the whole panel hang on any modern distro while the
# one-shot `bluetoothctl show` keeps working, which is a thoroughly confusing
# way to fail.
PROMPT = re.compile(r"(?:\x1b\[[0-9;]*m)*\[[^\]\n]*\](?:\x1b\[[0-9;]*m)*[#>]\s")

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# Lines of a `devices` listing: "Device AA:BB:CC:DD:EE:FF Living Room Speaker".
# Anchored at the start so the asynchronous "[NEW] Device ..." / "[CHG] Device
# ..." notifications that bluetoothctl interleaves are not mistaken for listing
# output.
DEVICE_LINE = re.compile(r"^Device\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s*(.*)$")

# Lines of a `list` listing: "Controller C8:8A:D8:05:65:0B DOCK [default]".
CONTROLLER_LINE = re.compile(
    r"^Controller\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s*(.*)$"
)

# sysfs is the only place that knows how an adapter is attached. Containers
# share the host's /sys, so this works without mounting anything extra.
SYS_BLUETOOTH = "/sys/class/bluetooth"
HCI_NAME = re.compile(r"^hci\d+$")

BUS_LABELS = {"usb": "USB", "pci": "PCI", "serial": "UART", "platform": "platform",
              "sdio": "SDIO", "bluetooth": "virtual"}

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

# `pair` is acknowledged immediately and answered much later -- see pair().
PAIR_SUCCESS = r"Pairing successful"
PAIR_FAILED = r"Failed to pair:\s*(?P<why>[^\r\n]+)"
PAIR_UNAVAILABLE = r"Device [0-9A-Fa-f:]{17} not available"

# Recovering a wedged controller: power off, wait, power on -- and the first
# power on after a wedge often fails on its own, so try more than once.
POWER_SETTLE = 2.0
POWER_ATTEMPTS = 3
PROBE_SECONDS = 6.0

# A scan that turns up nothing *new* is the visible symptom of a controller
# stuck mid-inquiry: everything still looks healthy -- powered adapter, no
# errors, bluetoothd up -- and nothing is ever discovered again. A real scan
# almost always sees something, if only a passing phone advertising a random
# address, so silence is worth mentioning. Only for scans long enough to mean
# it, hence the threshold.
STUCK_SCAN_SECONDS = 5.0
STUCK_HINT = (
    "that scan discovered nothing new, not even a passing phone or watch. If "
    "that keeps happening the controller may be stuck -- Reset radio "
    "power-cycles it."
)
CONNECT_TIMEOUT = 45.0
# How long re-pairing scans for the device it just forgot.
REDISCOVER_TIMEOUT = 20.0


class BluetoothctlError(RuntimeError):
    """A bluetoothctl command ran but BlueZ refused it."""

    status = 502


class BluetoothUnavailable(BluetoothctlError):
    """bluetoothctl could not be started, or there is no controller at all."""

    status = 503


class BluetoothBusy(BluetoothctlError):
    """Another request holds the REPL (almost always an in-flight scan)."""

    status = 409


def _read_text(path):
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return ""


# BlueZ names each adapter object /org/bluez/hciN and carries its Address, which
# is the only reliable way to tie a controller MAC to an hciN: current kernels no
# longer expose /sys/class/bluetooth/hciN/address.
ADAPTER_OBJECT = re.compile(r"^/org/bluez/(hci\d+)$")
ADAPTER_ADDRESS = re.compile(
    r'string "Address" variant string "((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})"'
)


def adapter_paths():
    """Map controller MAC -> hciN, straight out of BlueZ's object manager."""
    ok, out = _dbus_send(
        "--dest=org.bluez", "/", "org.freedesktop.DBus.ObjectManager.GetManagedObjects",
        timeout=15,
    )
    if not ok:
        return {}

    found = {}
    # _dbus_send has already collapsed the runs of whitespace dbus-send emits.
    for chunk in out.split('object path "')[1:]:
        path = chunk.split('"', 1)[0]
        match = ADAPTER_OBJECT.match(path)
        if not match:
            continue  # a device under the adapter, not the adapter itself
        address = ADAPTER_ADDRESS.search(chunk)
        if address:
            found[address.group(1).upper()] = match.group(1)
    return found


def adapter_hardware(macs=None):
    """Map controller MAC -> how it is attached (hciN, bus, product name).

    Purely cosmetic: it is what lets the UI say "hci0 · USB · Intel" so you can
    tell an internal radio from a dongle. Everything degrades to blank strings if
    sysfs or the object manager is unavailable.
    """
    paths = adapter_paths()
    if not paths and macs and len(macs) == 1:
        # One controller and one hciN: the pairing is unambiguous even without
        # the object manager.
        try:
            names = [n for n in sorted(os.listdir(SYS_BLUETOOTH)) if HCI_NAME.match(n)]
        except OSError:
            names = []
        if len(names) == 1:
            paths = {macs[0].upper(): names[0]}

    found = {}
    for mac, hci in paths.items():
        device = os.path.join(SYS_BLUETOOTH, hci, "device")
        bus = os.path.basename(os.path.realpath(os.path.join(device, "subsystem")))
        # For USB the product/manufacturer strings sit on the parent device, not
        # on the interface that hciN/device points at.
        product = (
            _read_text(os.path.join(device, "product"))
            or _read_text(os.path.join(device, os.pardir, "product"))
        )
        vendor = (
            _read_text(os.path.join(device, "manufacturer"))
            or _read_text(os.path.join(device, os.pardir, "manufacturer"))
        )
        found[mac] = {
            "hci": hci,
            "bus": BUS_LABELS.get(bus, bus if bus and bus != "/" else ""),
            "product": " ".join(x for x in (vendor, product) if x),
        }
    return found


def bus_socket_path(address=None):
    """The filesystem path out of a DBUS_SYSTEM_BUS_ADDRESS value."""
    address = address or os.environ.get(
        "DBUS_SYSTEM_BUS_ADDRESS", "unix:path=/run/dbus/system_bus_socket"
    )
    for part in address.split(","):
        for field in part.split(";"):
            if field.startswith("unix:path="):
                return field[len("unix:path="):]
    return None


def _dbus_send(*args, timeout=10):
    """Run dbus-send; return (ok, combined output). ok is None if unavailable."""
    try:
        proc = subprocess.run(
            ["dbus-send", "--system", "--print-reply"] + list(args),
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None, ""
    text = " ".join(((proc.stderr or "") + " " + (proc.stdout or "")).split())
    return proc.returncode == 0, text


def diagnose_dbus():
    """Explain why the system bus is unusable, or return None if it is fine.

    bluetoothctl 5.82 does not check the result of dbus_bus_get(): when the bus
    refuses it, the NULL connection trips an assertion and the process dumps
    core, so all the wrapper sees is an immediate EOF. That looks exactly like a
    missing socket. Ask D-Bus directly rather than guessing.

    The two probes are chosen to be ones the *default* D-Bus policy permits.
    org.freedesktop.DBus.Peer.Ping is not: system.conf denies method calls by
    default and only punches holes for the DBus, Introspectable, Properties and
    Containers1 interfaces on the bus destination, so pinging Peer is refused on
    a perfectly healthy host -- which an earlier version of this function
    mistook for a real fault.
    """
    path = bus_socket_path()
    if path and not os.path.exists(path):
        return (
            "the D-Bus socket %s is not present inside the container -- mount the "
            "host's system bus with `-v %s:%s`" % (path, path, path)
        )

    # Probe 1: can we register on the bus at all? This is what AppArmor blocks.
    ok, error = _dbus_send(
        "--dest=org.freedesktop.DBus", "/org/freedesktop/DBus",
        "org.freedesktop.DBus.GetId",
    )
    if ok is None:
        return None  # no dbus-send to ask; fall back to the generic message
    if not ok:
        if "AppArmor" in error:
            return (
                "the host's AppArmor policy is blocking this container from "
                "registering on the system bus (the D-Bus \"Hello\" was denied). "
                "The socket is mounted correctly -- this is host policy, not a "
                "mount problem. Docker confines containers with the "
                "`docker-default` profile, which grants no D-Bus rules. Add "
                "`security_opt: [\"apparmor=unconfined\"]` to this service, or "
                "install an AppArmor profile that permits dbus. See the README."
            )
        return "the system D-Bus refused this container: %s" % error[:300]

    # Probe 2: is bluetoothd actually there, and may we talk to it?
    ok, error = _dbus_send(
        "--dest=org.bluez", "/", "org.freedesktop.DBus.Introspectable.Introspect"
    )
    if ok:
        return None
    if "ServiceUnknown" in error or "not provided by any .service" in error:
        return (
            "the system bus is reachable but nothing owns org.bluez -- bluetoothd "
            "is not running on the host (`systemctl status bluetooth`)"
        )
    if "AccessDenied" in error or "Rejected send message" in error:
        return (
            "the host's D-Bus policy denies this container access to org.bluez. "
            "BlueZ usually grants that to root or to a `bluetooth`/`lp` group; "
            "check /etc/dbus-1/system.d/bluetooth.conf on the host. (%s)"
            % error[:200]
        )
    return "cannot reach org.bluez over the system bus: %s" % error[:300]


def strip_ansi(text):
    cleaned = ANSI.sub("", text or "")
    for junk in ("\r", "\x1b", "\x08"):
        cleaned = cleaned.replace(junk, "")
    return cleaned


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
                diagnose_dbus()
                or "bluetoothctl exited immediately without reaching its prompt"
            ) from None
        except pexpect.TIMEOUT:
            child.terminate(force=True)
            raise BluetoothUnavailable(
                diagnose_dbus()
                or "bluetoothctl never reached its prompt -- bluetoothd is "
                "probably not running on the host"
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
                name = names.get(mac) or mac
                devices.append(
                    {
                        "mac": mac,
                        "name": name,
                        # BlueZ falls back to the address with dashes when a
                        # device advertises no name. A busy room is mostly those
                        # -- randomised BLE addresses from phones and watches --
                        # so the UI needs to be able to hide them.
                        "named": _has_name(name, mac),
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

    # ---- adapters (controllers) --------------------------------------------

    def list_adapters(self, lock_timeout=-1):
        """Every controller bluetoothd knows about, plus how it is attached.

        `devices` and every action are scoped to the selected controller, so on
        a box with both an internal adapter and a USB dongle this is what tells
        you which radio you are actually driving.
        """
        with self._locked(lock_timeout):
            out = self._send("list")

            rows = []
            for line in out.splitlines():
                match = CONTROLLER_LINE.match(line.strip())
                if match:
                    rows.append((match.group(1).upper(), match.group(2).strip()))
            hardware = adapter_hardware([mac for mac, _ in rows])

            adapters = []
            for mac, rest in rows:
                selected = rest.endswith("[default]")
                if selected:
                    rest = rest[: -len("[default]")].strip()
                info = hardware.get(mac, {})
                adapters.append(
                    {
                        "mac": mac,
                        "name": rest or info.get("hci") or mac,
                        "selected": selected,
                        "hci": info.get("hci", ""),
                        "bus": info.get("bus", ""),
                        "product": info.get("product", ""),
                        "powered": self._adapter_powered(mac),
                    }
                )
            return adapters

    def _adapter_powered(self, mac):
        try:
            out = self._send("show %s" % mac)
        except BluetoothctlError:
            return None
        for line in out.splitlines():
            if line.strip().startswith("Powered:"):
                return line.split(":", 1)[1].strip() == "yes"
        return None

    def select_adapter(self, mac):
        """Point the session at another controller and power it up."""
        with self._locked(-1):
            # `select` prints nothing on success, so confirm via `list` instead
            # of trying to read a result out of it.
            self._send("select %s" % mac)
            adapters = self.list_adapters()
            chosen = next((a for a in adapters if a["mac"] == mac), None)
            if chosen is None:
                raise BluetoothctlError("no controller %s on this host" % mac)
            if not chosen["selected"]:
                raise BluetoothctlError(
                    "bluetoothctl did not switch to %s" % mac
                )
            try:
                self._send("power on")
            except BluetoothctlError:
                pass
            return adapters

    def reset_adapter(self, probe_seconds=None):
        """Power the controller off and on again, then prove it still discovers.

        The failure this exists for: the controller wedges mid-inquiry and every
        scan comes back empty while everything looks healthy -- adapter powered,
        bluetoothd running, nothing in the panel to see. Observed on a Realtek
        dongle, with the kernel logging "Failed to cancel inquiry -16" and only
        a power cycle bringing discovery back.

        Disconnects anything currently connected, so callers should confirm.
        """
        probe_seconds = PROBE_SECONDS if probe_seconds is None else probe_seconds
        steps = []
        with self._locked(-1):
            try:
                self._send("power off")
                steps.append({"step": "power off", "ok": True, "output": ""})
            except BluetoothctlError as exc:
                # Not fatal: a controller that will not power down may still
                # come back with the power on below.
                steps.append({"step": "power off", "ok": False, "output": str(exc)})
            time.sleep(POWER_SETTLE)

            last = ""
            for attempt in range(1, POWER_ATTEMPTS + 1):
                try:
                    self._send_checked("power on")
                except BluetoothctlError as exc:
                    last = str(exc)
                    time.sleep(POWER_SETTLE)
                    continue
                steps.append({
                    "step": "power on", "ok": True,
                    "output": "" if attempt == 1 else "took %d attempts" % attempt,
                })
                break
            else:
                steps.append({"step": "power on", "ok": False, "output": last})
                raise StepFailure(
                    "the controller refused to power back on (%s). On the host: "
                    "sudo hciconfig %s up, then try again."
                    % (last, self._hci_hint()), steps)

            seen = self._probe_discovery(probe_seconds)
            found = "%d device%s seen in %ds" % (
                seen, "" if seen == 1 else "s", int(probe_seconds))
            steps.append({"step": "discovery probe", "ok": seen > 0, "output": found})
            if seen == 0:
                raise StepFailure(
                    "the controller powered back on but discovered nothing in "
                    "%ds, so it may still be stuck. On the host: sudo hciconfig "
                    "%s up, then reset again."
                    % (int(probe_seconds), self._hci_hint()), steps)
        return steps

    def _hci_hint(self):
        """Best guess at the selected controller's hciN name, for error text."""
        try:
            selected = next(
                (a["mac"] for a in self.list_adapters() if a.get("selected")), None
            )
            return adapter_paths().get(selected, "hci0")
        except Exception:
            return "hci0"

    def _known_macs(self):
        return {device["mac"] for device in self.list_devices()}

    def _probe_discovery(self, seconds):
        """Scan briefly and count what turns up that we did not already know.

        Counting *new* addresses rather than devices in the list is the point:
        a stuck controller still lists everything BlueZ remembers, so a healthy
        looking table proves nothing about whether the radio is hearing.
        """
        before = self._known_macs()
        self._send_checked("scan on")
        try:
            time.sleep(seconds)
        finally:
            try:
                self._send("scan off")
            except BluetoothctlError:
                pass
        return len(self._known_macs() - before)

    # ---- actions -----------------------------------------------------------

    def scan(self, duration):
        with self._locked(-1):
            before = self._known_macs()
            self._send_checked("scan on")
            try:
                time.sleep(duration)
            finally:
                # Always stop the radio scanning, even if the sleep is cut short.
                try:
                    self._send("scan off")
                except BluetoothctlError:
                    pass
            devices = self.list_devices()
            # Re-evaluated every scan: the hint must disappear the moment the
            # radio starts hearing things again.
            self.warnings = [w for w in self.warnings if w != STUCK_HINT]
            found = {device["mac"] for device in devices} - before
            if not found and duration >= STUCK_SCAN_SECONDS:
                self._warn(STUCK_HINT)
            return devices

    def pair(self, mac):
        """Pair, and wait for the verdict rather than the acknowledgement.

        bluetoothctl answers `pair` with "Attempting to pair with ..." and
        reprints its prompt at once; the outcome -- "Pairing successful", or
        "Failed to pair: org.bluez.Error.AuthenticationFailed" when the speaker
        is not in pairing mode -- arrives seconds later as an asynchronous line.
        Stopping at the first prompt reported every failed pairing as a success,
        which is the one case where the truth matters: the panel said pair ok,
        trust ok, connect ok, while BlueZ still had `Paired: no`.
        """
        with self._locked(-1):
            child = self._ensure_started()
            self._drain(child)
            child.sendline("pair %s" % mac)
            try:
                index = child.expect(
                    [PAIR_SUCCESS, PAIR_FAILED, PAIR_UNAVAILABLE],
                    timeout=PAIR_TIMEOUT,
                )
            except pexpect.EOF:
                self._child = None
                raise BluetoothUnavailable(
                    "bluetoothctl exited while pairing with %s" % mac
                ) from None
            except pexpect.TIMEOUT:
                raise BluetoothctlError(
                    "no answer from %s after %ds -- is it in pairing mode?"
                    % (mac, int(PAIR_TIMEOUT))
                ) from None

            answer = strip_ansi(child.after or "").strip()
            match = child.match
            # The verdict is followed by another prompt. Wait for it rather than
            # draining on a timer: if it lands after the drain gives up, the
            # *next* command's expect() matches that stale prompt and returns a
            # truncated answer -- a device list one line short, in practice.
            try:
                child.expect(PROMPT, timeout=POWER_SETTLE)
            except (pexpect.TIMEOUT, pexpect.EOF):
                pass
            self._drain(child)

            if index == 0:
                return answer
            if index == 2:
                raise BluetoothctlError(
                    "%s is not available -- scan for it, then pair" % mac
                )
            why = (match.group("why") if match else "").strip()
            if "AlreadyExists" in why:
                return "already paired"
            raise BluetoothctlError(_pair_hint(why))

    def trust(self, mac):
        return self._send_checked("trust %s" % mac)

    def connect(self, mac):
        return self._send_checked("connect %s" % mac, timeout=CONNECT_TIMEOUT)

    def disconnect(self, mac):
        return self._send_checked("disconnect %s" % mac, timeout=CONNECT_TIMEOUT)

    def remove(self, mac):
        return self._send_checked("remove %s" % mac)

    def reconnect(self, mac):
        """Drop the link and bring it straight back.

        The usual fix for a speaker that is nominally connected but silent, or
        one that came back from standby on a stale link. Disconnect failures are
        ignored: not being connected is a perfectly good starting point.
        """
        steps = []
        with self._locked(-1):
            try:
                output = self.disconnect(mac)
                steps.append({"step": "disconnect", "ok": True, "output": _tail(output)})
            except BluetoothctlError as exc:
                steps.append({"step": "disconnect", "ok": True,
                              "output": "already disconnected (%s)" % exc})
            try:
                output = self.connect(mac)
            except BluetoothctlError as exc:
                steps.append({"step": "connect", "ok": False, "output": str(exc)})
                raise StepFailure(str(exc), steps) from exc
            steps.append({"step": "connect", "ok": True, "output": _tail(output)})
        return steps

    def repair(self, mac):
        """Forget the pairing and establish it again from scratch.

        Destructive on purpose: `remove` drops the link key, so the device has
        to be in pairing mode for the follow-up to succeed. Callers should
        confirm with the user first -- if the peer is not pairing-ready the
        result is an unpaired device.

        `remove` also deletes BlueZ's device object, and `pair` on an object
        that no longer exists just answers "not available". So this rediscovers
        the device before trying to pair, rather than assuming BlueZ still knows
        about something it was explicitly told to forget.
        """
        steps = []
        with self._locked(-1):
            try:
                output = self.remove(mac)
                steps.append({"step": "forget", "ok": True, "output": _tail(output)})
            except BluetoothctlError as exc:
                steps.append({"step": "forget", "ok": True,
                              "output": "not paired (%s)" % exc})

            found = self._rediscover(mac)
            steps.append({
                "step": "rediscover", "ok": found,
                "output": "found again" if found else
                          "not seen in %ds -- is it in pairing mode?" % REDISCOVER_TIMEOUT,
            })
            if not found:
                raise StepFailure(
                    "%s did not reappear after being forgotten -- put it in "
                    "pairing mode and try again" % mac, steps)

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

    def _rediscover(self, mac, timeout=None):
        """Scan until `mac` shows up again, or the budget runs out."""
        timeout = REDISCOVER_TIMEOUT if timeout is None else timeout
        deadline = time.time() + timeout
        try:
            self._send_checked("scan on")
        except BluetoothctlError:
            return False
        try:
            while time.time() < deadline:
                time.sleep(1.0)
                if mac in self._device_macs():
                    return True
        finally:
            try:
                self._send("scan off")
            except BluetoothctlError:
                pass
        return mac in self._device_macs()

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


def _has_name(name, mac):
    return name.strip().upper().replace("-", ":") != mac.upper()


def _pair_hint(why):
    """Turn a BlueZ pairing error into something a person can act on."""
    why = why or "unknown error"
    if "AuthenticationFailed" in why or "AuthenticationCanceled" in why:
        return (
            "%s -- the device refused the bond. Put it in pairing mode (most "
            "speakers want the button held until it flashes) and try again; if "
            "it still refuses, clear its own list of paired devices." % why
        )
    if "AuthenticationTimeout" in why or "ConnectionAttemptFailed" in why:
        return "%s -- the device stopped answering. Wake it and try again." % why
    return why


def _first_failure_line(out):
    for line in strip_ansi(out).splitlines():
        line = line.strip()
        if any(marker in line for marker in FAILURE_MARKERS):
            return line
    return None


def _tail(out, limit=3):
    lines = [l.strip() for l in strip_ansi(out).splitlines() if l.strip()]
    return " / ".join(lines[-limit:])
