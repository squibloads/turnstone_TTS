"""``GET /v1/api/stt/config`` — the dictation knobs the browser applies.

Auto-stop is decided frame by frame in the browser, so the mic module needs
``audio.stt_auto_stop``, ``audio.stt_silence_ms`` and ``audio.stt_vad_threshold``
at recording time, over the same transport base it uploads to.  These pin the
payload (typed, bounded, every field present, tolerant of a missing or
unusable setting) and both routes that serve it: the node's for interactive
panes, the console's for coordinator panes.
"""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import _AuthMiddleware, _FakeConfigStore
from turnstone.core.web_helpers import stt_config_payload

_DEFAULTS = {
    "auto_stop": False,
    "silence_ms": 900,
    "vad_threshold": 0.5,
    "voice_mode_enabled": True,
    "frame_capture": False,
    "segment_gap_ms": 250,
    "frame_capture_max_seconds": 300,
}


def _store(values: dict[str, Any]) -> _FakeConfigStore:
    return _FakeConfigStore(dict(values))


def _client(handler: Any, values: dict[str, Any] | None) -> TestClient:
    app = Starlette(
        routes=[Route("/v1/api/stt/config", handler)],
        middleware=[Middleware(_AuthMiddleware)],
    )
    # ``None`` mirrors a degraded lifespan where no config store was bound.
    app.state.config_store = None if values is None else _store(values)
    return TestClient(app)


# ---------------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------------


def test_defaults_when_nothing_is_stored() -> None:
    assert stt_config_payload(config_store=_store({})) == _DEFAULTS


def test_defaults_when_no_config_store_is_bound() -> None:
    assert stt_config_payload(config_store=None) == _DEFAULTS


def test_stored_values_pass_through() -> None:
    values = {
        "audio.stt_auto_stop": True,
        "audio.stt_silence_ms": 1500,
        "audio.stt_vad_threshold": 0.7,
    }
    assert stt_config_payload(config_store=_store(values)) == {
        "auto_stop": True,
        "silence_ms": 1500,
        "vad_threshold": 0.7,
        "voice_mode_enabled": True,
        "frame_capture": False,
        "segment_gap_ms": 250,
        "frame_capture_max_seconds": 300,
    }


def test_out_of_range_values_clamp_to_the_registry_bounds() -> None:
    """A row written by an older build must not hand the browser a
    ``silence_ms`` the setting cannot hold — the detector would never stop."""
    low = stt_config_payload(
        config_store=_store({"audio.stt_silence_ms": 1, "audio.stt_vad_threshold": 0.0})
    )
    high = stt_config_payload(
        config_store=_store({"audio.stt_silence_ms": 99999, "audio.stt_vad_threshold": 7.0})
    )
    assert (low["silence_ms"], low["vad_threshold"]) == (300, 0.1)
    assert (high["silence_ms"], high["vad_threshold"]) == (5000, 0.9)


def test_unusable_values_fall_back_per_field() -> None:
    body = stt_config_payload(
        config_store=_store(
            {
                "audio.stt_auto_stop": "false",
                "audio.stt_silence_ms": "soon",
                "audio.stt_vad_threshold": "loud",
            }
        )
    )
    assert body["auto_stop"] is False
    assert body["silence_ms"] == 900 and body["vad_threshold"] == 0.5


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


def test_the_console_route_serves_the_payload() -> None:
    from turnstone.console.server import stt_config

    body = _client(stt_config, {"audio.stt_auto_stop": True}).get("/v1/api/stt/config")
    assert body.status_code == 200, body.text
    assert body.json() == {
        "auto_stop": True,
        "silence_ms": 900,
        "vad_threshold": 0.5,
        "voice_mode_enabled": True,
        "frame_capture": False,
        "segment_gap_ms": 250,
        "frame_capture_max_seconds": 300,
    }


def test_the_node_route_serves_the_payload() -> None:
    from turnstone.server import stt_config

    body = _client(stt_config, {"audio.stt_silence_ms": 1200}).get("/v1/api/stt/config")
    assert body.status_code == 200, body.text
    assert body.json() == {
        "auto_stop": False,
        "silence_ms": 1200,
        "vad_threshold": 0.5,
        "voice_mode_enabled": True,
        "frame_capture": False,
        "segment_gap_ms": 250,
        "frame_capture_max_seconds": 300,
    }


# ---------------------------------------------------------------------------
# Voice mode switch
# ---------------------------------------------------------------------------


def test_voice_mode_is_on_unless_explicitly_switched_off() -> None:
    # The coordinator pane reads this field to decide whether to offer hands-free
    # mode; the registry default is on, so only an explicit false turns it off.
    def body(v: Any) -> Any:
        return stt_config_payload(config_store=_store({"audio.voice_mode_enabled": v}))

    assert body(False)["voice_mode_enabled"] is False
    assert body("false")["voice_mode_enabled"] is False
    assert body(True)["voice_mode_enabled"] is True
    assert body(None)["voice_mode_enabled"] is True


def test_frame_capture_knobs_are_typed_bounded_and_off_by_default() -> None:
    """Frame capture is groundwork: a degraded or older server must never
    switch it on, and a bad stored row must not hand the browser a gap or cap
    the settings registry would not accept."""
    off = stt_config_payload(config_store=_store({}))
    assert off["frame_capture"] is False
    assert off["segment_gap_ms"] == 250
    assert off["frame_capture_max_seconds"] == 300

    on = stt_config_payload(
        config_store=_store(
            {
                "audio.stt_frame_capture": True,
                "audio.stt_segment_gap_ms": 400,
                "audio.stt_frame_capture_max_seconds": 600,
            }
        )
    )
    assert on["frame_capture"] is True
    assert on["segment_gap_ms"] == 400
    assert on["frame_capture_max_seconds"] == 600

    low = stt_config_payload(
        config_store=_store(
            {
                "audio.stt_segment_gap_ms": 1,
                "audio.stt_frame_capture_max_seconds": 1,
            }
        )
    )
    high = stt_config_payload(
        config_store=_store(
            {
                "audio.stt_segment_gap_ms": 99999,
                "audio.stt_frame_capture_max_seconds": 99999,
            }
        )
    )
    assert (low["segment_gap_ms"], low["frame_capture_max_seconds"]) == (100, 10)
    assert (high["segment_gap_ms"], high["frame_capture_max_seconds"]) == (2000, 3600)
    assert (
        stt_config_payload(config_store=_store({"audio.stt_frame_capture": "false"}))[
            "frame_capture"
        ]
        is False
    )
