"""The live spectrum strip's lifecycle (``shared_static/voice_viz.js``).

The strip mounts above the coordinator's CHILDREN list only during a voice
session, draws its bars from whichever source is speaking, and does no work
when it is idle: no animation frame and no AudioContext while voice mode is
off, and the frame loop stops when the page is hidden.  The speaker colour and
the reduced-motion meter are part of the same contract.

The shipped module runs in node against a fake canvas, a fake analyser and a
fake AudioContext, so the tests assert what the code asks the browser for, not
what a browser happens to draw.  Audible playback is pinned separately: the
captureStream path must not reroute the element, and the fallback must keep
it audible.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import demodulize, node_skip, run_node_source

pytestmark = node_skip

_ROOT = Path(__file__).resolve().parents[1] / "turnstone" / "shared_static"
_VIZ_PATH = _ROOT / "voice_viz.js"
_MODULE = (
    demodulize(_VIZ_PATH) if _VIZ_PATH.exists() else "throw new Error('missing voice_viz.js');"
)

# Fakes for the browser surface the strip touches.  `log` records what the
# module asked for, so each test can assert on requests rather than pixels.
_FAKES = r"""
const log = { frames: [], cancelled: [], contexts: 0, analysers: 0, analyserList: [], connects: [], sourcesMade: 0, liveBranches: 0, gets: 0 };
const dest = { kind: "destination" };
let fps = [];
globalThis.requestAnimationFrame = (fn) => { log.frames.push(fn); return log.frames.length; };
globalThis.cancelAnimationFrame = (id) => { log.cancelled.push(id); };
globalThis.matchMedia = (q) => ({ matches: fps.includes(q) || fps.includes("*"), media: q });
// A branch is live from connect() until its own disconnect().  Counting live
// branches, not connect calls, is what shows whether a re-attach leaks.
class FakeAnalyser {
  // A real AnalyserNode reports frequencyBinCount as half its fftSize; the strip
  // sizes its bin buffer from it, so the fake must report it too or every read
  // is zero-length and the bars never move.
  constructor() { log.analysers++; this.fftSize = 0; this.smoothingTimeConstant = 0; this.bins = new Uint8Array(4096); this.connected = false; log.analyserList.push(this); }
  get frequencyBinCount() { return this.fftSize / 2; }
  connect(t) { this.connected = true; log.connects.push(["analyser", t === dest ? "destination" : "other"]); }
  disconnect() { if (this.connected) { this.connected = false; } }
  getByteFrequencyData(out) { log.gets++; out.set(this.bins.subarray(0, out.length)); }
}
class FakeContext {
  constructor() { log.contexts++; this.sampleRate = 48000; this.state = "suspended"; this.destination = dest; }
  createAnalyser() { return new FakeAnalyser(); }
  createMediaStreamSource(s) { const n = { kind: "stream-source", stream: s, connect(t) { log.connects.push(["stream-source", t === dest ? "destination" : "other"]); }, disconnect() {} }; return n; }
  createMediaElementSource(el) {
    log.sourcesMade++;
    const n = {
      kind: "element-source", el, branches: new Set(),
      connect(t) {
        log.connects.push(["element-source", t === dest ? "destination" : "other"]);
        // Only analyser branches are tracked as live; the destination route is
        // the speaker path and is never torn down by a re-attach.
        if (t !== dest) { this.branches.add(t); log.liveBranches = this.branches.size; }
        return t;
      },
      disconnect(t) { if (t) this.branches.delete(t); else this.branches.clear(); log.liveBranches = this.branches.size; },
    };
    return n;
  }
  resume() { this.state = "running"; return Promise.resolve(); }
  close() { this.state = "closed"; return Promise.resolve(); }
}
globalThis.AudioContext = FakeContext;
globalThis.webkitAudioContext = undefined;
// A canvas with the 2D surface the strip draws on.  Each draw is counted.
const draws = { rects: 0, fills: [], clears: 0, rectsGeom: [] };
function fakeCanvas() {
  const ctx = {
    fillStyle: "", clearRect() { draws.clears++; }, fillRect(x, y, w, h) { draws.rects++; draws.fills.push(ctx.fillStyle); draws.rectsGeom.push({ x, y, w, h, fill: ctx.fillStyle }); },
    beginPath() {}, moveTo() {}, lineTo() {}, fill() {}, stroke() {}, arc() {}, rect() {}, fillText() {},
    save() {}, restore() {}, scale() {}, setTransform() {},
  };
  return { width: 0, height: 0, clientWidth: 240, clientHeight: 48, getContext: () => ctx, style: {} };
}
// Minimal host: a `div` with children and a classList, enough for the strip to
// mount, query and remove itself.  Visibility is a document-level flag.
class FakeEl {
  constructor(tag) { this.tagName = String(tag).toUpperCase(); this.children = []; this.parentNode = null; this.className = ""; this.attrs = {}; this.style = {}; this.textContent = ""; this.hidden = false; this.listeners = {}; this.isConnected = false; }
  get classList() { const self = this; return { add: (...c) => { const s = new Set(self.className.split(/\s+/).filter(Boolean)); c.forEach((x) => s.add(x)); self.className = [...s].join(" "); }, remove: (...c) => { self.className = self.className.split(/\s+/).filter((x) => x && !c.includes(x)).join(" "); }, contains: (c) => self.className.split(/\s+/).includes(c), toggle: (c, on) => { on ? this.add(c) : this.remove(c); } }; }
  add(c) { this.classList.add(c); }
  appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); c._attach(this.isConnected); return c; }
  _attach(conn) { this.isConnected = conn; this.children.forEach((k) => k._attach(conn)); }
  insertBefore(c, ref) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; const i = ref ? this.children.indexOf(ref) : -1; if (i < 0) this.children.push(c); else this.children.splice(i, 0, c); c._attach(this.isConnected); return c; }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c.parentNode = null; c._attach(false); return c; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(n, f) { (this.listeners[n] ||= []).push(f); }
  querySelector(sel) { const hit = (n) => { if (("." + n.className.split(/\s+/)[0]) === sel || n.tagName === sel.toUpperCase()) return n; for (const c of n.children) { const r = hit(c); if (r) return r; } return null; }; return hit(this); }
  contains(n) { for (let x = n; x; x = x.parentNode) if (x === this) return true; return false; }
}
const docListeners = {};
globalThis.document = {
  visibilityState: "visible",
  hidden: false,
  createElement: (t) => { const e = new FakeEl(t); if (t === "canvas") Object.assign(e, fakeCanvas()); return e; },
  addEventListener: (n, f) => { (docListeners[n] ||= []).push(f); },
  removeEventListener: (n, f) => { docListeners[n] = (docListeners[n] || []).filter((x) => x !== f); },
  body: new FakeEl("body"),
  documentElement: new FakeEl("html"),
};
document.body.isConnected = true;
document.documentElement.isConnected = true;
function fireDoc(name) { for (const f of [...(docListeners[name] || [])]) f({ type: name }); }
const tick = async () => { for (let i = 0; i < 4; i++) await new Promise((r) => setImmediate(r)); };
"""

_PRELUDE = "\n".join((_FAKES, _MODULE))


def _run(body: str) -> Any:
    src = (
        _PRELUDE
        + f"\n(async () => {{\n{body}\n}})().catch((e) => {{ console.error(e); process.exit(1); }});"
    )
    r = run_node_source(src)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_nothing_is_created_and_no_frame_runs_while_voice_mode_is_off() -> None:
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
// Construction alone must not touch the audio graph or the frame loop.
const idle = { contexts: log.contexts, frames: log.frames.length, mounted: host.children.length };
strip.stop();
console.log(JSON.stringify({ idle, after: { contexts: log.contexts, frames: log.frames.length } }));
"""
    )
    assert out["idle"] == {"contexts": 0, "frames": 0, "mounted": 0}
    assert out["after"] == {"contexts": 0, "frames": 0}


