"""Voice frame session (``shared_static/stt_frame_session.js``).

The session is the seam between dictation and the speaker-gate groundwork:
it owns the AudioWorklet capture, feeds every 32 ms frame to the segmenter and
the upload sender, and stops itself at the configured cap.  The capture and
segmenter modules are the shipped ones, evaluated in the same node harness, so
these tests exercise the real frame contract rather than a hand-built stand-in.

The transport half is pinned separately: batches are one second of audio, the
frame sequence is explicit on the wire (an auth retry must replay, not
duplicate), and a refusal is reported once and ends the sender instead of
retrying a hot microphone forever.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import demodulize, node_skip, run_node_source
from tests._stt_js_stubs import FRAME_AUDIO_STUBS

pytestmark = node_skip

_STATIC = Path(__file__).resolve().parents[1] / "turnstone/shared_static"
_PRELUDE = "\n".join(
    (
        FRAME_AUDIO_STUBS,
        demodulize(_STATIC / "stt_frames.js"),
        demodulize(_STATIC / "stt_segmenter.js"),
        demodulize(_STATIC / "stt_frame_session.js"),
    )
)

_HELPERS = r"""
// One 512-sample chunk at 16 kHz (the stub context's rate): the capture's
// resampler then passes it through one-for-one, so frame n is chunk n.
const SR = 16000;
const FRAME_MS = 32;
const FRAME_SAMPLES = 512;

function chunkAt(t0Ms, spans) {
  const chunk = new Float32Array(FRAME_SAMPLES);
  for (let i = 0; i < FRAME_SAMPLES; i++) {
    const tMs = t0Ms + (i * 1000) / SR;
    let speech = false;
    for (let s = 0; s < spans.length; s++) {
      if (tMs >= spans[s][0] && tMs < spans[s][1]) { speech = true; break; }
    }
    chunk[i] = speech
      ? 0.2 * Math.sin((2 * Math.PI * 440 * tMs) / 1000)
      : ((i * 37 + t0Ms) % 41 - 20) / 32768;
  }
  return chunk;
}

function feed(spans, untilMs) {
  let frames = 0;
  for (let t0 = 0; t0 < untilMs; t0 += FRAME_MS) {
    frameLog.nodes[0].port.deliver(chunkAt(t0, spans));
    frames++;
  }
  return frames;
}

const tick = async () => {
  for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r));
};

function frameOf(value, seq) {
  return { pcm: new Int16Array(FRAME_SAMPLES).fill(value), seq, t0Ms: seq * FRAME_MS };
}
"""


def _run(body: str) -> Any:
    src = (
        _PRELUDE
        + "\n"
        + _HELPERS
        + "\n(async () => {\n"
        + body
        + "\n})().catch((e) => { console.error(e); process.exit(1); });"
    )
    r = run_node_source(src)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------


def test_the_sender_batches_one_second_and_keeps_sequence_on_the_wire() -> None:
    out = _run(
        """
const posts = [];
const sender = createFrameSender({
  base: "/node/n1", wsId: "ws 1", captureId: "cap-1", batchFrames: 31,
  fetchImpl: (url, opts) => {
    posts.push({ url, method: opts.method, headers: opts.headers, body: opts.body });
    return Promise.resolve({ ok: true, json: async () => ({}) });
  },
});
for (let i = 0; i < 32; i++) sender.push(frameOf(0x1234, i));
await sender.close(); await tick();
console.log(JSON.stringify({
  posts: posts.map((p) => ({
    url: p.url, method: p.method, type: p.headers["Content-Type"],
    length: p.body.length, first: [p.body[0], p.body[1]], last: [p.body[p.body.length - 2], p.body[p.body.length - 1]],
  })),
  failed: sender.failed,
}));
"""
    )
    assert len(out["posts"]) == 2
    first, last = out["posts"]
    assert first == {
        "url": (
            "/node/n1/v1/api/workstreams/ws%201/speech-to-text/frames"
            "?capture_id=cap-1&seq=0&t0_ms=0"
        ),
        "method": "POST",
        "type": "application/octet-stream",
        "length": 31 * 1024,
        "first": [0x34, 0x12],
        "last": [0x34, 0x12],
    }
    assert last["url"].endswith("?capture_id=cap-1&seq=31&t0_ms=992")
    assert last["length"] == 1024 and last["first"] == [0x34, 0x12]
    assert out["failed"] is False


def test_a_refused_batch_is_reported_once_and_ends_the_sender() -> None:
    out = _run(
        """
const posts = [];
const errors = [];
const sender = createFrameSender({
  base: "", wsId: "w", captureId: "c", batchFrames: 2,
  fetchImpl: (url) => {
    posts.push(url);
    return Promise.resolve({ ok: false, json: async () => ({ error: "capture cap reached" }) });
  },
  onError: (err) => errors.push(err.message),
});
for (let i = 0; i < 2; i++) sender.push(frameOf(100, i));
await tick();
const afterFailure = posts.length;
for (let i = 2; i < 6; i++) sender.push(frameOf(100, i));
await sender.close(); await tick();
console.log(JSON.stringify({ posts: posts.length, afterFailure, failed: sender.failed, errors }));
"""
    )
    assert out["posts"] == out["afterFailure"] == 1
    assert out["failed"] is True
    assert out["errors"] == ["capture cap reached"]


# ---------------------------------------------------------------------------
# Capture + segmentation
# ---------------------------------------------------------------------------


def test_a_pause_shorter_than_the_segment_gap_stays_inside_one_segment() -> None:
    out = _run(
        """
