<h1 align="center">🔊 bluetooth-web-snapclient</h1>

<p align="center">
  <b>Turn the Bluetooth speakers you already own into Hi-Fi multiroom players.</b><br>
  Pair from your browser. Play in sync with every other room. No SSH, no config files.
</p>

<p align="center">
  <a href="https://github.com/shuricksumy/bluetooth-web-snapclient/actions/workflows/build.yml">
    <img alt="Build" src="https://github.com/shuricksumy/bluetooth-web-snapclient/actions/workflows/build.yml/badge.svg">
  </a>
  <img alt="Platforms" src="https://img.shields.io/badge/arch-amd64%20%7C%20arm64-informational">
  <img alt="Base" src="https://img.shields.io/badge/base-debian%3Atrixie--slim-a80030">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-green"></a>
</p>

> **Part of the [Home Audio Stack](https://github.com/shuricksumy/home-audio-stack)** — Music Assistant → Snapcast → PipeWire, into USB DACs, Bluetooth speakers and LED strips. That page maps how these projects fit together.

<p align="center">
  <img src="docs/players-light.png" alt="Players tab: now playing, transport controls, per-player volume" width="100%">
</p>

---

## 🤔 The problem

You have a JBL on the shelf, headphones in a drawer, a portable speaker in the
bathroom. Good speakers, already paid for — and completely outside your music
system, because pairing a Bluetooth speaker to a headless Linux box means SSH,
`bluetoothctl`, and then digging a name like `bluez_output.F8_5C_7D_75_D6_06.1`
out of the audio stack and pasting it into a config file. For every speaker.
Again after every reinstall.

And once it *is* connected, it is still not a *room*: it plays on its own,
slightly out of step with everything else in the house.

## ✨ What you get

|  | |
| :-- | :-- |
| 🖱️ **Pairing from a web page** | Scan, pair, connect, forget. The command line stays closed. |
| 🏠 **Bluetooth speakers become rooms** | Each one joins Music Assistant as a synced [Snapcast](https://github.com/badaix/snapcast) player, next to your wired rooms. |
| 🎧 **Several at once** | The JBL in the kitchen and headphones in the study, playing together or separately. |
| 🔁 **It heals itself** | Turn a speaker off and on: it reconnects and its player comes back, on its own. |
| ▶️ **See and control what is playing** | Cover art, title, artist, volume, play / pause / skip — per room. |
| 🧭 **No node names to copy** | Pick the speaker; the audio device name is worked out for you. |
| 🎚️ **Sound check** | A left/right test tone, and the Bluetooth codec (LDAC, aptX HD, SBC-XQ …) as a dropdown. |

## 🖥️ The two tabs

| | |
| :-- | :-- |
| **Players** | Create, start and stop synced Snapcast players. Shows what is playing — title, artist, album art — with play / pause / next / previous and per-player volume. |
| **Devices** | Scan, pair, trust, reconnect, re-pair and forget Bluetooth devices. Pick between adapters if you have more than one (`hci0`, `hci1`, …). |

They belong together because a Bluetooth speaker's audio output only exists while
the speaker is connected. Starting a player **connects the speaker first**, waits
for its output to appear, and if the speaker later drops off, brings both back.

<table>
<tr>
<td width="50%"><img src="docs/devices-light.png" alt="Devices tab, light theme"></td>
<td width="50%"><img src="docs/devices-dark.png" alt="Devices tab, dark theme"></td>
</tr>
<tr>
<td align="center"><em>Devices — pair and manage speakers</em></td>
<td align="center"><em>…in light or dark</em></td>
</tr>
<tr>
<td width="50%"><img src="docs/players-light.png" alt="Players tab, light theme"></td>
<td width="50%"><img src="docs/players-dark.png" alt="Players tab, dark theme"></td>
</tr>
<tr>
<td align="center"><em>Players — one row per room</em></td>
<td align="center"><em>theme follows your system, or your choice</em></td>
</tr>
</table>

<details>
<summary><b>Under the hood</b>, for the curious (click to expand)</summary>

- **The node name is derived for you.** A paired speaker's sink is
  `bluez_output.<MAC with underscores>.1`, prefilled when you add a player.
- **Self-healing.** `snapclient` does *not* exit when its sink disappears — it sits
  there silently — so a watchdog restarts the player and reconnects the device.
- **And when the sink comes back without the player.** A sink that goes away and
  returns takes the stream with it: WirePlumber moves the player to the default
  sink and leaves it there, under a node that exists again and a process that
  never died. Everything reads healthy and the room is silent. So the watchdog
  also compares where the audio is actually linked against where it was aimed,
  and restarts the player when they differ.
- **Honest failure.** No adapter, no `bluetoothd`, no D-Bus socket or an AppArmor
  denial each produce a specific message, not a spinner.
- **Cheap polling.** The device list costs four D-Bus property reads and never
  touches the radio. Scanning does, so it only runs when you ask.
- **No build step, no pip.** Vanilla JS in one static page; Flask, pexpect and
  snapclient all come from distro packages.
- **Multi-arch, rebuilt weekly** for `linux/amd64` and `linux/arm64`, scanned with
  Trivy on every push.
- **Optional HTTP Basic Auth** via `ADMIN_PASSWORD`.

</details>

**Running more than one room?** The [Home Audio Stack](https://github.com/shuricksumy/home-audio-stack) has a [complete compose file](https://github.com/shuricksumy/home-audio-stack/tree/main/examples) with this panel alongside Music Assistant, the DACs and the LED strips.

## 🚀 Quick start

```yaml
services:
  bluetooth-web:
    image: ghcr.io/shuricksumy/bluetooth-web-snapclient:latest
    container_name: bluetooth-web
    ports:
      - "8088:8080"

    # Ubuntu and any AppArmor host — see "AppArmor" below. Not needed on Debian.
    security_opt:
      - apparmor=unconfined

    volumes:
      - /run/dbus/system_bus_socket:/run/dbus/system_bus_socket   # Bluetooth
      - /run/user/1000/pipewire-0:/tmp/pipewire-0                 # players
      - /dev/shm:/dev/shm                                         # players
      - ./bluetooth-web-config:/config                            # players.json

    environment:
      - ADMIN_PASSWORD=changeme        # empty or unset disables auth
      - SNAPSERVER_HOST=192.168.1.50   # optional; also editable in the web UI

    restart: unless-stopped
```

Then open `http://<host>:8088`. Ready-made files:
[`docker-compose-example.yaml`](docker-compose-example.yaml) (published image),
[`docker-compose.yml`](docker-compose.yml) (local build).

> Only pairing devices, not running players? Drop the last three volumes.

### Host requirements

BlueZ **5.65+** must be running on the host — this container only talks to it:

```bash
sudo apt-get install -y bluetooth bluez bluez-tools
sudo systemctl enable --now bluetooth
bluetoothctl show          # must print a controller
```

For players, the host also needs a working PipeWire session; see
[pipewire-snapclient](https://github.com/shuricksumy/pipewire-snapclient) for that
setup. Check the socket path matches your uid: `ls /run/user/$(id -u)/pipewire-0`.

### ⚠️ AppArmor hosts (Ubuntu)

On Ubuntu the socket mount is **not enough**. Docker confines containers with the
`docker-default` profile, which grants no D-Bus rules, so the kernel denies the
container's very first message:

```
An AppArmor policy prevents this sender from sending this message to this
recipient; member="Hello" destination="org.freedesktop.DBus"
```

`bluetoothctl` 5.82 does not check the resulting NULL connection and **dumps core**
rather than printing anything useful — the panel detects this case and says so.
The fix is `security_opt: [apparmor=unconfined]`, which drops only that profile:
seccomp, capabilities and namespaces stay, and the container still has nothing but
its sockets. Check whether it applies to you:

```bash
cat /sys/module/apparmor/parameters/enabled     # Y = enforcing
```

## 🏠 Home Assistant

The panel runs where the Bluetooth adapter and PipeWire session are, which is
rarely the Home Assistant box — so it has no place in the sidebar, because
Ingress only serves add-ons. A companion add-on proxies it in:

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fshuricksumy%2Fhome-assistant-apps)
[![Open your Home Assistant instance and show the dashboard of an add-on.](https://my.home-assistant.io/badges/supervisor_addon.svg)](https://my.home-assistant.io/redirect/supervisor_addon/?addon=bluetooth_web_proxy&repository_url=https%3A%2F%2Fgithub.com%2Fshuricksumy%2Fhome-assistant-apps)

Add the repository with the first button, open the add-on with the second, set
`server_host` / `server_port` (and the panel's `username` / `password` if you set
one), start it and tick *Show in sidebar*. Home Assistant's own login sits in
front, and it works through Nabu Casa like any other add-on.

Source: [bluetooth_web_proxy](https://github.com/shuricksumy/home-assistant-apps/tree/main/bluetooth_web_proxy).

## 🔊 Players

Pair a speaker on **Devices**, then **Players → Add player**. Choosing the device
fills in the rest:

| Field | Default |
| :-- | :-- |
| Output | the paired device — decides `PIPEWIRE_NODE` |
| Name | the device name plus a marker, e.g. `JBL Charge 5 (BT)` — template editable under ⚙ |
| Snapserver | from settings, else the host you are browsing |
| Latency (ms) | `0` — **see below** |
| PipeWire buffer | `1024/48000` for Bluetooth, `2048/192000` otherwise |
| ALSA bridge | on — copes with a sink changing sample rate under it |

### 🎚️ Sound check

**Devices → Sound**, or **Edit → Sound check** on a player. Two things live here.

<p align="center">
  <img src="docs/sound-check.png" width="72%" alt="The sound check dialog: left/right/both test tone buttons and a Bluetooth codec dropdown showing LDAC">
</p>

**Test tone.** A short beep straight into that speaker's own sink — left, right,
or both. It mixes with whatever is playing, so nothing has to be stopped, and it
answers the questions a player cannot: does this speaker make a sound at all, and
are the channels the way round you think they are. If the sink is muted the panel
says so rather than letting you conclude the hardware is dead.

**Bluetooth codec.** The dropdown lists what your speaker and your host actually
have in common — typically SBC, SBC-XQ, aptX, aptX HD and LDAC. A short list is a
host packaging question (`libspa-0.2-bluetooth`, and WirePlumber's `bluez5.codecs`),
not something the panel can widen.

Two things to know before you switch:

- **It renegotiates the link.** The sink is destroyed and rebuilt, so the players
  on that device are stopped and started again around the switch. Takes about a
  second.
- **Latency changes with the codec.** LDAC buys quality and spends delay; aptX-LL
  does the opposite. Re-check the player's *Latency (ms)* afterwards.

If a speaker ends up on its **headset** profile — mono, and it sounds like a phone
call — the panel flags it here, because nothing else will.

### ⚠️ Bluetooth latency

A2DP adds roughly **150–250 ms**. Against wired rooms a Bluetooth speaker will be
audibly late until you compensate, so set a **negative** *Latency (ms)* — start
near `-180` and tune by ear. This is the most likely reason a new BT player sounds
wrong, and it is not a bug.

### What this costs you

Players are children of this container, so **restarting the panel stops every
player**. If you need audio that survives a panel restart, run those players as
separate [pipewire-snapclient](https://github.com/shuricksumy/pipewire-snapclient)
containers — this panel leaves containers it did not create alone.

## ⚙️ Configuration

Everything under **⚙** in the UI is stored in `/config` and wins over the
environment; the environment only seeds the defaults.

| Variable | Default | Description |
| :-- | :-- | :-- |
| `ADMIN_PASSWORD` | _(empty)_ | Enables HTTP Basic Auth on every route. Empty = no auth. |
| `ADMIN_USER` | `admin` | Username for Basic Auth. |
| `SNAPSERVER_HOST` | _(empty)_ | Default Snapserver for new players. Empty = the host you browsed. |
| `SNAPSERVER_PORT` | `1704` | Audio port. |
| `SNAPSERVER_CONTROL_PORT` | `1705` | JSON-RPC: now playing, transport, client naming. |
| `SNAPSERVER_WEB_PORT` | `1780` | Snapweb, linked from the toolbar. |
| `POLL_SECONDS` | `5` | How often the browser re-reads state. |
| `CONFIG_DIR` | `/config` | Where `players.json` lives. |
| `PORT` / `BIND_HOST` | `8080` / `0.0.0.0` | Listener inside the container. |
| `DEBUG` | `false` | `true` raises the log level. |

## 🔒 Security

**It runs as root.** BlueZ's D-Bus policy grants `Pair`/`Trust`/`Remove` to uid 0,
and on some distributions to a `bluetooth`/`lp` group whose gid differs per host —
brittle to match from a container. It is still unprivileged in the Docker sense:
no extra capabilities, no device nodes, no host namespaces.

**Auth is off by default.** Unset `ADMIN_PASSWORD` means anyone who can reach the
port can unpair your speakers. Set it, keep this on a trusted LAN, and put TLS in
front if it goes anywhere else.

MAC addresses from the browser are checked with `fullmatch` against
`^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$` before reaching `bluetoothctl`'s stdin —
the backend drives one REPL that takes a command per line, so a MAC carrying a
newline would append commands to the session. (`re.match` would not do: `$` also
matches before a trailing newline.)

## 🧩 API

Every route returns JSON. `<mac>` must match the pattern above or you get a `400`.

| Method | Route | Description |
| :-- | :-- | :-- |
| `GET` | `/api/devices` | The device table. |
| `POST` | `/api/scan` | `scan on`, wait, `scan off`. Body `{"duration": 10}`, capped at 60s. |
| `POST` | `/api/pair/<mac>` | Pair → trust → connect. |
| `POST` | `/api/reconnect/<mac>` | Disconnect then connect. |
| `POST` | `/api/repair/<mac>` | Forget, rediscover, pair again. |
| `POST` | `/api/{connect,disconnect,trust,remove}/<mac>` | The individual steps. |
| `GET` | `/api/adapters` · `POST /api/adapter/<mac>` | List and select controllers. |
| `GET` | `/api/players` · `POST /api/players` | List and create players. |
| `PATCH`/`DELETE` | `/api/players/<id>` | Update (restarts a running player) / remove. |
| `POST` | `/api/players/<id>/{start,stop,restart}` | Lifecycle. |
| `POST` | `/api/players/<id>/control/{play,pause,next,previous}` | Transport, where the stream allows it. |
| `POST` | `/api/players/<id>/volume` | `{"percent":50}` or `{"muted":true}`. |
| `GET` | `/api/players/<id>/logs` | Last 200 lines. |
| `POST` | `/api/players/<id>/test/{left,right,both}` · `/api/devices/<mac>/test/{…}` | Play the test tone. |
| `GET`/`POST` | `/api/devices/<mac>/codec` | Read the codec list / switch it with `{"index": 6}`. |
| `GET` | `/api/sinks` | PipeWire sinks available now. |
| `GET`/`PATCH` | `/api/settings` | Panel settings. |
| `GET` | `/api/snapcast/stale` · `DELETE /api/snapcast/client/<id>` | Forget clients nothing uses. |

Status codes: `400` bad input · `401` auth required · `409` REPL busy or stream
cannot be controlled · `502` BlueZ/Snapcast refused · `503` no controller, no
bluetoothd, or the bus is unreachable.

## 🏗️ Development

```bash
pip install flask pexpect pytest
python -m pytest tests/ -v      # ~170 tests, no adapter or D-Bus needed

# Run against fakes instead of real hardware
BLUETOOTHCTL="python3 tests/mock_bluetoothctl.py" python3 app.py
# MOCK_MODE=nocontroller|exits|hangs|oldbluez exercises the degraded paths

docker build -t bluetooth-web-snapclient .
```

| File | Role |
| :-- | :-- |
| [`app.py`](app.py) | Flask routes, MAC validation, auth, stale-cache fallback |
| [`btctl.py`](btctl.py) | The long-lived `bluetoothctl` REPL and its parsing |
| [`players.py`](players.py) | Supervised `snapclient` children: launch, watchdog, backoff, persistence |
| [`snapctl.py`](snapctl.py) | Snapserver JSON-RPC: now playing, transport, volume, naming |
| [`static/index.html`](static/index.html) | The whole frontend — vanilla JS, no build step |
| [`tests/`](tests/) | pytest suite plus fake `bluetoothctl`, `snapclient` and Snapserver |

### Why drive a REPL instead of speaking D-Bus directly?

Pairing needs a registered BlueZ *agent*. `bluetoothctl agent NoInputNoOutput`
provides one in a line; doing it over raw D-Bus means exporting an
`org.bluez.Agent1` object and handling `RequestConfirmation` and friends — a lot
of surface for a panel whose job is "click pair". The cost is parsing a terminal
UI, which [`btctl.py`](btctl.py) is careful about: it strips ANSI, ignores async
`[NEW]`/`[CHG]` notifications, drains the buffer before each command, and accepts
both the BlueZ 5.7x `#` prompt and the 5.8x `>` one.

## License

MIT — see [LICENSE](LICENSE).
