"""Supervised snapclient players.

Each player is a long-running `snapclient` child process of this container,
bound to one PipeWire sink -- normally the `bluez_output.*` node that appears
when a paired Bluetooth speaker connects. That binding is the whole point of
running this next to the Bluetooth panel: the sink only exists while the device
is connected, so starting a player means "connect the speaker, wait for its
node, then launch snapclient".

The launch recipe mirrors pipewire-snapclient's entrypoint.sh (same env, same
arguments, same 5s->60s reconnect backoff) so players behave identically to the
snapclient containers that image produces. The difference is that N of them live
in one process tree here, each with its own environment -- which is what makes
per-player PIPEWIRE_NODE work without one container per speaker.

Trade-off worth knowing: restarting this container stops every player. The
snapclient-per-container approach survives a panel restart; this one does not.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque

import snapctl

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "players.json")

SNAPCLIENT = os.environ.get("SNAPCLIENT", "snapclient")
PW_DUMP = os.environ.get("PW_DUMP", "pw-dump")
WPCTL = os.environ.get("WPCTL", "wpctl")

# Same backoff shape as entrypoint.sh: a session that stayed up for a while is
# not part of a failure streak, so the delay resets rather than carrying a 60s
# penalty over from an outage that is long since fixed.
RETRY_START = 5.0
RETRY_MAX = 60.0
HEALTHY_AFTER = 60.0

# How long to wait for a sink to appear after connecting its Bluetooth device.
NODE_WAIT_SECONDS = 20.0

# Watchdog. snapclient does NOT exit when its output sink disappears -- switch a
# Bluetooth speaker off mid-stream and the process sits there happily, having
# quietly closed ALSA, so a supervisor that only watches for process exit reports
# a healthy player that cannot make a sound and never recovers. Verified on real
# hardware. So while a player is running we also watch its sink, and restart it
# once the sink has been gone long enough to not be a momentary blip -- the
# restart re-runs the readiness step, which reconnects the Bluetooth device.
HEALTH_INTERVAL = 3.0
SINK_GRACE = 15.0

LOG_LINES = 200

# Any printable text, up to 64 characters. Deliberately permissive: names are
# passed to snapclient as a single argv element, never through a shell, so the
# pattern is not a security boundary -- it only keeps control characters (which
# would corrupt the log stream and the JSON config) out. An ASCII-only rule
# rejected perfectly reasonable names like "BT · Kitchen" and anything Cyrillic.
NAME_RE = re.compile(r"[^\x00-\x1f\x7f]{1,64}")
NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_-]{0,255}$")
LATENCY_RE = re.compile(r"^\d{1,7}/\d{1,7}$")

def _env_int(name, fallback):
    try:
        return int(os.environ.get(name) or fallback)
    except ValueError:
        return fallback


# Defaults for a newly created player. Every one is overridable per player in
# the UI; these just decide what the Add-player form starts with, so a host with
# a snapserver somewhere else only has to be told once.
SNAPSERVER_HOST = os.environ.get("SNAPSERVER_HOST", "")
SNAPSERVER_PORT = _env_int("SNAPSERVER_PORT", 1704)
SNAPSERVER_CONTROL_PORT = _env_int("SNAPSERVER_CONTROL_PORT", snapctl.DEFAULT_CONTROL_PORT)
# Snapserver's own web UI (snapweb). Not used by the panel, just linked to:
# it owns groups, stream assignment and every client on the server, which is
# deliberately more than this panel tries to be.
SNAPSERVER_WEB_PORT = _env_int("SNAPSERVER_WEB_PORT", 1780)

DEFAULTS = {
    "name": "",
    "mac": "",
    "node": "",
    "server": SNAPSERVER_HOST or "127.0.0.1",
    "port": SNAPSERVER_PORT,
    "use_alsa": True,
    "pipewire_latency": "1024/48000",
    "latency_ms": 0,
    "volume": 1.0,
    "autostart": True,
    "extra": "",
    # Snapserver's JSON-RPC port. Audio is 1704, control is 1705; they are
    # separate listeners, so this is not derived from `port`.
    "control_port": SNAPSERVER_CONTROL_PORT,
}


# Panel-wide settings, editable from the web and stored next to the players.
# The environment only seeds them: once saved, the stored value wins, so a host
# can be re-pointed at another snapserver without touching compose. The
# Bluetooth marker lives here for the same reason -- "(BT)" is a starting value,
# not something baked into the code.
def _default_settings():
    return {
        "bt_name_template": "{name} (BT)",
        "snapserver_host": SNAPSERVER_HOST,
        "snapserver_port": SNAPSERVER_PORT,
        "snapserver_control_port": SNAPSERVER_CONTROL_PORT,
        "snapserver_web_port": SNAPSERVER_WEB_PORT,
    }


class SettingsError(ValueError):
    status = 400


def validate_settings(patch, current=None):
    defaults = _default_settings()
    clean = dict(current or defaults)
    for key, value in (patch or {}).items():
        if key not in defaults:
            raise SettingsError("unknown setting %r" % key)
        clean[key] = value

    host = str(clean.get("snapserver_host", "")).strip()
    if host and not HOST_RE.fullmatch(host):
        raise SettingsError("invalid snapserver address")
    clean["snapserver_host"] = host

    for key, label in (("snapserver_port", "port"),
                       ("snapserver_control_port", "control port"),
                       ("snapserver_web_port", "web port")):
        try:
            clean[key] = int(clean[key])
        except (TypeError, ValueError):
            raise SettingsError("%s must be a number" % label) from None
        if not 1 <= clean[key] <= 65535:
            raise SettingsError("%s must be between 1 and 65535" % label)

    template = str(clean["bt_name_template"]).strip()
    if "{name}" not in template:
        raise SettingsError("the template must contain {name}")
    if len(template) > 80:
        raise SettingsError("the template is too long")
    # It has to still produce a legal player name once filled in.
    sample = template.format(name="Speaker")
    if not NAME_RE.fullmatch(sample):
        raise SettingsError(
            "the template produces an invalid name (%r) -- it must be 1-64 "
            "printable characters" % sample
        )
    clean["bt_name_template"] = template
    return clean


class PlayerError(ValueError):
    """A player definition was rejected."""

    status = 400


def node_for_mac(mac):
    """The PipeWire node a connected Bluetooth sink shows up as.

    BlueZ/PipeWire name it after the address with underscores, e.g.
    00:02:5B:00:FF:04 -> bluez_output.00_02_5B_00_FF_04.1 -- which is exactly
    the value the README used to make you dig out of `pw-cli ls Node`.
    """
    return "bluez_output.%s.1" % mac.upper().replace(":", "_")


def _pw_env():
    env = dict(os.environ)
    env.setdefault("PIPEWIRE_RUNTIME_DIR", "/tmp")
    env.setdefault("PIPEWIRE_REMOTE", "pipewire-0")
    return env


def list_sinks():
    """Audio sinks PipeWire currently exposes, newest state each call.

    Best effort: no PipeWire socket means no sinks, not an error -- the panel
    still lists players, it just cannot pre-validate their nodes.
    """
    if not shutil.which(PW_DUMP):
        return []
    try:
        raw = subprocess.run(
            [PW_DUMP], capture_output=True, text=True, timeout=10, env=_pw_env()
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if raw.returncode != 0:
        return []
    try:
        objects = json.loads(raw.stdout or "[]")
    except ValueError:
        return []

    sinks = []
    for item in objects:
        if not isinstance(item, dict):
            continue
        props = (item.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Audio/Sink":
            continue
        name = props.get("node.name")
        if not name:
            continue
        sinks.append(
            {
                "id": item.get("id"),
                "node": name,
                "description": props.get("node.description") or name,
                "bluetooth": name.startswith("bluez_output."),
            }
        )
    sinks.sort(key=lambda s: s["node"])
    return sinks


def sink_present(node):
    return any(s["node"] == node for s in list_sinks())


def set_sink_volume(node, volume):
    """Unmute and set a sink's volume, matching what entrypoint.sh does once."""
    if not shutil.which(WPCTL):
        return
    target = next((s for s in list_sinks() if s["node"] == node), None)
    if target is None or target.get("id") is None:
        return
    for args in (
        [WPCTL, "set-mute", str(target["id"]), "0"],
        [WPCTL, "set-volume", str(target["id"]), "%.2f" % volume],
    ):
        try:
            subprocess.run(args, capture_output=True, timeout=10, env=_pw_env())
        except (OSError, subprocess.SubprocessError):
            return


