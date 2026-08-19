# bluetooth-web

[![Build_Push_Scan](https://github.com/shuricksumy/bluetooth-web/actions/workflows/build.yml/badge.svg)](https://github.com/shuricksumy/bluetooth-web/actions/workflows/build.yml)

A web admin panel for Bluetooth audio on a Linux host: pair a speaker from the
browser, then run a **Snapcast player** against it — no SSH, no `bluetoothctl`,
no hand-copying `bluez_output.*` node names into a compose file.

Built as a companion to [pipewire-snapclient](https://github.com/shuricksumy/pipewire-snapclient):
pair a Bluetooth sink here, then feed its `bluez_output.*` node name to that
container's `PIPEWIRE_NODE`. It is useful on its own on any Linux box running
BlueZ, though.

```mermaid
flowchart LR
    B["🌐 browser<br/>:8088"] -- HTTP --> W["<b>bluetooth-web</b><br/>Flask + pexpect"]
    W -- "bluetoothctl<br/>(D-Bus client)" --> S(["/run/dbus/<br/>system_bus_socket"])
    S -- "system D-Bus" --> D["host bluetoothd"]
    D -- "the actual radio" --> SPK["🎧 speaker · DAC · headphones"]

    style W stroke-width:3px
```

The container holds **no** Bluetooth hardware access. It runs `bluetoothctl` as a
plain D-Bus client against the host's daemon, so the host keeps owning the radio.

## Features

- **Scan / pair / trust / connect / disconnect / remove**, all from one page.
- **Quick pair** — one button runs `pair` → `trust` → `connect`. Trust matters for
  audio sinks: without it BlueZ refuses the speaker's own reconnect when you next
  power it on.
- **No hardware privileges.** One bind-mounted D-Bus socket. No `--privileged`,
  no `cap_add`, no `network_mode: host`, no `/dev` passthrough.
- **Cheap device listing.** Uses BlueZ 5.65+'s filtered `devices Paired` /
  `Connected` / `Trusted` subcommands, so the whole table costs 4 `bluetoothctl`
  round trips rather than one `info <mac>` per device.
- **No build step, no pip.** Vanilla JS in a single static page; Flask and pexpect
  come from Debian packages, so security updates arrive with a plain rebuild.
- **Snapcast players, created and supervised from the web.** Each player is a
  `snapclient` process bound to one PipeWire sink, with start/stop/restart,
  live state, log tail and a 5s→60s reconnect backoff. Definitions persist in
  `/config/players.json`.
- **The node name is derived for you.** A paired speaker's sink is
  `bluez_output.<MAC with underscores>.1`, so the Add-player form prefills it —
  that is the `pw-cli ls Node | grep` step gone.
- **Optional HTTP Basic Auth** via `ADMIN_PASSWORD`.
- **Degrades honestly.** No adapter, no bluetoothd, no D-Bus socket or an
  AppArmor denial each produce a specific message in the UI, not a spinner and
  not a 500. On startup failure it asks D-Bus directly what went wrong rather
  than guessing from `bluetoothctl`'s core dump.

## 🚀 Quick start

```bash
docker run -d --name bluetooth-web \
  -p 8088:8080 \
  -v /run/dbus/system_bus_socket:/run/dbus/system_bus_socket \
  -e ADMIN_PASSWORD=changeme \
  --restart unless-stopped \
  ghcr.io/shuricksumy/bluetooth-web:latest
```

Or with Compose — [`docker-compose-example.yaml`](docker-compose-example.yaml)
(published image) and [`docker-compose.yml`](docker-compose.yml) (local build):

```yaml
services:
  bluetooth-web:
    image: ghcr.io/shuricksumy/bluetooth-web:latest
    container_name: bluetooth-web
    ports:
      - "8088:8080"
    volumes:
      - /run/dbus/system_bus_socket:/run/dbus/system_bus_socket
    environment:
      - ADMIN_PASSWORD=changeme   # empty or unset disables auth entirely
    restart: unless-stopped
```

Then open `http://<host>:8088`.

### Host requirements

BlueZ has to be installed and running on the **host** — this container only talks
to it:

```bash
sudo apt-get update && sudo apt-get install -y bluetooth bluez bluez-tools
sudo systemctl enable --now bluetooth
systemctl is-active bluetooth          # -> active
ls -l /run/dbus/system_bus_socket      # the socket the container mounts
bluetoothctl show                      # should print a controller, not "No default controller"
```

BlueZ **5.65 or newer** is what the filtered `devices` subcommands need
(Debian 12+, Ubuntu 22.10+). On anything older the table still lists devices, but
the Paired/Connected/Trusted badges stay blank and the panel says so.

On **Ubuntu** you will also need `security_opt: [apparmor=unconfined]` — see
[AppArmor hosts](#️-apparmor-hosts-ubuntu-the-socket-is-not-enough).

## ⚙️ Configuration

| Variable | Default | Description |
| :-- | :-- | :-- |
| `ADMIN_PASSWORD` | _(empty)_ | Enables HTTP Basic Auth on **every** route when set. Empty means no auth at all. |
| `ADMIN_USER` | `admin` | Username for Basic Auth. Ignored unless `ADMIN_PASSWORD` is set. |
| `PORT` | `8080` | Port inside the container. |
| `BIND_HOST` | `0.0.0.0` | Listen address inside the container. |
| `DBUS_SYSTEM_BUS_ADDRESS` | `unix:path=/run/dbus/system_bus_socket` | Where libdbus looks for the host bus. Change only if you mount the socket elsewhere. |
| `SNAPSERVER_HOST` | _(empty)_ | Default Snapserver for new players. Empty means "whatever host you browsed the panel on". |

| `SNAPSERVER_PORT` | `1704` | Default audio port. |
| `SNAPSERVER_CONTROL_PORT` | `1705` | JSON-RPC port used for now-playing, transport and naming. A separate listener from the audio port, not derived from it. |
| `SNAPSERVER_WEB_PORT` | `1780` | Snapweb, linked from each player row. |
| `POLL_SECONDS` | `5` | How often the browser re-reads state. |
| `CONFIG_DIR` | `/config` | Where `players.json` lives. |
| `DEBUG` | `false` | `true` raises the log level to DEBUG. |
| `BLUETOOTHCTL` | `bluetoothctl` | Binary to drive. Only useful for testing. |

## 🔌 The D-Bus mount

```yaml
volumes:
  - /run/dbus/system_bus_socket:/run/dbus/system_bus_socket
```

This is the whole host interface. Two things worth knowing:

- **It must be read-write.** D-Bus is a bidirectional socket; adding `:ro` stops
  the client from writing method calls and the panel never connects.
- **It is not `/var/run`.** On Debian and Ubuntu `/var/run` is a symlink to `/run`,
  so both paths work on the host side, but the container path above is the one
  `DBUS_SYSTEM_BUS_ADDRESS` points at.

Nothing else is needed. In particular this image deliberately does **not** use
`--privileged`, `--cap-add`, `network_mode: host` or `/dev/*` passthrough: the
container never touches the adapter, it asks the host's `bluetoothd` to.

### ⚠️ AppArmor hosts (Ubuntu): the socket is not enough

On a host with AppArmor enforcing — Ubuntu 24.04 out of the box — mounting the
socket correctly is **still not enough**. Docker confines containers with the
`docker-default` profile, which grants no D-Bus rules, and the kernel's D-Bus
mediation (`acquire send receive`) then denies the container's very first
message:

```
An AppArmor policy prevents this sender from sending this message to this
recipient; member="Hello" destination="org.freedesktop.DBus"
```

Without the `Hello` the container can never register on the bus. Worse,
`bluetoothctl` 5.82 does not check the result of `dbus_bus_get()`, so the NULL
connection trips an assertion and the process **dumps core** — it does not print
a useful error. (It crashes the same way on `bluetoothctl --help`.) The panel
detects this case explicitly and says so instead of blaming the mount.

The blunt fix, and the usual one for D-Bus containers:

```yaml
services:
  bluetooth-web:
    security_opt:
      - apparmor=unconfined
```

That drops only the AppArmor profile. Seccomp, capabilities, namespaces and the
read-only root filesystem of the host are all untouched, and the container still
has nothing but the one socket. It is a much smaller step than `--privileged`.

The targeted alternative is a custom profile that starts from `docker-default`
and adds D-Bus rules, loaded with `apparmor_parser -r -W /etc/apparmor.d/<name>`
and selected with `security_opt: [apparmor=<name>]`. Better posture, more
moving parts.

**Debian, Alpine, or any host without AppArmor enforcement needs none of this.**
Check with:

```bash
cat /sys/module/apparmor/parameters/enabled     # Y means it is enforcing
docker inspect <container> --format '{{.AppArmorProfile}}'
```

## 🔒 Security

Two deliberate trade-offs, both worth understanding before you expose this.

### It runs as root

Unlike the sibling snapclient image, this container runs as root. BlueZ's D-Bus
policy (`/etc/dbus-1/system.d/bluetooth.conf`) grants the `org.bluez` methods this
panel needs — `Pair`, `Trust`, `Remove` — to uid 0, and on some distributions to a
`bluetooth` or `lp` group whose **gid differs per host**. Matching that gid from
inside a container means either hardcoding a number that is wrong elsewhere or
asking every user to look theirs up.

For a small admin sidecar on a trusted LAN, root in a container whose only host
access is one socket is the better trade. It is still unprivileged in the Docker
sense — no extra capabilities, no device nodes, no host namespaces.

### Auth is off by default

With `ADMIN_PASSWORD` unset, **every route is open to anyone who can reach the
port** — they can unpair your speakers or pair their own device to your host. That
default exists so a first run on a home LAN just works, and the app logs a warning
at startup when it applies.

- Set `ADMIN_PASSWORD` to turn on HTTP Basic Auth over every route, the static page
  included.
- Basic Auth sends credentials base64-encoded, not encrypted. On anything but a
  trusted LAN, put it behind a reverse proxy with TLS.
- Do not port-forward this to the internet.

MAC addresses arriving from the browser are validated against
`^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$` before they reach `bluetoothctl`'s stdin.
That check is load-bearing rather than cosmetic: the backend drives one long-lived
REPL that takes one command per line, so a MAC carrying an embedded newline would
let a caller append arbitrary `bluetoothctl` commands to the session. Anything that
is not exactly a MAC is rejected with a 400.

## 🔊 Snapcast players

Pair a speaker on the **Devices** tab, then switch to **Players** → *Add player*.
Pick the device, and the form fills in the rest:

| Field | Default | Why |
| :-- | :-- | :-- |
| Output | the paired device | Determines `PIPEWIRE_NODE`; Bluetooth devices are listed first |
| Name | the device's name, marked as Bluetooth | `--hostID`, i.e. what Snapcast and Music Assistant show. The marker comes from a template you edit under ⚙ — `{name} (BT)` by default, `{name}` to drop it |
| Snapserver | the host you are browsing | Usually correct: the panel and the server are typically the same box |
| Port | `1704` | Snapcast's stream port |
| Latency (ms) | `0` | **Sync offset — see below** |
| PipeWire buffer | `1024/48000` for BT, `2048/192000` otherwise | quantum/rate |
| ALSA bridge | on | `--player alsa -s default`; copes with a sink changing rate under it |

Starting a player **connects its Bluetooth device first** and waits up to 20 s for
the sink to appear, because a `bluez_output` node only exists while the speaker is
connected. If it never shows up the player sits in `waiting` and says so rather
than thrashing.

### Now playing and transport control

Players show what is playing, pulled from the Snapserver's JSON-RPC port — title,
artist, album art, plus volume and mute. Where the stream supports it you also get
**play / pause / next / previous**.

Those buttons are drawn from the stream's own `canControl` / `canPause` /
`canGoNext` / `canGoPrevious` flags, which differ per stream and change at
runtime: a Music Assistant stream typically reports all four, a plain pipe stream
reports none and gets no buttons. Snapcast has no "stop" — pause is the stop.

The panel also sets each client's **name** on the server. `snapclient --hostID`
only sets the client *id*; the display name starts empty, so Snapcast and Music
Assistant fall back to the hostname — which is this container's, identical for
every player running in here. Without that call all your players show up under
one meaningless name.

For anything beyond a single player — groups, stream assignment, clients this
panel did not create — the header links to **Snapweb** on the server itself. A
row gets its own link only when that player points at a different server.

The Snapserver address and its three ports are set under **⚙** as well as by
environment variable: the environment seeds them, the saved settings win
afterwards, so a host can be re-pointed without touching compose. The panel
opens on the Players tab and remembers a light/dark/system theme choice.

### ⚠️ Bluetooth latency

A2DP adds roughly **150–250 ms**. Against wired rooms a Bluetooth speaker will be
audibly late until you compensate, so set a **negative** *Latency (ms)* — start
around `-180` and tune by ear. This is the single thing most likely to make a new
BT player sound wrong, and it is not a bug in the player.

### What this costs you

Players are children of this container, so **restarting the panel stops every
player**. If you want audio that survives a panel restart, run those players as
separate [pipewire-snapclient](https://github.com/shuricksumy/pipewire-snapclient)
containers instead — this panel deliberately leaves containers it did not create
alone.

Players need three mounts the Bluetooth half does not: the host's PipeWire socket,
`/dev/shm`, and a `/config` volume for `players.json`. Drop them if you only want
device pairing.

## 🧩 API

Every route returns JSON. `<mac>` must match the pattern above or the request is
rejected with `400 {"error": "invalid MAC address"}`.

| Method | Route | Body | Description |
| :-- | :-- | :-- | :-- |
| `GET` | `/api/devices` | — | The device table. |
| `POST` | `/api/scan` | `{"duration": 10}` | `scan on`, wait, `scan off`, return the refreshed table. Capped at 60 s. |
| `POST` | `/api/pair/<mac>` | — | Quick pair: `pair` → `trust` → `connect`. |
| `POST` | `/api/connect/<mac>` | — | Connect an already-paired device. |
| `POST` | `/api/disconnect/<mac>` | — | Disconnect. |
| `POST` | `/api/trust/<mac>` | — | Mark trusted so the device may reconnect itself. |
| `POST` | `/api/remove/<mac>` | — | Forget the pairing. |
| `GET` | `/api/adapters` | — | Controllers on this host (`hci0`, bus, product). |
| `POST` | `/api/adapter/<mac>` | — | Switch which controller everything acts on. |
| `GET` | `/api/players` | — | Players with state, uptime and restart count. |
| `POST` | `/api/players` | player JSON | Create one. |
| `PATCH` | `/api/players/<id>` | partial JSON | Update; a running player is restarted. |
| `DELETE` | `/api/players/<id>` | — | Stop and forget. |
| `POST` | `/api/players/<id>/{start,stop,restart}` | — | Lifecycle. |
| `GET` | `/api/players/<id>/logs` | — | Last 200 log lines. |
| `GET` | `/api/sinks` | — | PipeWire sinks available right now. |
| `POST` | `/api/players/<id>/control/{play,pause,playPause,next,previous}` | — | Transport, if the stream allows it. |
| `POST` | `/api/players/<id>/volume` | `{"percent":50}` or `{"muted":true}` | Per-client volume. |
| `GET`/`PATCH` | `/api/settings` | `{"bt_name_template":"{name} (BT)"}` | Defaults for new players. |
| `GET` | `/api/snapcast/stale` | — | Clients the server remembers but nothing uses. |
| `DELETE` | `/api/snapcast/client/<id>` | — | Forget one of those. |

A successful response carries the refreshed table, so the UI never needs a second
round trip:

```jsonc
{
  "devices": [
    { "mac": "AA:BB:CC:DD:EE:01", "name": "Topping DX5",
      "paired": true, "connected": true, "trusted": true }
  ],
  "warnings": [],
  "ok": true,
  "action": "connect"
}
```

Status codes: `400` invalid MAC or bad scan duration · `401` auth required ·
`409` the REPL is busy (a scan is running) · `502` BlueZ refused the operation ·
`503` no bluetoothctl, no controller, or bluetoothd unreachable.

While a scan holds the single `bluetoothctl` session, `GET /api/devices` does not
block for the scan's full duration — it waits ~2 s, then serves the last known
table tagged `"stale": true, "busy": true`. That keeps the 5-second poll and the
Docker healthcheck responsive.

## 🎧 Using it with Snapcast / PipeWire

The point of pairing a sink here is to hand it to a player. After connecting,
find the node name on the host and put it in the snapclient container's
`PIPEWIRE_NODE`:

```bash
pw-cli ls Node | grep -E 'node.name|node.description'
# node.name = "bluez_output.20_18_12_00_07_C4.1"
```

```yaml
environment:
  - PIPEWIRE_NODE=bluez_output.20_18_12_00_07_C4.1
```

See [pipewire-snapclient](https://github.com/shuricksumy/pipewire-snapclient) for
the rest of that setup.

## 🏗️ Development

```bash
# Run the tests -- they drive a fake bluetoothctl, so no adapter, D-Bus or root needed
pip install flask pexpect pytest
python -m pytest tests/ -v

# Run the app against the mock instead of a real daemon
BLUETOOTHCTL="python3 tests/mock_bluetoothctl.py" python3 app.py
# MOCK_MODE=nocontroller|exits|hangs|oldbluez exercises the degraded paths

# Build the image
docker build -t bluetooth-web .
```

| File | Role |
| :-- | :-- |
| [`app.py`](app.py) | Flask routes, MAC validation, Basic Auth, the stale-cache fallback |
| [`players.py`](players.py) | Supervised `snapclient` children: launch, backoff, logs, persistence |
| [`snapctl.py`](snapctl.py) | Snapserver JSON-RPC: now playing, transport, volume, client naming |
| [`btctl.py`](btctl.py) | The one long-lived `bluetoothctl` REPL, its lock, and all output parsing |
| [`static/index.html`](static/index.html) | The entire frontend — vanilla JS, no build step |
| [`healthcheck.py`](healthcheck.py) | Docker `HEALTHCHECK`; any HTTP answer counts as alive |
| [`tests/`](tests/) | pytest suite plus the fake `bluetoothctl` it runs against |

### Why drive a REPL instead of speaking D-Bus directly?

Pairing needs a registered BlueZ *agent* to answer the pairing request.
`bluetoothctl agent NoInputNoOutput` provides a working one in a single line;
reimplementing it over raw D-Bus means exporting an `org.bluez.Agent1` object and
handling `RequestConfirmation`, `RequestPinCode` and friends — a lot of surface
for a panel whose entire job is "click pair". The cost is parsing a terminal UI,
which is what `btctl.py` is careful about: it strips ANSI, ignores the
asynchronous `[NEW]`/`[CHG]` notifications, and drains the buffer before every
command so a reprinted prompt cannot be mistaken for a reply.

## License

MIT — see [LICENSE](LICENSE).
