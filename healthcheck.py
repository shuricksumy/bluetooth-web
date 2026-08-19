#!/usr/bin/env python3
"""Docker healthcheck: is the panel answering HTTP at all?

Any status code counts as healthy, including 401 (ADMIN_PASSWORD is set and we
have no credentials) and 503 (the app is up but bluetoothd is unreachable). Only
a refused/timed-out connection means the process is actually broken -- the same
"the container is alive and doing its job" rule the snapclient healthcheck uses.
"""
import os
import sys
import urllib.error
import urllib.request

url = "http://127.0.0.1:%s/api/devices" % os.environ.get("PORT", "8080")

try:
    urllib.request.urlopen(url, timeout=5)
except urllib.error.HTTPError:
    pass
except Exception as exc:  # URLError, socket.timeout, ...
    print("unhealthy: %s" % exc, file=sys.stderr)
    sys.exit(1)
