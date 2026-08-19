# syntax=docker/dockerfile:1

# --- Stage 0: fetch the Snapcast packages from the upstream GitHub release ---
# Same approach as shuricksumy/pipewire-snapclient: runs on the build host (not
# under QEMU), picks the asset for $TARGETARCH, and verifies it against the
# sha256 digest the GitHub API publishes. The "with-pipewire" variant is the one
# that matters -- the plain package installs cleanly and then rejects
# "--player pipewire" only at runtime.
FROM --platform=$BUILDPLATFORM debian:trixie-slim AS snapcast

ARG TARGETARCH
ARG SNAPCAST_VERSION=latest
ARG SNAPCAST_SUITE=trixie

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl jq \
    && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    if [ "$SNAPCAST_VERSION" = "latest" ]; then \
        api="https://api.github.com/repos/snapcast/snapcast/releases/latest"; \
    else \
        api="https://api.github.com/repos/snapcast/snapcast/releases/tags/${SNAPCAST_VERSION}"; \
    fi; \
    curl -fsSL --retry 3 --retry-delay 5 -H "Accept: application/vnd.github+json" "$api" -o /tmp/release.json; \
    tag="$(jq -r .tag_name /tmp/release.json)"; \
    echo "==> Snapcast release ${tag} (${TARGETARCH}/${SNAPCAST_SUITE}, with-pipewire)"; \
    mkdir -p /debs; \
    pattern="snapclient_.*_${TARGETARCH}_${SNAPCAST_SUITE}_with-pipewire[.]deb"; \
    asset="$(jq -c --arg p "^${pattern}$" '[.assets[] | select(.name | test($p))] | first' /tmp/release.json)"; \
    [ "$asset" != "null" ] || { echo "ERROR: ${tag} has no asset matching ${pattern}" >&2; exit 1; }; \
    url="$(echo "$asset" | jq -r .browser_download_url)"; \
    sha="$(echo "$asset" | jq -r '.digest // ""' | sed 's/^sha256://')"; \
    [ -n "$sha" ] || { echo "ERROR: no sha256 digest published for the asset" >&2; exit 1; }; \
    curl -fsSL --retry 3 --retry-delay 5 "$url" -o /debs/snapclient.deb; \
    echo "${sha}  /debs/snapclient.deb" | sha256sum -c -


# --- Stage 1: runtime ---
FROM debian:trixie-slim

ARG REFRESH_WEEK=0

LABEL org.opencontainers.image.title="bluetooth-web" \
      org.opencontainers.image.description="Web panel for pairing Bluetooth devices and running Snapcast players against them" \
      org.opencontainers.image.source="https://github.com/shuricksumy/bluetooth-web" \
      org.opencontainers.image.licenses="MIT"

# Distro packages only -- no pip, no wheels to audit.
#   bluez                       bluetoothctl, the D-Bus client the panel drives
#   python3-flask               the HTTP layer
#   python3-pexpect             drives bluetoothctl's interactive REPL over a pty
#   dbus-bin                    dbus-send, used by the startup preflight
#   pipewire-bin                pw-dump: enumerate the host's sinks
#   wireplumber                 wpctl, for the per-player volume init. Only the
#                               client tool is used; the daemon stays unstarted.
#   pipewire-alsa + libasound2-plugins   the ALSA -> PipeWire bridge players use
#   ca-certificates             so apt/TLS in derived builds behave
RUN echo "cache epoch: ${REFRESH_WEEK}" && apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
    bluez \
    python3 \
    python3-flask \
    python3-pexpect \
    dbus-bin \
    pipewire-bin \
    wireplumber \
    pipewire-alsa \
    libasound2-plugins \
    ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ALSA -> PipeWire bridge. Players launched with use_alsa run
# `snapclient --player alsa -s default`, so pcm.default has to land on PipeWire
# rather than on real hardware. This is the path that copes with a Bluetooth
# sink changing sample rate underneath it.
RUN printf '%s\n' \
    'pcm.pipewire { type pipewire }' \
    'ctl.pipewire { type pipewire }' \
    'pcm.!default pcm.pipewire' \
    'ctl.!default ctl.pipewire' \
    > /etc/asound.conf

# Install snapclient and let apt resolve the libraries its .deb declares.
COPY --from=snapcast /debs/snapclient.deb /tmp/
RUN apt-get update && \
    apt-get install -y --no-install-recommends /tmp/snapclient.deb && \
    rm -f /tmp/*.deb && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Fail the build here rather than at runtime: a stock package would install fine
# and only reject "--player pipewire" once a user tried to start a player.
RUN set -eu; \
    ldd /usr/bin/snapclient | grep -q "not found" && { \
        echo "ERROR: unresolved shared libraries for snapclient" >&2; exit 1; }; \
    ldd /usr/bin/snapclient | grep -q libpipewire || { \
        echo "ERROR: snapclient is not linked against libpipewire" >&2; exit 1; }; \
    snapclient --version

WORKDIR /app
COPY app.py btctl.py players.py healthcheck.py /app/
COPY static/ /app/static/

# /config holds players.json. Mount it to keep players across image updates.
RUN install -d /config

# libdbus reads DBUS_SYSTEM_BUS_ADDRESS to find the system bus; the compose file
# bind-mounts the host's socket at exactly this path. PIPEWIRE_RUNTIME_DIR and
# PIPEWIRE_REMOTE point at the host's PipeWire socket, mounted at /tmp/pipewire-0
# -- the same convention pipewire-snapclient uses.
ENV DBUS_SYSTEM_BUS_ADDRESS="unix:path=/run/dbus/system_bus_socket" \
    PIPEWIRE_RUNTIME_DIR="/tmp" \
    PIPEWIRE_REMOTE="pipewire-0" \
    CONFIG_DIR="/config" \
    PORT="8080" \
    BIND_HOST="0.0.0.0" \
    DEBUG="false" \
    PYTHONUNBUFFERED="1"

EXPOSE 8080

# Deliberately root. BlueZ's D-Bus policy grants org.bluez Pair/Trust/Remove to
# uid 0 (and, on some distros, to a bluetooth/lp group whose gid differs per
# host); matching that gid from inside a container is brittle. The container is
# still unprivileged in the Docker sense -- see README.md.

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD python3 /app/healthcheck.py || exit 1

CMD ["python3", "/app/app.py"]
