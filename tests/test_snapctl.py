"""Snapserver RPC tests against a fake control port."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import snapctl  # noqa: E402
from fake_snapserver import FakeSnapserver  # noqa: E402


@pytest.fixture
def server():
    snapctl.invalidate()
    fake = FakeSnapserver()
    yield fake
    fake.close()
    snapctl.invalidate()


# ---- client id derivation ---------------------------------------------------


def test_client_id_matches_what_snapclient_registers():
    """snapclient appends #N for instances above the first -- observed live."""
    assert snapctl.client_id_for("DX5", 1) == "DX5"
    assert snapctl.client_id_for("Kitchen", 2) == "Kitchen#2"
    assert snapctl.client_id_for("Sumy JBL Charge5", 3) == "Sumy JBL Charge5#3"


# ---- reading ----------------------------------------------------------------


def test_describe_pulls_metadata_and_capabilities(server):
    info = snapctl.describe("127.0.0.1", server.port, "Kitchen#2")
    assert info["connected"] is True
    assert info["stream_id"] == "ma-kitchen"
    assert info["playback_status"] == "playing"
    assert info["title"] == "Baby Don't Hurt Me"
    # The artist arrives as a list and has to be flattened for display.
    assert info["artist"] == "David Guetta, Anne-Marie"
    assert info["can_control"] and info["can_pause"] and info["can_next"] and info["can_prev"]
    assert info["volume"] == 8


def test_capabilities_differ_per_stream(server):
    """The UI must read these, not assume a fixed button set."""
    dx5 = snapctl.describe("127.0.0.1", server.port, "DX5")
    assert dx5["can_next"] is True
    assert dx5["can_prev"] is False        # this stream cannot go back


def test_a_plain_stream_reports_no_control(server):
    info = snapctl.describe("127.0.0.1", server.port, "Ghost", use_cache=False)
    assert info["stream_id"] == "default"
    assert info["can_control"] is False
    assert info["title"] == ""


def test_unknown_client_is_none_not_an_error(server):
    assert snapctl.describe("127.0.0.1", server.port, "nope") is None


def test_notifications_do_not_confuse_the_reply(server):
    """The fake pushes an OnUpdate before every reply, like the real server."""
    for _ in range(3):
        assert snapctl.describe("127.0.0.1", server.port, "DX5",
                                use_cache=False)["client_id"] == "DX5"


def test_unreachable_server_is_a_clean_error():
    with pytest.raises(snapctl.SnapcastError) as err:
        snapctl.describe("127.0.0.1", 1, "DX5", use_cache=False)
    assert "cannot reach snapserver" in str(err.value)


def test_status_is_cached_briefly(server):
    snapctl.describe("127.0.0.1", server.port, "DX5")
    before = len([c for c in server.calls if c[0] == "Server.GetStatus"])
    snapctl.describe("127.0.0.1", server.port, "Kitchen#2")
    after = len([c for c in server.calls if c[0] == "Server.GetStatus"])
    assert after == before, "a second player should reuse the cached status"


# ---- writing ----------------------------------------------------------------


def test_set_name(server):
    snapctl.set_name("127.0.0.1", server.port, "DX5", "Living Room")
    assert server.clients["DX5"]["name"] == "Living Room"
    assert snapctl.describe("127.0.0.1", server.port, "DX5")["name"] == "Living Room"


def test_set_volume_and_mute(server):
    snapctl.set_volume("127.0.0.1", server.port, "DX5", percent=55)
    assert server.clients["DX5"]["volume"] == 55
    snapctl.set_volume("127.0.0.1", server.port, "DX5", muted=True)
    assert server.clients["DX5"]["muted"] is True


@pytest.mark.parametrize("percent,expected", [(-10, 0), (250, 100)])
def test_volume_is_clamped(server, percent, expected):
    snapctl.set_volume("127.0.0.1", server.port, "DX5", percent=percent)
    assert server.clients["DX5"]["volume"] == expected


def test_control_commands(server):
    snapctl.control("127.0.0.1", server.port, "ma-kitchen", "pause")
    assert server.streams["ma-kitchen"]["playbackStatus"] == "paused"
    snapctl.control("127.0.0.1", server.port, "ma-kitchen", "play")
    assert server.streams["ma-kitchen"]["playbackStatus"] == "playing"


def test_unsupported_command_is_refused_locally(server):
    with pytest.raises(snapctl.SnapcastError):
        snapctl.control("127.0.0.1", server.port, "ma-kitchen", "selfdestruct")
    assert not any(c[0] == "Stream.Control" for c in server.calls)


def test_control_without_a_stream_is_refused(server):
    with pytest.raises(snapctl.SnapcastError) as err:
        snapctl.control("127.0.0.1", server.port, "", "play")
    assert "not attached to a stream" in str(err.value)


def test_server_side_errors_surface(server):
    with pytest.raises(snapctl.SnapcastError) as err:
        snapctl.control("127.0.0.1", server.port, "default", "play")
    assert "cannot be controlled" in str(err.value)


# ---- housekeeping -----------------------------------------------------------


def test_stale_clients_and_deletion(server):
    stale = snapctl.stale_clients("127.0.0.1", server.port)
    assert [c["id"] for c in stale] == ["Ghost"]

    snapctl.delete_client("127.0.0.1", server.port, "Ghost")
    assert "Ghost" not in server.clients
    assert snapctl.stale_clients("127.0.0.1", server.port) == []