def validate(config, existing_names=()):
    """Normalise and check a player definition coming from the browser.

    Every value here ends up in a subprocess argument list or an environment
    variable. argv is passed as a list so there is no shell to inject into, but
    the patterns still keep obvious nonsense out of the config file and give the
    user a real error instead of a snapclient that dies on startup.
    """
    clean = dict(DEFAULTS)
    clean.update({k: v for k, v in (config or {}).items() if k in DEFAULTS})

    clean["name"] = str(clean["name"]).strip()
    if not NAME_RE.fullmatch(clean["name"]):
        raise PlayerError(
            "name must be 1-64 printable characters (no control characters)"
        )
    if clean["name"] in existing_names:
        raise PlayerError("a player named %r already exists" % clean["name"])

    clean["mac"] = str(clean["mac"]).strip().upper()
    if clean["mac"] and not re.fullmatch(
        r"[0-9A-F]{2}(?::[0-9A-F]{2}){5}", clean["mac"]
    ):
        raise PlayerError("invalid MAC address")

    clean["node"] = str(clean["node"]).strip()
    if not clean["node"] and clean["mac"]:
        clean["node"] = node_for_mac(clean["mac"])
    if clean["node"] and not NODE_RE.match(clean["node"]):
        raise PlayerError("invalid PipeWire node name")

    clean["server"] = str(clean["server"]).strip()
    if not HOST_RE.match(clean["server"]):
        raise PlayerError("invalid server address")

    try:
        clean["port"] = int(clean["port"])
    except (TypeError, ValueError):
        raise PlayerError("port must be a number") from None
    if not 1 <= clean["port"] <= 65535:
        raise PlayerError("port must be between 1 and 65535")

    try:
        clean["control_port"] = int(clean["control_port"])
    except (TypeError, ValueError):
        raise PlayerError("control port must be a number") from None
    if not 1 <= clean["control_port"] <= 65535:
        raise PlayerError("control port must be between 1 and 65535")

    try:
        clean["latency_ms"] = int(clean["latency_ms"])
    except (TypeError, ValueError):
        raise PlayerError("latency must be a whole number of milliseconds") from None
    if not -2000 <= clean["latency_ms"] <= 2000:
        raise PlayerError("latency must be between -2000 and 2000 ms")

    try:
        clean["volume"] = float(clean["volume"])
    except (TypeError, ValueError):
        raise PlayerError("volume must be a number") from None
    if not 0.0 <= clean["volume"] <= 1.0:
        raise PlayerError("volume must be between 0.0 and 1.0")

    clean["pipewire_latency"] = str(clean["pipewire_latency"]).strip()
    if clean["pipewire_latency"] and not LATENCY_RE.match(clean["pipewire_latency"]):
        raise PlayerError("PipeWire latency must look like 1024/48000")

    clean["use_alsa"] = bool(clean["use_alsa"])
    clean["autostart"] = bool(clean["autostart"])

    # SNAP_EXTRA is split with shlex and appended to argv, never shell-evaluated.
    clean["extra"] = str(clean["extra"]).strip()
    if len(clean["extra"]) > 200:
        raise PlayerError("extra arguments are too long")

    return clean


