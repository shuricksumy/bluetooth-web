# syntax=docker/dockerfile:1

# bluetooth-web -- a pairing panel for the *host's* bluetoothd.
#
# Intentionally a separate image from snapcast-pipewire rather than a third ROLE
# in that entrypoint: the dependency sets do not overlap (bluez + Flask here,
# PipeWire + Snapcast there) and the lifecycles differ -- you pair a speaker once
# and leave this idle, while the snapclient runs continuously.
FROM debian:trixie-slim

# Changing this busts the apt cache so a rebuild really re-runs apt-get upgrade
# instead of restoring a stale layer (same trick as the snapcast image).
ARG REFRESH_WEEK=0

LABEL org.opencontainers.image.title="bluetooth-web" \
      org.opencontainers.image.description="Web panel for pairing Bluetooth devices on the host via BlueZ" \
      org.opencontainers.image.source="https://github.com/shuricksumy/pipewire-snapclient" \
      org.opencontainers.image.licenses="MIT"

# Distro packages only -- no pip, no wheels to audit, and security updates
# arrive with a plain rebuild.
#   bluez            provides bluetoothctl, the D-Bus client this drives.
#                    bluetoothd itself is installed but never started here; all
#                    radio work happens in the host's daemon.
#   python3-flask    the HTTP layer
#   python3-pexpect  drives bluetoothctl's interactive REPL over a pty
#   dbus-bin         dbus-send, used by the startup preflight. bluez pulls it in
#                    transitively today, but the error reporting depends on it,
#                    so it is named explicitly rather than left to chance.
#   ca-certificates  so apt/TLS in derived builds behave
RUN echo "cache epoch: ${REFRESH_WEEK}" && apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
    bluez \
    python3 \
    python3-flask \
    python3-pexpect \
    dbus-bin \
    ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py btctl.py healthcheck.py /app/
COPY static/ /app/static/

# libdbus reads this to find the system bus. The compose file bind-mounts the
# host's socket at exactly this path; nothing else from the host is needed --
# no --privileged, no --cap-add, no host networking, no /dev access.
# ADMIN_USER/ADMIN_PASSWORD are deliberately NOT declared here: app.py already
# defaults them ("admin" / no auth), and baking a credential-shaped ENV into the
# image trips secret scanners for no gain. Set them in compose instead.
ENV DBUS_SYSTEM_BUS_ADDRESS="unix:path=/run/dbus/system_bus_socket" \
    PORT="8080" \
    BIND_HOST="0.0.0.0" \
    DEBUG="false" \
    PYTHONUNBUFFERED="1"

EXPOSE 8080

# Deliberately root, unlike the snapcast image. BlueZ's D-Bus policy in
# /etc/dbus-1/system.d/bluetooth.conf grants org.bluez Pair/Trust/Remove to
# uid 0 (and, on some distros, to a "bluetooth"/"lp" group whose gid differs per
# host). Matching that gid from inside a container is brittle enough that root
# in a trusted-LAN sidecar is the better trade -- the container is still
# unprivileged in the Docker sense and its only host access is one socket.
# See bluetooth-web/README.md for the full reasoning.

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD python3 /app/healthcheck.py || exit 1

CMD ["python3", "/app/app.py"]