def test_the_strip_mounts_above_the_children_list_only_while_shown() -> None:
    out = _run(
        """
const host = document.createElement("div");
const children = document.createElement("div");
children.className = "side-section";
host.appendChild(children);
const strip = createSpectrumStrip({ host, before: children });
strip.show();
const shown = { first: host.children[0] === strip.el, order: host.children.indexOf(strip.el) < host.children.indexOf(children), className: strip.el.className };
strip.hide();
const hidden = { mounted: host.children.indexOf(strip.el) };
console.log(JSON.stringify({ shown, hidden }));
"""
    )
    assert out["shown"]["order"] is True
    assert "voice-viz" in out["shown"]["className"]
    assert out["hidden"]["mounted"] == -1


def test_stop_is_the_one_teardown_and_leaves_no_document_listener_behind() -> None:
    # The visibility listener lives on the document, so the strip must be torn
    # down through stop(), which removes it.  A pane that skips stop() keeps the
    # listener; the coordinator's destroy() is what calls stop(), and its own
    # suite pins that call.  This pins the module side of that contract.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
const attached = (docListeners["visibilitychange"] || []).length;
strip.stop();
const after = (docListeners["visibilitychange"] || []).length;
console.log(JSON.stringify({ attached, after }));
"""
    )
    assert out["attached"] == 1
    assert out["after"] == 0


def test_leaving_the_session_unmounts_the_strip_and_releases_its_audio() -> None:
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
const micStream = { getTracks: () => [] };
strip.attachStream(micStream, "user");
strip.hide();
console.log(JSON.stringify({ mounted: host.children.length, frames: log.cancelled.length >= 0, active: strip.isActive() }));
"""
    )
    assert out["mounted"] == 0
    assert out["active"] is False


