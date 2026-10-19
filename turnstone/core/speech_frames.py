"""Receiver for per-frame microphone audio (voice-mode speaker gate groundwork).

Voice mode's speaker gate has to score short spans of the microphone stream,
but dictation records one compressed webm/opus clip per turn — a codec stream
only means something as a whole.  The browser therefore taps the same
microphone through an AudioWorklet and posts raw 32 ms PCM16 frames here.

This module is the bounded server half: an in-memory buffer per capture, fed by
``POST /v1/api/workstreams/{ws_id}/speech-to-text/frames``.  Nothing here
transcribes, stores on disk or scores audio.  The frames wait for the speaker
gate story and for diagnostics; the existing clip upload remains the only path
to the transcriber.

Caps are hard by construction.  A capture has a fixed audio-byte budget (the
configured duration at 32,000 bytes/second) and a wall-clock lifetime; crossing
either closes it, and later batches are refused rather than silently dropped.
A capture also expires after its cap plus a grace period so a crashed tab
cannot pin memory forever.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

# The client contract: 512 samples of mono PCM16 at 16 kHz per frame.
FRAME_SAMPLES = 512
SAMPLE_RATE = 16000
FRAME_BYTES = FRAME_SAMPLES * 2
BYTES_PER_SECOND = SAMPLE_RATE * 2
FRAME_MS = (FRAME_SAMPLES * 1000) // SAMPLE_RATE

# One request is a transport batch, not a capture: eight seconds is far above
# the one-second batches the browser sends and still bounds a single body
# before it is buffered.
MAX_BATCH_BYTES = 256 * 1024

# Safety rails around the whole process rather than useful settings: a capture
# is already bounded by its own cap, and a leaked one is evicted after its
# lifetime.  The extra grace lets the last batch land after the cap edge.
MAX_CAPTURES = 8
IDLE_GRACE_SECONDS = 30.0

_CAPTURE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class FrameCaptureError(ValueError):
    """A batch the receiver will not buffer; ``status_code`` is the HTTP answer."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class _Capture:
    capture_id: str
    ws_id: str
    max_seconds: int
    max_bytes: int
    started_at: float
    updated_at: float
    first_t0_ms: int = 0
    frames: list[bytes] = field(default_factory=list)
    next_seq: int = 0
    total_bytes: int = 0
    closed: bool = False
    capped: bool = False

    def expires_at(self) -> float:
        return self.started_at + self.max_seconds + IDLE_GRACE_SECONDS


