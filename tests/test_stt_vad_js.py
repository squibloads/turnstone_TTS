"""Mic auto-stop: the silence decision and the detector wiring in ``stt_mic.js``.

``vadDecision`` is the pure half — the previous state, one frame's speech
probability and the clock go in, the next state and an optional ``"start"`` /
``"stop"`` come out — so the hysteresis is pinned here without a browser.  The
integration half runs the shipped ``createMicController`` against the dictation
fakes and a scripted detector: that the setting gates the detector, that a
``"stop"`` reaches the recorder's ordinary stop path (clip kept, upload made),
and that every teardown edge releases the detector.

The vendored detector is absent in this checkout by design; ``loadVad``
resolves null here, which is exactly the fallback the last tests pin.  A
suite that needs a working detector injects one via ``opts.loadVad``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import demodulize, node_skip, run_node_source
from tests._stt_js_stubs import MIC_DOM, MIC_STUBS
from tests._tts_js_stubs import TREE_DOM

pytestmark = node_skip

_STATIC = Path(__file__).resolve().parents[1] / "turnstone"
_MODULE = demodulize(_STATIC / "shared_static/stt_mic.js")
_PRELUDE = "\n".join((TREE_DOM, MIC_DOM, MIC_STUBS, _MODULE))

# A detector stand-in with the loader's contract: watch(stream, onProbability)
# returns a handle the controller must release.  `frames` collects the
# per-frame callbacks so a test can replay probabilities on its own clock.
_VAD_FAKE = """
const frames = [];
const handles = [];
const vad = {
  watch(stream, onProbability) {
    const handle = { stream, stopped: false, stop() { this.stopped = true; } };
    handles.push(handle);
    frames.push(onProbability);
    return handle;
  },
};
"""


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
# The pure decision
# ---------------------------------------------------------------------------


def test_hysteresis_starts_at_the_threshold_and_stops_after_a_full_silence_run() -> None:
    out = _run(
        """
const cfg = { threshold: 0.5, silenceMs: 900 };
let st = null;
const steps = [];
const feed = (p, t) => {
  const r = vadDecision(st, p, t, cfg);
  st = r.state;
  steps.push([t, r.action, st.speaking, st.silentSince]);
};
feed(0.20, 0);      // quiet before anything is said
feed(0.49, 50);     // just under the start threshold: still not speech
feed(0.50, 100);    // the threshold itself counts as speech
feed(0.80, 300);    // still speech
feed(0.20, 400);    // below threshold - 0.15: the silence run starts
feed(0.20, 1299);   // 899 ms of it
feed(0.20, 1300);   // 900 ms: stop
feed(0.20, 1400);   // nothing left to stop
console.log(JSON.stringify(steps));
"""
    )
    assert out == [
        [0, None, False, None],
        [50, None, False, None],
        [100, "start", True, None],
        [300, None, True, None],
        [400, None, True, 400],
        [1299, None, True, 400],
        [1300, "stop", False, None],
        [1400, None, False, None],
    ]


def test_recording_never_stops_before_speech_has_started() -> None:
    out = _run(
        """
const cfg = { threshold: 0.5, silenceMs: 900 };
let st = null;
const actions = [];
for (const t of [0, 900, 5000, 60000]) {
  const r = vadDecision(st, 0.0, t, cfg);
  st = r.state;
  actions.push([r.action, st.speaking]);
}
console.log(JSON.stringify(actions));
"""
    )
    assert out == [[None, False], [None, False], [None, False], [None, False]]


def test_a_blip_above_the_threshold_restarts_the_silence_run_without_a_second_start() -> None:
    out = _run(
        """
const cfg = { threshold: 0.5, silenceMs: 900 };
let st = null;
const steps = [];
const feed = (p, t) => { const r = vadDecision(st, p, t, cfg); st = r.state; steps.push([t, r.action]); return r; };
feed(0.90, 0);     // start
feed(0.10, 100);   // silence from 100
feed(0.60, 700);   // a blip: speech again, so the run restarts
feed(0.10, 800);   // silence from 800
feed(0.10, 1699);  // 899 ms after the restart
const beforeStop = st.speaking;
feed(0.10, 1700);  // 900 ms
console.log(JSON.stringify({ steps, beforeStop }));
"""
    )
    assert out["steps"] == [
        [0, "start"],
        [100, None],
        [700, None],  # the blip resets the clock, it does not re-announce speech
        [800, None],
        [1699, None],
        [1700, "stop"],
    ]
    assert out["beforeStop"] is True


def test_a_level_between_the_thresholds_breaks_the_silence_run() -> None:
    """The documented middle rule: the band is neither speech nor silence.

    It cannot start the recording, and because a stop needs an *unbroken* run
    below ``threshold - 0.15`` it restarts the clock — so one noisy frame
    during a pause does not cut the speaker off.  The cost is that a room
    sitting in the band never auto-stops (raise the threshold, or click).
    """
    out = _run(
        """
