#!/usr/bin/env python3
"""Stands in for pw-play: instead of playing the WAV, describe it.

Writes a JSON summary of what it was handed to $FAKE_PW_PLAY_OUT, so a test can
check the tone really has sound in the channel it asked for and silence in the
other -- the one thing a left/right test button has to get right.
"""
import json
import os
import struct
import sys
import wave

path = sys.argv[-1]
with wave.open(path) as handle:
    info = {
        "channels": handle.getnchannels(),
        "rate": handle.getframerate(),
        "sampwidth": handle.getsampwidth(),
        "frames": handle.getnframes(),
    }
    raw = handle.readframes(handle.getnframes())

samples = struct.unpack("<%dh" % (len(raw) // 2), raw)
info["peak_left"] = max((abs(v) for v in samples[0::2]), default=0)
info["peak_right"] = max((abs(v) for v in samples[1::2]), default=0)
info["node"] = os.environ.get("PIPEWIRE_NODE")

out = os.environ.get("FAKE_PW_PLAY_OUT")
if out:
    with open(out, "w") as handle:
        json.dump(info, handle)

sys.stderr.write(os.environ.get("FAKE_PW_PLAY_STDERR", ""))
sys.exit(int(os.environ.get("FAKE_PW_PLAY_RC", "0")))