frameFake.sampleRate = 16000;
const segments = [];
const posts = [];
const session = createFrameSession({
  base: "", wsId: "w", captureId: "c", segmentGapMs: 250, maxSeconds: 300,
  loadModules: async () => ({ createFrameCapture, createSegmenter }),
  fetchImpl: (url, opts) => { posts.push(opts.body.length); return Promise.resolve({ ok: true, json: async () => ({}) }); },
  onSegment: (s) => segments.push(s),
});
const ok = await session.start({ getTracks: () => [] });
const frames = feed([[320, 704], [904, 1288]], 2500);
await session.stop(); await tick();
console.log(JSON.stringify({
  ok, frames, count: segments.length,
  start: segments[0] && segments[0].startMs,
  end: segments[0] && segments[0].endMs,
  allInt16: !!(segments[0] && segments[0].frames.every((f) => f instanceof Int16Array && f.length === 512)),
  posts: posts.length,
}));
"""
    )
    assert out["ok"] is True
    assert out["frames"] == 79  # 2500 ms / 32 ms, frame-rounded
    assert out["count"] == 1
    assert out["start"] <= 320
    assert out["end"] >= 1288
    assert out["allInt16"] is True
    # 79 frames is two 31-frame batches plus a 17-frame flush on stop.
    assert out["posts"] == 3


def test_a_pause_over_the_segment_gap_splits_the_segments() -> None:
    out = _run(
        """
frameFake.sampleRate = 16000;
const segments = [];
const session = createFrameSession({
  base: "", wsId: "w", captureId: "c", segmentGapMs: 250, maxSeconds: 300,
  loadModules: async () => ({ createFrameCapture, createSegmenter }),
  fetchImpl: () => Promise.resolve({ ok: true, json: async () => ({}) }),
  onSegment: (s) => segments.push(s),
});
await session.start({ getTracks: () => [] });
feed([[320, 704], [1004, 1388]], 2500);
await session.stop(); await tick();
console.log(JSON.stringify({
  count: segments.length,
  first: segments[0] && [segments[0].startMs, segments[0].endMs],
  second: segments[1] && [segments[1].startMs, segments[1].endMs],
}));
"""
    )
    assert out["count"] == 2
    first, second = out["first"], out["second"]
    assert first[0] <= 320 and 704 <= first[1] <= 954
    assert second[0] <= 1004 and second[1] >= 1388


def test_the_configured_cap_stops_the_capture_and_flushes_what_it_has() -> None:
    out = _run(
        """
frameFake.sampleRate = 16000;
const segments = [];
const posts = [];
const session = createFrameSession({
  base: "", wsId: "w", captureId: "c", segmentGapMs: 250, maxSeconds: 0.256,
  loadModules: async () => ({ createFrameCapture, createSegmenter }),
  fetchImpl: (url, opts) => { posts.push(opts.body.length); return Promise.resolve({ ok: true, json: async () => ({}) }); },
  onSegment: (s) => segments.push(s),
});
await session.start({ getTracks: () => [] });
feed([[0, 1000]], 500);   // well past the two-frame cap
await tick();
console.log(JSON.stringify({
  running: session.running, posts, segments: segments.length,
  portOpen: frameLog.nodes[0].portOpen, contexts: frameLog.contexts.length,
}));
"""
    )
    assert out["running"] is False
    # Eight frames are the whole cap (256 ms, enough to keep a segment); they
    # were flushed by stop().
    assert out["posts"] == [8 * 1024]
    assert out["segments"] == 1
    assert out["portOpen"] is False and out["contexts"] == 1


def test_stopping_before_the_worklet_loads_leaves_no_hot_capture() -> None:
    out = _run(
        """
frameFake.sampleRate = 16000;
let release;
frameFake.moduleGate = new Promise((resolve) => { release = resolve; });
const errors = [];
const posts = [];
const session = createFrameSession({
  base: "", wsId: "w", captureId: "c", segmentGapMs: 250, maxSeconds: 300,
  loadModules: async () => ({ createFrameCapture, createSegmenter }),
  fetchImpl: (url, opts) => { posts.push(opts.body.length); return Promise.resolve({ ok: true, json: async () => ({}) }); },
  onError: (e) => errors.push(e.message),
});
const starting = session.start({ getTracks: () => [] });
await tick();
session.stop();
release();
const ok = await starting;
await tick();
console.log(JSON.stringify({
  ok, running: session.running, errors, posts,
  contexts: frameLog.contexts.length,
  states: frameLog.contexts.map((c) => c.state),
  revoked: frameLog.revoked.length,
}));
"""
    )
    assert out["ok"] is False
    assert out["running"] is False and out["errors"] == [] and out["posts"] == []
    assert out["contexts"] == 1 and out["states"] == ["closed"]
    assert out["revoked"] == 1
