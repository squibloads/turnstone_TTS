"""Frame capture: the pure DSP and the AudioWorklet plumbing in ``stt_frames.js``.

The DSP half is deliberately browser-free — a streaming resampler, the
float→int16 conversion and the fixed-frame framer each run here under node, so
the sample math is pinned without an AudioContext.  The integration half runs
the shipped ``createFrameCapture`` against the stubbed audio fakes in
``_stt_js_stubs``: the start/stop lifecycle, the frame contract another module
codes against, and the teardown a capture that fails to initialise still owes
its caller.

``stt_frames.js`` is not wired into dictation yet; nothing here touches
``stt_mic.js``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import demodulize, node_skip, run_node_source
from tests._stt_js_stubs import FRAME_AUDIO_STUBS

pytestmark = node_skip

_STATIC = Path(__file__).resolve().parents[1] / "turnstone"
_MODULE = demodulize(_STATIC / "shared_static/stt_frames.js")
_PRELUDE = FRAME_AUDIO_STUBS + "\n" + _MODULE


def _run(body: str) -> Any:
    src = (
        _PRELUDE
        + "\n(async () => {\n"
        + body
        + "\n})().catch((e) => { console.error(e); process.exit(1); });"
    )
    r = run_node_source(src)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# The pure DSP
# ---------------------------------------------------------------------------


def test_48k_to_16k_keeps_a_sine_continuous_across_arbitrary_chunk_boundaries() -> None:
    """A boundary reset would show up twice: a sample-count drift and a jump.

    The chunk sizes are deliberately coprime-ish (1, 7, 127, 333, …) so the
    input split lands inside an interpolation interval as often as possible.
    """
    out = _run(
        """
const r = createResampler(48000, 16000);
const chunks = [1, 127, 1000, 333, 7, 2048, 511, 4096];
const inputRate = 48000, freq = 440, amp = 0.5;
let i = 0, ci = 0;
const samples = [];
while (i < inputRate) {
  const n = Math.min(chunks[ci++ % chunks.length], inputRate - i);
  const chunk = new Float32Array(n);
  for (let k = 0; k < n; k++) {
    chunk[k] = amp * Math.sin((2 * Math.PI * freq * (i + k)) / inputRate);
  }
  samples.push(...r.push(chunk));
  i += n;
}
let maxErr = 0, maxStep = 0;
for (let k = 0; k < samples.length; k++) {
  const ideal = amp * Math.sin((2 * Math.PI * freq * k) / 16000);
  maxErr = Math.max(maxErr, Math.abs(samples[k] - ideal));
  if (k) maxStep = Math.max(maxStep, Math.abs(samples[k] - samples[k - 1]));
}
console.log(JSON.stringify({ len: samples.length, maxErr, maxStep }));
"""
    )
    assert out["len"] == 16000
    # Linear interpolation of a 440 Hz sine at 16 kHz: the error is tiny.  A
    # seam that restarted the phase would be orders of magnitude larger.
    assert out["maxErr"] < 0.001
    # The largest true step is 0.5 * 2π * 440 / 16000 ≈ 0.086.  A discontinuity
    # at one of the many chunk boundaries would approach the sine's full swing.
    assert out["maxStep"] < 0.1


def test_44_1k_to_16k_is_within_one_sample_of_a_second() -> None:
    out = _run(
        """
const r = createResampler(44100, 16000);
const chunks = [441, 1000, 17];
let i = 0, ci = 0, len = 0;
while (i < 44100) {
  const n = Math.min(chunks[ci++ % chunks.length], 44100 - i);
  len += r.push(new Float32Array(n).fill(0.25)).length;
  i += n;
}
const oneShot = createResampler(44100, 16000).push(new Float32Array(44100).fill(0.25)).length;
console.log(JSON.stringify({ len, oneShot }));
"""
    )
    assert abs(out["len"] - 16000) <= 1
    assert abs(out["oneShot"] - 16000) <= 1


def test_the_framer_emits_exact_frames_and_carries_the_remainder() -> None:
    out = _run(
        """
