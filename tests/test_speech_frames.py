"""The per-frame microphone receiver (``turnstone/core/speech_frames.py``).

Voice-mode frame capture sends 32 ms PCM16 frames to the server so the speaker
gate can score them later.  The receiver is deliberately a bounded in-memory
buffer, not a recording surface: these tests pin the frame contract, the
sequence/idempotency rules a retried batch relies on, and both hard caps (audio
bytes and wall-clock duration) that keep one capture from growing without
bound.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import _AuthMiddleware, _FakeConfigStore
from turnstone.core.speech_frames import (
    BYTES_PER_SECOND,
    FRAME_BYTES,
    FRAME_SAMPLES,
    FrameCaptureError,
    FrameCaptureStore,
    frame_capture_store,
)
from turnstone.core.web_helpers import frame_capture_response

_CAPTURE = "cap-1"
_WS = "ws-1"


def _frame(value: int = 0) -> bytes:
    """One whole frame: 512 little-endian PCM16 samples."""
    return int(value).to_bytes(2, "little", signed=True) * FRAME_SAMPLES


def _append(
    store: FrameCaptureStore,
    *,
    seq: int = 0,
    t0: int = 0,
    frames: int = 1,
    payload: bytes | None = None,
    max_seconds: int = 10,
    capture_id: str = _CAPTURE,
    ws_id: str = _WS,
    now: float | None = None,
) -> dict[str, object]:
    return store.append(
        ws_id=ws_id,
        capture_id=capture_id,
        first_seq=seq,
        first_t0_ms=t0,
        payload=_frame() * frames if payload is None else payload,
        max_seconds=max_seconds,
        now=now,
    )


# ---------------------------------------------------------------------------
# The buffer
# ---------------------------------------------------------------------------


def test_a_capture_counts_whole_frames_and_their_audio_span() -> None:
    store = FrameCaptureStore()
    first = _append(store, frames=31)
    second = _append(store, seq=31, t0=992, frames=1)

    assert first == {
        "capture_id": _CAPTURE,
        "accepted": 31,
        "frames": 31,
        "duplicate": False,
        "buffered_ms": 992,
    }
    assert second["accepted"] == 1 and second["frames"] == 32
    assert second["buffered_ms"] == 1024
    # The next story consumes the stored PCM as individual frames, not as the
    # batches the transport happened to use.
    frames = store.frames_for(_WS, _CAPTURE)
    assert len(frames) == 32
    assert frames[0] == _frame() and frames[-1] == _frame()


def test_a_partial_frame_is_refused() -> None:
    store = FrameCaptureStore()
    with pytest.raises(FrameCaptureError) as exc:
        _append(store, payload=b"\x00" * (FRAME_BYTES - 1))
    assert exc.value.status_code == 400
    assert store.frames_for(_WS, _CAPTURE) == ()


def test_an_empty_batch_is_refused() -> None:
    store = FrameCaptureStore()
    with pytest.raises(FrameCaptureError) as exc:
        _append(store, payload=b"")
    assert exc.value.status_code == 400


def test_a_retried_batch_is_idempotent_not_duplicated() -> None:
    """authFetch retries a 401 itself; the receiver must survive the replay."""
    store = FrameCaptureStore()
    _append(store, frames=2)
    replay = _append(store, frames=2)
    assert replay["accepted"] == 0
    assert replay["duplicate"] is True
    assert replay["frames"] == 2
    assert len(store.frames_for(_WS, _CAPTURE)) == 2


def test_a_sequence_gap_is_refused_with_the_expected_offset() -> None:
    store = FrameCaptureStore()
    _append(store, frames=2)
    with pytest.raises(FrameCaptureError) as exc:
        _append(store, seq=3, frames=1)
    assert exc.value.status_code == 409
    assert "expected 2" in str(exc.value)


def test_a_batch_that_straddles_the_buffer_is_refused() -> None:
    """Overlap means the sender's view and the buffer's disagree; a retry is
    all-or-nothing, so silently merging the new tail would hide the bug."""
    store = FrameCaptureStore()
    _append(store, frames=2)
    with pytest.raises(FrameCaptureError) as exc:
        _append(store, seq=1, frames=2)
    assert exc.value.status_code == 409


def test_the_audio_byte_cap_closes_the_capture_and_later_batches_are_gone() -> None:
    store = FrameCaptureStore()
    # 1 second of audio: 31 whole frames fit (31,744 bytes), one more does not.
    _append(store, frames=31, max_seconds=1)
    with pytest.raises(FrameCaptureError) as exc:
        _append(store, seq=31, t0=992, frames=1, max_seconds=1)
    assert exc.value.status_code == 413
    assert "cap" in str(exc.value)
    with pytest.raises(FrameCaptureError) as closed:
        _append(store, seq=31, t0=992, frames=1, max_seconds=1)
    assert closed.value.status_code == 410


def test_the_wall_clock_cap_closes_a_capture_that_keeps_dribbling() -> None:
    store = FrameCaptureStore()
    _append(store, frames=1, max_seconds=1, now=100.0)
    # The check carries a few seconds of grace for the final live batch, so a
    # capture is still accepted just past its audio cap and closed after it.
    _append(store, seq=1, t0=32, frames=1, max_seconds=1, now=105.0)
    with pytest.raises(FrameCaptureError) as exc:
        _append(store, seq=2, t0=64, frames=1, max_seconds=1, now=107.0)
    assert exc.value.status_code == 413
    assert "second" in str(exc.value)


def test_captures_are_isolated_by_workspace_and_id() -> None:
    store = FrameCaptureStore()
    _append(store, capture_id="a", frames=1)
    _append(store, capture_id="b", frames=2)
    _append(store, ws_id="ws-2", capture_id="a", frames=3)
    assert len(store.frames_for(_WS, "a")) == 1
    assert len(store.frames_for(_WS, "b")) == 2
    assert len(store.frames_for("ws-2", "a")) == 3


def test_an_idle_capture_is_evicted_after_its_cap_and_a_grace_period() -> None:
    store = FrameCaptureStore()
    _append(store, frames=1, max_seconds=10, now=100.0)
    # Another capture touches the store after the first one's lifetime;
    # pruning happens on access, so this call is what evicts the idle buffer.
    _append(store, capture_id="fresh", frames=1, max_seconds=10, now=141.0)
    assert store.frames_for(_WS, _CAPTURE) == ()
    assert len(store.frames_for(_WS, "fresh")) == 1


def test_every_capture_gets_its_own_store_handle_on_app_state() -> None:
    class _App:
        class state:  # noqa: N801 - mirrors Starlette's namespace object
            pass

    app = _App()
    first = frame_capture_store(app)
    assert isinstance(first, FrameCaptureStore)
    assert frame_capture_store(app) is first


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


def _client(values: dict[str, object]) -> TestClient:
    async def _allow(_request: object, _ws_id: str) -> None:
        return None

    async def _handler(request: object) -> object:
        return await frame_capture_response(request, authorize=_allow)  # type: ignore[arg-type]

    app = Starlette(
        routes=[
            Route(
                "/v1/api/workstreams/{ws_id}/speech-to-text/frames",
                _handler,
                methods=["POST"],
            )
        ],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.config_store = _FakeConfigStore(dict(values))
    return TestClient(app)


def _post(
    client: TestClient,
    *,
    capture: str = _CAPTURE,
    seq: int = 0,
    t0: int = 0,
    body: bytes | None = None,
    content_type: str = "application/octet-stream",
) -> object:
    return client.post(
        f"/v1/api/workstreams/{_WS}/speech-to-text/frames"
        f"?capture_id={capture}&seq={seq}&t0_ms={t0}",
        content=_frame() if body is None else body,
        headers={"Content-Type": content_type},
    )


def test_the_route_is_disabled_until_the_setting_is_on() -> None:
    client = _client({})
    response = _post(client)
    assert response.status_code == 403  # type: ignore[attr-defined]
    assert "disabled" in response.json()["error"]  # type: ignore[attr-defined]


def test_the_route_accepts_whole_frames_when_enabled() -> None:
    client = _client({"audio.stt_frame_capture": True})
    response = _post(client)
    assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    assert response.json() == {  # type: ignore[attr-defined]
        "capture_id": _CAPTURE,
        "accepted": 1,
        "frames": 1,
        "duplicate": False,
        "buffered_ms": 32,
    }


def test_the_route_requires_octet_stream_and_numeric_query_values() -> None:
    client = _client({"audio.stt_frame_capture": True})
    wrong_type = _post(client, content_type="application/json")
    assert wrong_type.status_code == 400  # type: ignore[attr-defined]
    response = client.post(
        f"/v1/api/workstreams/{_WS}/speech-to-text/frames?capture_id={_CAPTURE}&seq=soon&t0_ms=0",
        content=_frame(),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 400
    assert "seq" in response.json()["error"]


def test_the_route_rejects_a_batch_larger_than_a_capture_may_hold() -> None:
    """The configured cap is 10 s (the registry minimum); the sender gets a
    413 once the buffered audio would pass it, and the capture closes."""
    client = _client(
        {
            "audio.stt_frame_capture": True,
            "audio.stt_frame_capture_max_seconds": 10,
        }
    )
    one_second = _frame() * 31
    # 10 seconds is 320,000 bytes; ten 31-frame batches are 317,440, so the
    # eleventh batch is the first one that cannot fit.
    for seq in range(0, 310, 31):
        response = _post(client, seq=seq, t0=seq * 32, body=one_second)
        assert response.status_code == 200, response.text  # type: ignore[attr-defined]
    over = _post(client, seq=310, t0=310 * 32, body=one_second)
    assert over.status_code == 413  # type: ignore[attr-defined]
    assert "cap" in over.json()["error"]  # type: ignore[attr-defined]


def test_the_route_refuses_a_single_batch_above_its_own_bound() -> None:
    client = _client({"audio.stt_frame_capture": True})
    oversized = _frame() * 300  # 300 KiB, above the 256 KiB request bound
    response = _post(client, body=oversized)
    assert response.status_code == 413  # type: ignore[attr-defined]
    assert "batch" in response.json()["error"]  # type: ignore[attr-defined]


def test_the_route_bounds_a_chunked_body_as_it_arrives() -> None:
    """No Content-Length means the pre-check cannot fire; the streaming read
    must stop at the same ceiling rather than buffer the whole upload."""
    client = _client({"audio.stt_frame_capture": True})

    def _chunks() -> Any:
        for _ in range(300):
            yield _frame()

    response = client.post(
        f"/v1/api/workstreams/{_WS}/speech-to-text/frames?capture_id={_CAPTURE}&seq=0&t0_ms=0",
        content=_chunks(),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 413
    assert "batch" in response.json()["error"]


def test_the_byte_rate_constant_matches_the_client_frame_contract() -> None:
    assert FRAME_SAMPLES == 512
    assert FRAME_BYTES == 1024
    assert BYTES_PER_SECOND == 32000
