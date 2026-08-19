# bluetooth-web

[![Build_Push_Scan](https://github.com/shuricksumy/bluetooth-web/actions/workflows/build.yml/badge.svg)](https://github.com/shuricksumy/bluetooth-web/actions/workflows/build.yml)

A tiny web admin panel for pairing, connecting and removing Bluetooth devices on
the **host** — scan, pair and trust a speaker from a browser instead of SSHing in
and driving `bluetoothctl` or the `bluetuith` TUI by hand.

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