const cfg = { threshold: 0.5, silenceMs: 900 };
let st = null;
const steps = [];
const feed = (p, t) => { const r = vadDecision(st, p, t, cfg); st = r.state; steps.push([t, r.action, st.silentSince]); return r; };
feed(0.90, 0);     // start
feed(0.10, 100);   // silence from 100
feed(0.40, 700);   // band (>= 0.35, < 0.5): breaks the run
feed(0.10, 800);   // silence from 800
feed(0.10, 1699);  // 899 ms
const beforeStop = st.speaking;
feed(0.10, 1700);  // 900 ms
console.log(JSON.stringify({ steps, beforeStop }));
"""
    )
    assert out["steps"] == [
        [0, "start", None],
        [100, None, 100],
        [700, None, None],
        [800, None, 800],
        [1699, None, 800],
        [1700, "stop", None],
    ]
    assert out["beforeStop"] is True


def test_a_missing_or_unusable_configuration_falls_back_to_the_registry_defaults() -> None:
    out = _run(
        """
const steps = [];
let st = vadDecision(null, 0.6, 0, null);   // no cfg at all: default threshold 0.5
steps.push([st.action, st.state.speaking]);
st = vadDecision(st.state, 0.1, 100, {});   // default silence 900
const from = st.state.silentSince;
st = vadDecision(st.state, 0.1, 999, { threshold: "nope", silenceMs: "nope" });
const at899 = st.action;
st = vadDecision(st.state, 0.1, 1000, {});
console.log(JSON.stringify({ steps, from, at899, stop: st.action, speaking: st.state.speaking }));
"""
    )
    assert out["steps"] == [["start", True]]
    assert out["from"] == 100
    assert out["at899"] is None
    assert out["stop"] == "stop" and out["speaking"] is False


# ---------------------------------------------------------------------------
# The controller wiring
# ---------------------------------------------------------------------------


def test_auto_stop_ends_the_recording_and_keeps_the_clip() -> None:
    out = _run(
        _VAD_FAKE
        + """
let now = 1000;
Date.now = () => now;
const composer = makeComposer();
const m = createMicController({
  composer, base: "", wsId: "w",
  sttConfig: { auto_stop: true, silence_ms: 900, vad_threshold: 0.5 },
  loadVad: async () => vad,
});
m.setAvailable(true);
m.button.click(); await tick();
const armed = frames.length;
frames[0](0.90); now = 1500; frames[0](0.90);
now = 1600; frames[0](0.05);            // silence clock starts
now = 2499; frames[0](0.05);            // 899 ms
const beforeStop = [m.isRecording(), log.fetches.length];
now = 2500; frames[0](0.05);            // 900 ms -> auto-stop
await tick();
const f = log.fetches[0];
console.log(JSON.stringify({
  armed, beforeStop, recording: m.isRecording(),
  url: f.url, file: f.body.get("audio").name,
  handles: handles.length, stopped: handles.map((h) => h.stopped),
  tracksStopped: log.tracks.map((t) => t.stopped),
  announce: log.announce,
}));
"""
    )
    assert out["armed"] == 1
    assert out["beforeStop"] == [True, 0]
    assert out["recording"] is False
    assert out["url"] == "/v1/api/workstreams/w/speech-to-text/stream"
    assert out["file"] == "speech.webm"
    assert out["handles"] == 1 and out["stopped"] == [True]
    assert out["tracksStopped"] == [True]
    assert out["announce"][-1] == "Transcript added to message."


def test_the_setting_off_leaves_click_to_stop_untouched_and_never_loads_the_detector() -> None:
    out = _run(
        _VAD_FAKE
        + """
let loads = 0;
const composer = makeComposer();
const m = createMicController({
  composer, base: "", wsId: "w",
  sttConfig: { auto_stop: false, silence_ms: 900, vad_threshold: 0.5 },
  loadVad: async () => { loads++; return vad; },
});
m.button.click(); await tick();
const during = [loads, frames.length, m.isRecording()];
m.button.click(); await tick();
console.log(JSON.stringify({ during, loads, fetches: log.fetches.length, recording: m.isRecording() }));
"""
    )
    assert out["during"] == [0, 0, True]
    assert out["loads"] == 0
    assert out["fetches"] == 1 and out["recording"] is False


def test_a_detector_that_cannot_load_leaves_click_to_stop_in_place() -> None:
    out = _run(
        """