def test_a_hidden_strip_takes_no_place_in_the_panel() -> None:
    # Showing the strip must add exactly one band and hiding it must give that
    # band back, so the panel's other sections never move.
    out = _run(
        """
const host = document.createElement("div");
const children = document.createElement("div");
children.className = "side-section";
host.appendChild(children);
const strip = createSpectrumStrip({ host, before: children });
const before = host.children.length;
strip.show();
const shown = host.children.length;
strip.hide();
const hidden = host.children.length;
console.log(JSON.stringify({ before, shown, hidden }));
"""
    )
    assert out["shown"] == out["before"] + 1
    assert out["hidden"] == out["before"]


def test_the_frame_loop_runs_only_while_a_source_is_active() -> None:
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
const beforeSource = log.frames.length;                  // shown but nothing speaking
strip.attachStream({ getTracks: () => [] }, "user");
const afterSource = log.frames.length;
strip.detach("user");
const afterDetach = { active: strip.isActive(), cancelled: log.cancelled.length };
console.log(JSON.stringify({ beforeSource, afterSource, afterDetach }));
"""
    )
    assert out["beforeSource"] == 0
    assert out["afterSource"] > 0
    assert out["afterDetach"]["active"] is False
    assert out["afterDetach"]["cancelled"] >= 1


def test_the_frame_loop_stops_when_the_page_is_hidden_and_resumes_when_shown() -> None:
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
strip.attachStream({ getTracks: () => [] }, "user");
const running = strip.isActive();
document.visibilityState = "hidden";
fireDoc("visibilitychange");
// Frame work is what costs: count the frames requested while hidden, by
// running every queued callback and seeing whether one asks for another.
const requestedWhileHidden = (() => {
  const before = log.frames.length;
  for (let i = 0; i < 5; i++) for (const f of log.frames.slice()) f();
  return log.frames.length - before;
})();
const hiddenActive = strip.isActive();
document.visibilityState = "visible";
fireDoc("visibilitychange");
const back = strip.isActive();
console.log(JSON.stringify({ running, requestedWhileHidden, hiddenActive, back }));
"""
    )
    assert out["running"] is True
    assert out["requestedWhileHidden"] == 0
    assert out["hiddenActive"] is False
    assert out["back"] is True


def test_the_ai_speaking_draws_in_the_accent_and_the_user_in_the_muted_token() -> None:
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
strip.attachStream({ getTracks: () => [] }, "user");
strip.setSpeaker("user");
const userColour = strip.colourFor();
strip.setSpeaker("ai");
const aiColour = strip.colourFor();
strip.setSpeaker(null);
const silent = strip.colourFor();
console.log(JSON.stringify({ userColour, aiColour, silent }));
"""
    )
    assert out["aiColour"] == "var(--accent)"
    assert out["userColour"] == "var(--fg-dim)"
    # Silence settles to a flat muted line, drawn with the muted token.
    assert out["silent"] == "var(--fg-dim)"


def test_the_captured_stream_path_does_not_reroute_playback() -> None:
    # captureStream gives the analyser its own copy of the element's output;
    # the element keeps playing to the speakers with no node in between.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
const el = { captureStream: () => ({ id: "cap" }), src: "", paused: false };
strip.attachAudio(el, "ai");
console.log(JSON.stringify({ connects: log.connects, sourcesMade: log.connects.filter((c) => c[0] === "element-source").length }));
"""
    )
    assert out["sourcesMade"] == 0
    assert all(dest != "destination" or name != "element-source" for name, dest in out["connects"])


def test_the_fallback_routes_the_element_to_the_speakers_exactly_once() -> None:
    # No captureStream: the element has to go through a media-element source
    # to be read.  That source must reach the destination or playback goes
    # silent, and it must be made once per element, never per clip.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