class FrameCaptureStore:
    """Bounded, in-memory frame buffers keyed by ``(workspace, capture id)``.

    The store is intentionally process-local: deployment restart drops the
    buffers, which is the right lifetime for groundwork that is off by default.
    """

    def __init__(
        self,
        *,
        max_captures: int = MAX_CAPTURES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._max_captures = max(1, int(max_captures))
        self._clock = clock or time.monotonic
        self._captures: dict[tuple[str, str], _Capture] = {}

    def append(
        self,
        *,
        ws_id: str,
        capture_id: str,
        first_seq: int,
        first_t0_ms: int,
        payload: bytes,
        max_seconds: int,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Buffer one batch of whole frames and report the capture's totals.

        ``first_seq`` is the frame index, not the batch index: a retried batch
        therefore replays cleanly, while a gap (a lost batch) is refused so the
        next story never scores audio it cannot place on the clock.
        """
        if not _CAPTURE_ID_RE.match(capture_id or ""):
            raise FrameCaptureError("capture id is missing or malformed")
        if not payload:
            raise FrameCaptureError("frame batch is empty")
        if len(payload) % FRAME_BYTES:
            raise FrameCaptureError(f"frame batch must contain whole {FRAME_BYTES}-byte frames")
        if len(payload) > MAX_BATCH_BYTES:
            raise FrameCaptureError(
                f"frame batch exceeds the {MAX_BATCH_BYTES}-byte limit",
                status_code=413,
            )
        if first_seq < 0:
            raise FrameCaptureError("frame sequence must not be negative")
        if first_t0_ms < 0:
            raise FrameCaptureError("frame timestamp must not be negative")

        moment = self._clock() if now is None else now
        self._prune(moment)

        count = len(payload) // FRAME_BYTES
        key = (ws_id, capture_id)
        capture = self._captures.get(key)
        if capture is None:
            capture = _Capture(
                capture_id=capture_id,
                ws_id=ws_id,
                max_seconds=max(0, int(max_seconds)),
                max_bytes=max(0, int(max_seconds)) * BYTES_PER_SECOND,
                started_at=moment,
                updated_at=moment,
                first_t0_ms=first_t0_ms,
            )
            self._make_room()
            self._captures[key] = capture

        if capture.closed:
            raise FrameCaptureError("capture is closed", status_code=410)

        # A stored row can change under a live capture; the stricter cap wins,
        # and a raised cap never reopens a buffer that was closed already.
        narrowed = max(0, int(max_seconds))
        if narrowed < capture.max_seconds:
            capture.max_seconds = narrowed
            capture.max_bytes = narrowed * BYTES_PER_SECOND
        if moment - capture.started_at > capture.max_seconds:
            capture.closed = True
            capture.capped = True
            raise FrameCaptureError(
                f"capture exceeded its {capture.max_seconds}-second cap",
                status_code=413,
            )

        end_seq = first_seq + count
        if end_seq <= capture.next_seq:
            # authFetch can replay the whole request after a 401; the receiver
            # answers that as a successful no-op instead of doubling the audio.
            return self._summary(capture, accepted=0, duplicate=True)
        if first_seq != capture.next_seq:
            raise FrameCaptureError(
                f"frame sequence gap: expected {capture.next_seq}, got {first_seq}",
                status_code=409,
            )
        if capture.total_bytes + len(payload) > capture.max_bytes:
            capture.closed = True
            capture.capped = True
            raise FrameCaptureError(
                f"capture cap reached ({capture.max_seconds} seconds)",
                status_code=413,
            )

        capture.frames.extend(
            payload[offset : offset + FRAME_BYTES] for offset in range(0, len(payload), FRAME_BYTES)
        )
        capture.next_seq = end_seq
        capture.total_bytes += len(payload)
        capture.updated_at = moment
        return self._summary(capture, accepted=count, duplicate=False)

    def frames_for(self, ws_id: str, capture_id: str) -> tuple[bytes, ...]:
        """The buffered frames, oldest first (the next story's input)."""
        capture = self._captures.get((ws_id, capture_id))
        return () if capture is None else tuple(capture.frames)

    def info_for(self, ws_id: str, capture_id: str) -> dict[str, Any] | None:
        """Counts and timing for diagnostics; never the PCM itself."""
        capture = self._captures.get((ws_id, capture_id))
        if capture is None:
            return None
        return {
            "capture_id": capture.capture_id,
            "frames": len(capture.frames),
            "buffered_ms": len(capture.frames) * FRAME_MS,
            "first_t0_ms": capture.first_t0_ms,
            "closed": capture.closed,
            "capped": capture.capped,
        }

    def _summary(self, capture: _Capture, *, accepted: int, duplicate: bool) -> dict[str, Any]:
        return {
            "capture_id": capture.capture_id,
            "accepted": accepted,
            "frames": len(capture.frames),
            "duplicate": duplicate,
            "buffered_ms": len(capture.frames) * FRAME_MS,
        }

    def _prune(self, now: float) -> None:
        expired = [key for key, capture in self._captures.items() if now > capture.expires_at()]
        for key in expired:
            del self._captures[key]

    def _make_room(self) -> None:
        if len(self._captures) < self._max_captures:
            return
        # Evict the least recently touched capture.  Rejecting the new one
        # would let a crashed tab lock a user out of the feature until the
        # process restarts; dropping the stale buffer loses only audio the
        # next story never received anyway.
        oldest = min(
            self._captures,
            key=lambda key: self._captures[key].updated_at,
        )
        del self._captures[oldest]


def frame_capture_store(app: Any) -> FrameCaptureStore:
    """The app's one store, created on first use."""
    store = getattr(app.state, "frame_capture_store", None)
    if not isinstance(store, FrameCaptureStore):
        store = FrameCaptureStore()
        app.state.frame_capture_store = store
    return store