const framer = createFramer(512);
const a = new Float32Array(700);
for (let i = 0; i < a.length; i++) a[i] = i;
const b = new Float32Array(900);
for (let i = 0; i < b.length; i++) b[i] = 700 + i;
const first = framer.push(a);
const second = framer.push(b);
const c = new Float32Array(512);
for (let i = 0; i < c.length; i++) c[i] = 1536 + i;
const third = framer.push(c);
const empty = framer.push(new Float32Array(0));
const shape = (frames) => frames.map((f) => [f.length, f[0], f[f.length - 1]]);
console.log(JSON.stringify({
  first: shape(first), second: shape(second), third: shape(third),
  empty: empty.length,
}));
"""
    )
    assert out["first"] == [[512, 0, 511]]
    # The second push's first frame straddles the two input chunks; the
    # remainder (64 samples) carried into the third push.
    assert out["second"] == [[512, 512, 1023], [512, 1024, 1535]]
    # The 64-sample remainder begins this frame and the new chunk fills it.
    assert out["third"] == [[512, 1536, 1983]]
    assert out["empty"] == 0


def test_float_to_int16_clips_at_the_rails() -> None:
    out = _run(
        """
const out = floatToInt16(new Float32Array([0, 0.25, -0.25, 1, -1, 2, -2, NaN, Infinity, -Infinity]));
console.log(JSON.stringify(Array.from(out)));
"""
    )
    # Positive values scale by 0x7fff and negative by 0x8000, so the rails are
    # asymmetric exactly as PCM16 is; out-of-range and non-finite input clips.
    assert out == [0, 8192, -8192, 32767, -32768, 32767, -32768, 0, 0, 0]


# ---------------------------------------------------------------------------
# The capture pipeline
# ---------------------------------------------------------------------------


def test_frames_carry_the_documented_contract_and_advance_by_32ms() -> None:
    out = _run(
        """
const frames = [];
const cap = createFrameCapture({ onFrame: (f) => frames.push(f) });
const ok = await cap.start({ getTracks: () => [{ stopped: false }] });
// One second of 48 kHz audio resamples to exactly 16000 samples: 31 whole
// frames (15872) with 128 samples left over.  A pinned wall clock proves the
// timestamps come from the sample count.
Date.now = () => 987654321;
frameLog.nodes[0].port.deliver(new Float32Array(48000).fill(0.5));
const shape = frames.map((f) => [f.pcm instanceof Int16Array, f.pcm.length, f.seq, f.t0Ms]);
cap.stop();
console.log(JSON.stringify({ ok, running: cap.running, shape, first: frames[0].pcm[0], contexts: frameLog.contexts.length }));
"""
    )
    assert out["ok"] is True
    assert out["running"] is False
    assert len(out["shape"]) == 31
    for seq, row in enumerate(out["shape"]):
        assert row == [True, 512, seq, seq * 32]
    # 0.5 survives resampling (it is DC) and becomes PCM16 0x4000.
    assert out["first"] == 16384


def test_start_then_stop_tears_down_the_graph_and_leaves_the_callers_stream_alone() -> None:
    out = _run(
        """
const track = { stopped: false };
const stream = { getTracks: () => [track] };
const frames = [];
const cap = createFrameCapture({ onFrame: (f) => frames.push(f) });
const ok = await cap.start(stream);
const node = frameLog.nodes[0];
const ctx = frameLog.contexts[0];
const url = frameLog.urls[0].url;
const live = [cap.running, ctx.state, node.connected.length, frameLog.modules.length];
cap.stop();
const torn = {
  running: cap.running, state: ctx.state, disconnected: node.disconnected,
  portOpen: node.portOpen, revoked: frameLog.revoked, url,
  tracks: [track.stopped], sources: frameLog.sources.map((s) => s.disconnected),
};
cap.stop();  // a second stop has nothing left to release
console.log(JSON.stringify({ ok, live, torn, revokedAfterSecondStop: frameLog.revoked.length }));
"""
    )
    assert out["ok"] is True
    assert out["live"] == [True, "running", 1, 1]
    assert out["torn"]["running"] is False
    assert out["torn"]["state"] == "closed"
    assert out["torn"]["disconnected"] is True
    assert out["torn"]["portOpen"] is False
    assert out["torn"]["revoked"] == [out["torn"]["url"]]
    assert out["torn"]["tracks"] == [False]
    assert out["torn"]["sources"] == [True]
    assert out["revokedAfterSecondStop"] == 1


def test_starting_twice_is_refused_and_a_restart_begins_a_fresh_sequence() -> None:
    out = _run(
        """