const el = { src: "", paused: false, play() {} };
strip.attachAudio(el, "ai");
strip.attachAudio(el, "ai");     // a second clip on the same element
// Count element-source CREATIONS by their route to the speakers: exactly one
// creation, and exactly one of its connections reaches the destination.
const toDestination = log.connects.filter((c) => c[0] === "element-source" && c[1] === "destination").length;
console.log(JSON.stringify({ toDestination, sourcesMade: log.sourcesMade }));
"""
    )
    assert out["toDestination"] == 1
    assert out["sourcesMade"] == 1


def test_re_attaching_a_clip_leaves_no_stale_analyser_branch_on_the_element() -> None:
    # Each clip on a reply re-attaches.  A branch left behind per clip would
    # grow the graph for as long as the reply runs, so each attach must replace
    # the last branch rather than add to it.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
const el = { src: "", paused: false, play() {} };
for (let i = 0; i < 5; i++) strip.attachAudio(el, "ai");
console.log(JSON.stringify({ live: log.liveBranches }));
"""
    )
    assert out["live"] == 1


def test_the_audio_context_is_resumed_only_from_a_gesture() -> None:
    # Browsers start an AudioContext suspended.  The strip must not resume it on
    # its own timer; the coordinator calls resume from the Voice button click.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
strip.attachStream({ getTracks: () => [] }, "user");
const beforeGesture = strip.contextState();
await strip.resumeFromGesture();
const afterGesture = strip.contextState();
console.log(JSON.stringify({ beforeGesture, afterGesture }));
"""
    )
    assert out["beforeGesture"] == "suspended"
    assert out["afterGesture"] == "running"


def test_each_frame_paints_one_bar_per_band_in_the_speakers_colour() -> None:
    # The bars are the visible strip.  Each live frame must paint every band,
    # in the colour of whoever is speaking, or the strip is a blank box.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
strip.attachStream({ getTracks: () => [] }, "user");
draws.rects = 0; draws.fills = [];
strip.setSpeaker("ai");
// Run the frame the strip already queued on attach: that is the live frame.
const pending = log.frames.slice();
for (const f of pending) f();
console.log(JSON.stringify({ rects: draws.rects, fills: [...new Set(draws.fills)] }));
"""
    )
    # 32 bars, each with a falling peak marker: 64 rects per frame.
    assert out["rects"] == 64
    assert out["fills"] == ["var(--accent)"]


def test_a_peak_marker_holds_the_recent_maximum_and_falls_slower_than_the_bar() -> None:
    # The approved look has a falling peak marker above each bar: it jumps up
    # with a loud frame and then drops slowly, so a burst stays readable after
    # the bar itself has fallen.  Measured on the drawn geometry: in the first
    # column, the marker is the thin rect above the bar, and it must sit higher
    # than the bar after a silent frame.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
strip.attachStream({ getTracks: () => [] }, "user");
const analyser = log.analyserList[log.analyserList.length - 1];
analyser.bins = new Uint8Array(4096).fill(255);
draws.rectsGeom = [];
for (const f of log.frames.slice()) f();          // a loud frame
analyser.bins = new Uint8Array(4096);
draws.rectsGeom = [];
for (const f of log.frames.slice()) f();          // then silence
const col0 = draws.rectsGeom.filter((r) => r.x === 0);
const markerTop = Math.min(...col0.filter((r) => r.h <= 2).map((r) => r.y));
const barTop = Math.min(...col0.filter((r) => r.h > 2).map((r) => r.y));
console.log(JSON.stringify({ markerTop, barTop }));
"""
    )
    # Canvas y grows downward: a smaller top is higher.  The marker stays above
    # the fallen bar, so the burst remains visible for a moment.
    assert out["markerTop"] < out["barTop"]


def test_a_falling_peak_never_drops_below_the_floor() -> None:
    # The marker falls by a fixed step each frame.  Left unclamped, a long fall
    # takes the stored peak below zero, which draws above the canvas.  The
    # marker's drawn top must never leave the canvas.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
strip.attachStream({ getTracks: () => [] }, "user");
const analyser = log.analyserList[log.analyserList.length - 1];
analyser.bins = new Uint8Array(4096).fill(255);
log.frames[log.frames.length - 1]();
analyser.bins = new Uint8Array(4096);
const drawn = [];
for (let i = 0; i < 60; i++) {
  draws.rectsGeom = [];
  log.frames[log.frames.length - 1]();
  const m = draws.rectsGeom.filter((r) => r.x === 0 && r.h === 2)[0];
  if (m) drawn.push(m.y);
}
console.log(JSON.stringify({ minY: Math.min(...drawn) }));
"""
    )
    # Canvas y is 0 at the top.  The marker may rest at the floor, never above it.
    assert out["minY"] >= 0