const composer = makeComposer();
const m = createMicController({
  composer, base: "", wsId: "w",
  sttConfig: { auto_stop: true, silence_ms: 900, vad_threshold: 0.5 },
  loadVad: async () => null,
});
m.button.click(); await tick();
const during = [m.isRecording(), log.fetches.length];
m.button.click(); await tick();
console.log(JSON.stringify({ during, fetches: log.fetches.length, recording: m.isRecording(), toasts: log.toasts }));
"""
    )
    assert out == {"during": [True, 0], "fetches": 1, "recording": False, "toasts": []}


def test_teardown_releases_the_detector() -> None:
    out = _run(
        _VAD_FAKE
        + """
Date.now = () => 1000;
const composer = makeComposer();
const m = createMicController({
  composer, base: "", wsId: "w",
  sttConfig: { auto_stop: true, silence_ms: 900, vad_threshold: 0.5 },
  loadVad: async () => vad,
});
m.button.click(); await tick();
frames[0](0.90);                        // speech started; detector is live
m.destroy(); await tick();
console.log(JSON.stringify({ handles: handles.length, stopped: handles.map((h) => h.stopped),
  tracksStopped: log.tracks.map((t) => t.stopped), fetches: log.fetches.length }));
"""
    )
    assert out == {"handles": 1, "stopped": [True], "tracksStopped": [True], "fetches": 0}


def test_a_recording_that_ends_before_the_settings_land_never_loads_the_detector() -> None:
    out = _run(
        _VAD_FAKE
        + """
let release;
const gate = new Promise((resolve) => { release = resolve; });
let loads = 0;
const composer = makeComposer();
const m = createMicController({
  composer, base: "", wsId: "w",
  sttConfig: () => gate,
  loadVad: async () => { loads++; return vad; },
});
m.button.click(); await tick();
m.button.click(); await tick();          // clicked stop while the probe is in flight
release({ auto_stop: true, silence_ms: 900, vad_threshold: 0.5 });
await tick();
console.log(JSON.stringify({
  loads, handles: handles.length, fetches: log.fetches.length,
  recording: m.isRecording(), tracksStopped: log.tracks.map((t) => t.stopped),
}));
"""
    )
    assert out == {
        "loads": 0,
        "handles": 0,
        "fetches": 1,
        "recording": False,
        "tracksStopped": [True],
    }


def test_the_settings_ride_their_own_probe_once_per_transport_base() -> None:
    out = _run(
        _VAD_FAKE
        + """
fake.sttConfig = { auto_stop: true, silence_ms: 300, vad_threshold: 0.6 };
let now = 1000;
Date.now = () => now;
const composer = makeComposer();
const m = createMicController({
  composer, base: "/node/n1", wsId: "w",
  loadVad: async () => vad,
});
m.button.click(); await tick();
frames[0](0.90); now = 1100; frames[0](0.05);   // silence clock starts
now = 1399; frames[0](0.05);                    // 299 ms of the route's 300
const beforeStop = m.isRecording();
now = 1400; frames[0](0.05);                    // 300 ms -> stop
await tick();
const stoppedAfterFirst = m.isRecording();
m.button.click(); await tick();                 // a second recording reuses the probe
console.log(JSON.stringify({
  beforeStop, stoppedAfterFirst, recording: m.isRecording(),
  configFetches: log.configFetches, records: log.recorders.length, handles: handles.length,
}));
"""
    )
    assert out["beforeStop"] is True
    assert out["stoppedAfterFirst"] is False
    assert out["recording"] is True  # the second recording is live
    assert out["configFetches"] == ["/node/n1/v1/api/stt/config"]
    assert out["records"] == 2 and out["handles"] == 2


def test_a_failed_settings_probe_falls_back_to_click_to_stop() -> None:
    out = _run(
        _VAD_FAKE
        + """
fake.sttConfig = "error";
let loads = 0;
const composer = makeComposer();
const m = createMicController({
  composer, base: "", wsId: "w",
  loadVad: async () => { loads++; return vad; },
});
m.button.click(); await tick();
const during = [loads, frames.length, m.isRecording()];
m.button.click(); await tick();
console.log(JSON.stringify({ during, fetches: log.fetches.length, toasts: log.toasts }));
"""
    )
    assert out["during"] == [0, 0, True]
    assert out["fetches"] == 1
    assert out["toasts"] == []