const frames = [];
const cap = createFrameCapture({ onFrame: (f) => frames.push(f) });
const stream = { getTracks: () => [] };
const first = await cap.start(stream);
const second = await cap.start(stream);
frameLog.nodes[0].port.deliver(new Float32Array(16000).fill(0.5));
cap.stop();
const afterFirstStop = frames.map((f) => f.seq);
const restarted = await cap.start(stream);
frameLog.nodes[1].port.deliver(new Float32Array(16000).fill(0.5));
cap.stop();
console.log(JSON.stringify({
  first, second, restarted, contexts: frameLog.contexts.length,
  afterFirstStop, afterRestart: frames.slice(afterFirstStop.length).map((f) => [f.seq, f.t0Ms]),
}));
"""
    )
    assert out["first"] is True and out["second"] is False
    assert out["restarted"] is True and out["contexts"] == 2
    assert out["afterFirstStop"] == list(range(10))
    assert out["afterRestart"] == [[seq, seq * 32] for seq in range(10)]


def test_stop_during_module_load_abandons_that_start_and_allows_a_fresh_one() -> None:
    """The in-flight start must release only its own context, not the next one.

    A shared "current context" slot is the trap here: if the abandoned start
    called the ordinary teardown after its addModule resolved, it would close
    whichever capture had committed in the meantime.
    """
    out = _run(
        """
let releaseGate;
frameFake.moduleGate = new Promise((resolve) => { releaseGate = resolve; });
const frames = [];
const errors = [];
const stream = { getTracks: () => [] };
const cap = createFrameCapture({
  onFrame: (f) => frames.push(f),
  onError: (e) => errors.push(e.message),
});
const first = cap.start(stream);
await frameTick();
const during = cap.running;
const refused = await cap.start(stream);   // a second start while the first initialises
cap.stop();
releaseGate();
const firstResult = await first;
const restarted = await cap.start(stream);
frameLog.nodes[0].port.deliver(new Float32Array(16000).fill(0.5));
cap.stop();
console.log(JSON.stringify({
  during, refused, firstResult, restarted, errors,
  contexts: frameLog.contexts.length,
  states: frameLog.contexts.map((c) => c.state),
  urls: frameLog.urls.length, revoked: frameLog.revoked.length,
  seqs: frames.map((f) => f.seq),
}));
"""
    )
    assert out["during"] is False
    assert out["refused"] is False
    assert out["firstResult"] is False
    assert out["restarted"] is True
    assert out["errors"] == []
    assert out["contexts"] == 2 and out["states"] == ["closed", "closed"]
    # The abandoned start released its own worklet URL; the second stop
    # released the restarted one.
    assert out["urls"] == 2 and out["revoked"] == 2
    assert out["seqs"] == list(range(10))


def test_a_browser_without_audio_worklet_reports_an_error_and_start_resolves_false() -> None:
    out = _run(
        """
frameFake.worklet = false;
const errors = [];
const cap = createFrameCapture({ onFrame: () => {}, onError: (e) => errors.push(e.message) });
const ok = await cap.start({ getTracks: () => [] });
console.log(JSON.stringify({
  ok, running: cap.running, errors,
  contexts: frameLog.contexts.length, state: frameLog.contexts[0].state,
  urls: frameLog.urls.length, modules: frameLog.modules.length,
}));
"""
    )
    assert out["ok"] is False
    assert out["running"] is False
    assert len(out["errors"]) == 1 and "AudioWorklet" in out["errors"][0]
    assert out["contexts"] == 1 and out["state"] == "closed"
    assert out["urls"] == 0 and out["modules"] == 0


def test_a_worklet_module_that_fails_to_load_is_reported_and_cleaned_up() -> None:
    out = _run(
        """
frameFake.failModule = "worklet download failed";
const errors = [];
const cap = createFrameCapture({ onFrame: () => {}, onError: (e) => errors.push(e.message) });
const ok = await cap.start({ getTracks: () => [] });
console.log(JSON.stringify({
  ok, running: cap.running, errors,
  state: frameLog.contexts[0].state, urls: frameLog.urls.map((u) => u.url),
  revoked: frameLog.revoked, nodes: frameLog.nodes.length,
}));
"""
    )
    assert out["ok"] is False
    assert out["running"] is False
    assert len(out["errors"]) == 1 and "worklet download failed" in out["errors"][0]
    assert out["state"] == "closed"
    assert out["revoked"] == out["urls"] and len(out["urls"]) == 1
    assert out["nodes"] == 0


def test_missing_audio_context_is_reported_rather_than_thrown() -> None:
    out = _run(
        """
const errors = [];
delete globalThis.AudioContext;
const cap = createFrameCapture({ onFrame: () => {}, onError: (e) => errors.push(e.message) });
const ok = await cap.start({ getTracks: () => [] });
console.log(JSON.stringify({ ok, running: cap.running, errors }));
"""
    )
    assert out == {
        "ok": False,
        "running": False,
        "errors": ["Audio capture is not supported in this browser"],
    }