def test_the_marker_outlasts_the_bar_on_the_way_down() -> None:
    # The qualitative test proves the marker sits above a fallen bar once; this
    # pins the rate: a few frames after a burst the marker is still well above the
    # bar, and the bar has reached the floor before the marker does.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
strip.attachStream({ getTracks: () => [] }, "user");
const analyser = log.analyserList[log.analyserList.length - 1];
analyser.bins = new Uint8Array(4096).fill(255);
log.frames[log.frames.length - 1]();
analyser.bins = new Uint8Array(4096);
function step() {
  draws.rectsGeom = [];
  log.frames[log.frames.length - 1]();
  const c0 = draws.rectsGeom.filter((r) => r.x === 0);
  // The bar's drawn height and the marker's drawn top, both from the canvas.
  const bar = c0.filter((r) => r.h > 2).map((r) => r.h)[0] || 0;
  const markTop = c0.filter((r) => r.h === 2).map((r) => r.y)[0];
  return [bar, markTop];
}
const samples = [];
for (let i = 0; i < 40; i++) samples.push(step());
// The gap is how far the marker stands above the bar's top.  The bar's top is
// 48 - bar; the marker's top is markTop, so a larger gap means the marker is
// higher and still holding the burst.
const gap = (s) => (48 - s[0]) - s[1];
console.log(JSON.stringify({ gap5: gap(samples[5]), gap10: gap(samples[10]), bar10: samples[10][0] }));
"""
    )
    # Five frames in the marker still stands well above the bar's top.
    assert out["gap5"] > 10
    # Ten frames in the bar has largely fallen while the marker still holds
    # clear air above it, so the burst stays readable after the bar has gone.
    assert out["gap10"] > 10
    assert out["bar10"] < 10


def test_bars_settle_gradually_to_the_floor_instead_of_snapping() -> None:
    # A loud frame then silence: the bar must still stand above the floor on the
    # first silent frame, so it visibly falls rather than vanishing in one frame.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
strip.attachStream({ getTracks: () => [] }, "user");
const analyser = log.analyserList[log.analyserList.length - 1];
analyser.bins = new Uint8Array(4096).fill(255);
for (const f of log.frames.slice()) f();
analyser.bins = new Uint8Array(4096);
draws.rectsGeom = [];
for (const f of log.frames.slice()) f();
const col0 = draws.rectsGeom.filter((r) => r.x === 0 && r.h > 2);
console.log(JSON.stringify({ firstSilentH: col0[0].h }));
"""
    )
    # The floor is 2px; a bar still above it on the first silent frame has fallen
    # gradually, not in one frame.
    assert out["firstSilentH"] > 2


def test_silence_draws_a_flat_muted_line_not_nothing() -> None:
    # With no speaker the strip still paints its baseline, in the muted token,
    # so an idle-but-visible strip is not a blank gap.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
strip.attachStream({ getTracks: () => [] }, "user");
draws.rects = 0; draws.fills = [];
strip.setSpeaker(null);
const pending = log.frames.slice();
for (const f of pending) f();
console.log(JSON.stringify({ rects: draws.rects, fills: [...new Set(draws.fills)] }));
"""
    )
    # Silence still paints every bar and its marker: 64 rects, all muted.
    assert out["rects"] == 64
    assert out["fills"] == ["var(--fg-dim)"]


def test_reduced_motion_draws_a_single_level_meter_not_the_bars() -> None:
    out = _run(
        """
fps = ["(prefers-reduced-motion: reduce)"];
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
strip.attachStream({ getTracks: () => [] }, "user");
const analyser = log.analyserList[log.analyserList.length - 1];
analyser.bins = new Uint8Array(4096).fill(255);
draws.rects = 0;
draws.rectsGeom = [];
for (const f of log.frames.slice()) f();
const meterDraws = draws.rects;
const meterH = draws.rectsGeom[0] ? draws.rectsGeom[0].h : 0;
console.log(JSON.stringify({ reduced: strip.isReducedMotion(), meterDraws, bars: strip.barCount(), meterH }));
"""
    )
    assert out["reduced"] is True
    assert out["bars"] == 1
    # The meter is one rectangle painted, not the bar field.
    assert out["meterDraws"] == 1
    # And it reads a real level: a loud spectrum draws a tall meter, not the
    # 2px floor an empty read would give.
    assert out["meterH"] > 2


def test_a_stream_source_is_never_connected_to_the_destination() -> None:
    # The mic is analysed but must not be heard back: no echo, no second route.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
strip.attachStream({ getTracks: () => [] }, "user");
console.log(JSON.stringify({ toDestination: log.connects.filter((c) => c[1] === "destination").length }));
"""
    )
    assert out["toDestination"] == 0