class Player:
    """One supervised snapclient process."""

    def __init__(self, config, supervisor):
        self.config = config
        self.id = config["id"]
        self._supervisor = supervisor
        self._proc = None
        self._thread = None
        self._wake = threading.Event()   # interrupts the backoff sleep
        self._lock = threading.RLock()

        self.desired = False
        self.named_on_server = False
        self.state = "stopped"
        self.detail = ""
        self.started_at = None
        self.restarts = 0
        self.last_exit = None
        self.logs = deque(maxlen=LOG_LINES)

    # ---- reporting ---------------------------------------------------------

    @property
    def client_id(self):
        return snapctl.client_id_for(self.config["name"], self.config.get("instance", 1))

    def status(self):
        with self._lock:
            return {
                "client_id": self.client_id,
                **self.config,
                "state": self.state,
                "detail": self.detail,
                "running": self.state == "running",
                "uptime": (time.time() - self.started_at) if self.started_at else 0,
                "restarts": self.restarts,
                "last_exit": self.last_exit,
                "node_present": None,  # filled in by the supervisor, which batches
            }

    def log(self, line):
        self.logs.append("%s %s" % (time.strftime("%H:%M:%S"), line.rstrip()))

    # ---- lifecycle ---------------------------------------------------------

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                self.desired = True
                self._wake.set()  # cut short a backoff sleep
                return
            self.desired = True
            self.state = "starting"
            self.detail = ""
            self._wake.clear()
            self._thread = threading.Thread(
                target=self._supervise, name="player-%s" % self.id, daemon=True
            )
            self._thread.start()

    def stop(self, timeout=10.0):
        with self._lock:
            self.desired = False
            proc = self._proc
            thread = self._thread
        self._wake.set()
        if proc is not None:
            self._terminate(proc)
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                # Belt and braces: the lock above should make this unreachable,
                # but never leave a live snapclient behind.
                with self._lock:
                    proc = self._proc
                if proc is not None:
                    self._terminate(proc)
                thread.join(timeout=timeout)
        with self._lock:
            self.state = "stopped"
            self.detail = ""
            self.started_at = None

    def _terminate(self, proc):
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass

    # ---- the supervisor loop -----------------------------------------------

    def _supervise(self):
        delay = RETRY_START
        while self.desired:
            ready, why = self._prepare()
            if not ready:
                with self._lock:
                    self.state = "waiting"
                    self.detail = why
                self.log("not ready: %s" % why)
                if self._sleep(delay):
                    break
                delay = min(delay * 2, RETRY_MAX)
                continue

            started = time.time()
            proc = None
            # Deciding to launch and recording the child must be atomic against
            # stop(). Otherwise a stop that lands in between reads self._proc as
            # None, never terminates the process that is about to exist, and
            # leaves an orphaned snapclient holding the sink for good.
            with self._lock:
                if not self.desired:
                    break
                try:
                    proc = self._spawn()
                except OSError as exc:
                    self.state = "failed"
                    self.detail = "cannot start snapclient: %s" % exc
                else:
                    self._proc = proc
                    self.state = "running"
                    self.detail = ""
                    self.started_at = started

            if proc is None:
                self.log(self.detail)
                if self._sleep(delay):
                    break
                delay = min(delay * 2, RETRY_MAX)
                continue

            if self.config.get("volume") is not None and self.config.get("node"):
                set_sink_volume(self.config["node"], self.config["volume"])

            # The output pump has to run alongside the watchdog, not instead
            # of it: reading the child's stdout blocks until the child exits.
            reader = threading.Thread(
                target=self._pump, args=(proc,),
                name="player-%s-log" % self.id, daemon=True,
            )
            reader.start()
            self._watch(proc)
            code = proc.wait()
            reader.join(timeout=2)

            with self._lock:
                self._proc = None
                self.started_at = None
                self.last_exit = code
            self.log("snapclient exited with code %s" % code)

            if not self.desired:
                break

            with self._lock:
                self.restarts += 1
            # A session that stayed up is not a failure streak.
            if time.time() - started >= HEALTHY_AFTER:
                delay = RETRY_START
            with self._lock:
                self.state = "backoff"
                self.detail = "restarting in %ds" % int(delay)
            if self._sleep(delay):
                break
            delay = min(delay * 2, RETRY_MAX)

        with self._lock:
            if not self.desired:
                self.state = "stopped"
                self.detail = ""

    def _watch(self, proc):
        """Wait for the child to exit, restarting it if its sink goes away."""
        node = self.config.get("node")
        absent_since = None
        while proc.poll() is None:
            if not self.desired:
                return
            if node and not sink_present(node):
                absent_since = absent_since or time.time()
                gone = time.time() - absent_since
                if gone >= SINK_GRACE:
                    self.log(
                        "sink %s has been gone %ds -- restarting the player"
                        % (node, int(gone))
                    )
                    with self._lock:
                        self.state = "waiting"
                        self.detail = "output sink disappeared"
                    self._terminate(proc)
                    return
                with self._lock:
                    self.detail = "sink missing for %ds" % int(gone)
            elif absent_since is not None:
                absent_since = None
                self.log("sink %s is back" % node)
                with self._lock:
                    self.detail = ""
            self._wake.wait(timeout=HEALTH_INTERVAL)
            if not self.desired:
                return
            self._wake.clear()

    def _sleep(self, seconds):
        """Interruptible backoff. Returns True if we were told to stop."""
        self._wake.wait(timeout=seconds)
        self._wake.clear()
        return not self.desired

    def _prepare(self):
        """Connect the Bluetooth device and wait for its sink to show up."""
        mac = self.config.get("mac")
        node = self.config.get("node")

        if mac and self._supervisor.connect_bluetooth:
            try:
                self._supervisor.connect_bluetooth(mac)
            except Exception as exc:  # the panel's own error type, kept loose
                self.log("bluetooth connect failed: %s" % exc)

        if not node:
            return True, ""

        deadline = time.time() + NODE_WAIT_SECONDS
        while time.time() < deadline:
            if sink_present(node):
                return True, ""
            if not self.desired:
                return False, "stopped"
            time.sleep(1.0)

        if not shutil.which(PW_DUMP):
            # No way to check; let snapclient try and report for itself.
            return True, ""
        return False, "sink %s is not present (is the device connected?)" % node

    def _spawn(self):
        import shlex

        cfg = self.config
        env = _pw_env()
        if cfg.get("node"):
            env["PIPEWIRE_NODE"] = cfg["node"]
        if cfg.get("pipewire_latency"):
            env["PIPEWIRE_LATENCY"] = cfg["pipewire_latency"]

        args = [SNAPCLIENT, "--hostID", cfg["name"], "--instance", str(cfg["instance"])]
        if cfg.get("use_alsa"):
            # pcm.default is mapped to PipeWire in /etc/asound.conf; this is the
            # path that copes with a sink changing sample rate under it.
            args += ["--player", "alsa", "-s", "default"]
        else:
            args += ["--player", "pipewire"]
        if cfg.get("latency_ms"):
            args += ["--latency", str(cfg["latency_ms"])]
        if cfg.get("extra"):
            args += shlex.split(cfg["extra"])
        args.append("tcp://%s:%d" % (cfg["server"], cfg["port"]))

        self.log("launching: %s" % " ".join(args))
        return subprocess.Popen(
            args,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    def _pump(self, proc):
        """Drain the child's output into the ring buffer until it exits."""
        if proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                self.log(line)
        except (OSError, ValueError):
            pass


class Supervisor:
    def __init__(self, connect_bluetooth=None, config_path=CONFIG_PATH):
        self._players = {}
        self._lock = threading.RLock()
        self.config_path = config_path
        # Injected so players.py never imports the Bluetooth layer; keeps this
        # module testable with a plain lambda.
        self.connect_bluetooth = connect_bluetooth
        self.settings = _default_settings()
        self.load()

    # ---- persistence -------------------------------------------------------

    def load(self):
        try:
            with open(self.config_path) as handle:
                stored = json.load(handle)
        except (OSError, ValueError):
            return
        try:
            self.settings = validate_settings(stored.get("settings") or {})
        except SettingsError:
            self.settings = _default_settings()
        for entry in stored.get("players", []):
            try:
                config = validate(entry)
            except PlayerError:
                continue
            config["id"] = entry.get("id") or uuid.uuid4().hex[:8]
            config["instance"] = entry.get("instance") or self._next_instance()
            with self._lock:
                self._players[config["id"]] = Player(config, self)

    def save(self):
        with self._lock:
            payload = {
                "settings": self.settings,
                "players": [p.config for p in self._players.values()],
            }
        try:
            os.makedirs(os.path.dirname(self.config_path) or ".", exist_ok=True)
            tmp = self.config_path + ".tmp"
            with open(tmp, "w") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp, self.config_path)  # atomic: never a half-written config
        except OSError:
            pass

    def update_settings(self, patch):
        with self._lock:
            self.settings = validate_settings(patch, self.settings)
        self.save()
        return self.settings

    def suggest_name(self, base, bluetooth):
        """What the Add-player form should prefill for this output."""
        base = (base or "").strip()
        if not bluetooth or not base:
            return base
        return self.settings["bt_name_template"].format(name=base)

    # ---- CRUD --------------------------------------------------------------

    def _next_instance(self):
        used = {p.config.get("instance", 1) for p in self._players.values()}
        candidate = 1
        while candidate in used:
            candidate += 1
        return candidate

    def list(self, with_snapcast=True):
        sinks = {s["node"] for s in list_sinks()}
        with self._lock:
            players = list(self._players.values())

        out = []
        for player in players:
            status = player.status()
            status["node_present"] = (
                status["node"] in sinks if status["node"] else None
            )
            status["snapcast"] = None
            status["snapcast_error"] = None
            out.append(status)

        if with_snapcast:
            self._attach_snapcast(players, out)

        out.sort(key=lambda p: p["name"].lower())
        return out

    def _attach_snapcast(self, players, statuses):
        """Fold in what the Snapserver knows: now playing, volume, capabilities.

        One Server.GetStatus per distinct server covers every player, and the
        result is briefly cached, so a 5s UI poll costs one short-lived socket
        rather than one per player.
        """
        for player, status in zip(players, statuses):
            host = player.config.get("server")
            port = player.config.get("control_port", snapctl.DEFAULT_CONTROL_PORT)
            if not host:
                continue
            try:
                info = snapctl.describe(host, port, player.client_id)
            except snapctl.SnapcastError as exc:
                status["snapcast_error"] = str(exc)
                continue
            status["snapcast"] = info
            if info is None:
                continue
            # snapclient's --hostID sets the id, not the display name, so
            # Snapcast falls back to this container's hostname for every player
            # in here. Name it once, the first time we see it connected.
            if (
                info["connected"]
                and not player.named_on_server
                and info["name"] != player.config["name"]
            ):
                try:
                    snapctl.set_name(host, port, player.client_id, player.config["name"])
                    player.named_on_server = True
                    player.log("named this client %r on the snapserver"
                               % player.config["name"])
                    status["snapcast"]["name"] = player.config["name"]
                except snapctl.SnapcastError as exc:
                    player.log("could not set the snapserver name: %s" % exc)

    def get(self, player_id):
        with self._lock:
            player = self._players.get(player_id)
        if player is None:
            raise PlayerError("no such player")
        return player

    def new_player_defaults(self):
        """Seed values for a new player, from the live settings."""
        return {
            "server": self.settings["snapserver_host"] or DEFAULTS["server"],
            "port": self.settings["snapserver_port"],
            "control_port": self.settings["snapserver_control_port"],
        }

    def create(self, config):
        with self._lock:
            names = {p.config["name"] for p in self._players.values()}
            seeded = {**self.new_player_defaults(), **(config or {})}
            clean = validate(seeded, existing_names=names)
            clean["id"] = uuid.uuid4().hex[:8]
            clean["instance"] = self._next_instance()
            player = Player(clean, self)
            self._players[clean["id"]] = player
        self.save()
        if clean["autostart"]:
            player.start()
        return player

    def update(self, player_id, config):
        player = self.get(player_id)
        with self._lock:
            names = {
                p.config["name"]
                for p in self._players.values()
                if p.id != player_id
            }
            clean = validate({**player.config, **(config or {})}, existing_names=names)
            clean["id"] = player.id
            clean["instance"] = player.config["instance"]
        was_running = player.state != "stopped"
        player.stop()
        with self._lock:
            player.config = clean
            player.named_on_server = False   # re-apply under the new name
        self.save()
        if was_running:
            player.start()
        return player

    def delete(self, player_id):
        player = self.get(player_id)
        player.stop()
        with self._lock:
            self._players.pop(player_id, None)
        self.save()

    # ---- bulk --------------------------------------------------------------

    def autostart(self):
        with self._lock:
            players = list(self._players.values())
        for player in players:
            if player.config.get("autostart"):
                player.start()

    def stop_all(self):
        with self._lock:
            players = list(self._players.values())
        for player in players:
            player.stop(timeout=5)
