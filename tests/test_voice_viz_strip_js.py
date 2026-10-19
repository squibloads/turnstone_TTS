"""The live spectrum strip's lifecycle (``shared_static/voice_viz.js``).

The strip mounts above the coordinator's CHILDREN list only during a voice
session, draws its bars from whichever source is speaking, and does no work
when it is idle: no animation frame and no AudioContext while voice mode is
off, and the frame loop stops when the page is hidden.  The speaker colour and
the reduced-motion meter are part of the same contract.

The shipped module runs in node against a fake canvas, a fake analyser and a
fake AudioContext, so the tests assert what the code asks the browser for, not
what a browser happens to draw.  The fakes model the parts of the browser the
strip got wrong: captureStream() on a fresh element yields no audio track,
getComputedStyle() resolves the colour tokens, and an AudioContext starts
suspended.  Audible playback is pinned with those: an attached element must
reach the destination, and an element must not be rerouted while the context
cannot run.
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
const log = { frames: [], cancelled: [], contexts: 0, analysers: 0, analyserList: [], connects: [], sourcesMade: 0, streamSourcesMade: 0, liveBranches: 0, destLive: 0, gets: 0, invalidFills: [], transforms: [], observers: [], stateChanges: 0 };
const dest = { kind: "destination" };
let fps = [];
// The browser surface the tests vary: the device pixel ratio the canvas must
// honour, the CSS box the stylesheet gives it, and the tokens the theme
// resolves to.  The canvas reads these per frame, so they stay getters.
let dprValue = 1;
let canvasCss = { w: 240, h: 48 };
const themeColours = {
  dark: { "--accent": "#e5a042", "--fg-dim": "#8a93ad" },
  light: { "--accent": "#745415", "--fg-dim": "#576275" },
};
let themeName = "dark";
let elementSourceThrows = false;
Object.defineProperty(globalThis, "devicePixelRatio", { get: () => dprValue, configurable: true });
// getComputedStyle resolves a custom property to its computed value, exactly
// as the browser does.  A canvas that is handed the raw "var(--x)" instead
// paints black, which is the failure this fake exists to catch.
globalThis.getComputedStyle = () => ({
  getPropertyValue: (name) => (themeColours[themeName] || {})[name] || "",
});
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
  constructor() { log.contexts++; this.sampleRate = 48000; this.state = "suspended"; this.destination = dest; this.listeners = {}; }
  createAnalyser() { return new FakeAnalyser(); }
  createMediaStreamSource(s) {
    // Chromium refuses a stream with no audio tracks.  That is exactly what a
    // captureStream() taken from a fresh Audio element hands back, so the fake
    // must refuse it too or the suite cannot see the real failure.
    if (typeof s.getAudioTracks === "function" && s.getAudioTracks().length === 0) {
      throw new Error("Failed to execute 'createMediaStreamSource' on 'AudioContext': MediaStream has no audio track.");
    }
    log.streamSourcesMade++;
    const n = { kind: "stream-source", stream: s, connect(t) { log.connects.push(["stream-source", t === dest ? "destination" : "other"]); }, disconnect() {} };
    return n;
  }
  createMediaElementSource(el) {
    if (elementSourceThrows) throw new Error("InvalidStateError: the element already has a media-element source");
    log.sourcesMade++;
    const n = {
      kind: "element-source", el, branches: new Set(), destConnected: false,
      connect(t) {
        log.connects.push(["element-source", t === dest ? "destination" : "other"]);
        // Only analyser branches are tracked as live branches; the destination
        // route is the speaker path, counted separately so a test can see
        // whether a finished reply left anything in the graph.
        if (t === dest) {
          if (!this.destConnected) { this.destConnected = true; log.destLive++; }
        } else {
          this.branches.add(t);
          log.liveBranches = this.branches.size;
        }
        return t;
      },
      disconnect(t) {
        if (t === dest) {
          if (this.destConnected) { this.destConnected = false; log.destLive--; }
        } else if (t) {
          this.branches.delete(t);
        } else {
          if (this.destConnected) { this.destConnected = false; log.destLive--; }
          this.branches.clear();
        }
        log.liveBranches = this.branches.size;
      },
    };
    return n;
  }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  removeEventListener(name, fn) { this.listeners[name] = (this.listeners[name] || []).filter((x) => x !== fn); }
  resume() {
    this.state = "running";
    log.stateChanges++;
    for (const fn of [...(this.listeners.statechange || [])]) fn({ type: "statechange" });
    return Promise.resolve();
  }
  close() { this.state = "closed"; return Promise.resolve(); }
}
globalThis.AudioContext = FakeContext;
globalThis.webkitAudioContext = undefined;
// A canvas with the 2D surface the strip draws on.  The fill setter validates
// like a real canvas: an unresolved custom property or an empty string is
// ignored and the previous fill survives.  `setTransform` is recorded so the
// drawing units can be checked against the CSS box and the backing store.
const draws = { rects: 0, fills: [], clears: 0, rectsGeom: [] };
function canvasSurface() {
  let fill = "#000000";
  const ctx = {
    get fillStyle() { return fill; },
    set fillStyle(value) {
      if (typeof value !== "string" || value === "" || /^var\(/.test(value)) { log.invalidFills.push(String(value)); return; }
      fill = value;
    },
    clearRect() { draws.clears++; },
    fillRect(x, y, w, h) { draws.rects++; draws.fills.push(fill); draws.rectsGeom.push({ x, y, w, h, fill }); },
    beginPath() {}, moveTo() {}, lineTo() {}, fill() {}, stroke() {}, arc() {}, rect() {}, fillText() {},
    save() {}, restore() {}, scale() {}, setTransform(a, b, c, d, e, f) { log.transforms.push([a, b, c, d, e, f]); },
  };
  let width = 300, height = 150;
  return {
    get width() { return width; }, set width(v) { width = v; },
    get height() { return height; }, set height(v) { height = v; },
    getContext: () => ctx,
  };
}
// Minimal host: a `div` with children and a classList, enough for the strip to
// mount, query and remove itself.  Visibility is a document-level flag.
class FakeEl {
  constructor(tag) { this.tagName = String(tag).toUpperCase(); this.children = []; this.parentNode = null; this.className = ""; this.attrs = {}; this.dataset = {}; this.style = {}; this.textContent = ""; this.hidden = false; this.listeners = {}; this.isConnected = false; }
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
  createElement: (t) => {
    const e = new FakeEl(t);
    if (t === "canvas") {
      const c = canvasSurface();
      e.width = c.width;
      e.height = c.height;
      e.getContext = c.getContext;
      Object.defineProperty(e, "clientWidth", { get: () => canvasCss.w });
      Object.defineProperty(e, "clientHeight", { get: () => canvasCss.h });
    }
    return e;
  },
  addEventListener: (n, f) => { (docListeners[n] ||= []).push(f); },
  removeEventListener: (n, f) => { docListeners[n] = (docListeners[n] || []).filter((x) => x !== f); },
  body: new FakeEl("body"),
  documentElement: new FakeEl("html"),
};
document.body.isConnected = true;
document.documentElement.isConnected = true;
function fireDoc(name) { for (const f of [...(docListeners[name] || [])]) f({ type: name }); }
// A theme change is an attribute mutation on the document root (that is what
// theme.js toggles).  The fake delivers it the way the browser would, so the
// strip's refresh path is exercised rather than assumed.
class FakeMutationObserver {
  constructor(cb) { this.cb = cb; this.active = false; log.observers.push(this); }
  observe(target, opts) { this.active = true; this.target = target; this.opts = opts; }
  disconnect() { this.active = false; }
  takeRecords() { return []; }
}
globalThis.MutationObserver = FakeMutationObserver;
function setTheme(name) {
  themeName = name;
  document.documentElement.dataset.theme = name === "dark" ? "" : name;
  for (const o of log.observers) if (o.active) o.cb([{ type: "attributes" }], o);
}
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
    # Tokens resolved to the values the canvas can actually paint.  A raw
    # "var(--accent)" is not a colour a canvas accepts and renders black.
    assert out["aiColour"] == "#e5a042"
    assert out["userColour"] == "#8a93ad"
    # Silence settles to a flat muted line, drawn with the muted token.
    assert out["silent"] == "#8a93ad"


def test_a_reply_is_read_through_its_element_not_an_empty_capture_stream() -> None:
    # Chromium's captureStream() on a fresh Audio element returns a stream with
    # no audio tracks until a src has loaded, and createMediaStreamSource throws
    # on it.  The player reports the element before its first clip exists, so
    # that path can never work: the element itself must be routed through a
    # media-element source, which reads every clip the element plays.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
await strip.resumeFromGesture();
const el = {
  src: "", paused: false, play() {},
  captureStream: () => ({ getAudioTracks: () => [] }),
};
strip.attachAudio(el, "ai");
console.log(JSON.stringify({
  sourcesMade: log.sourcesMade,
  streamSources: log.streamSourcesMade,
  toDestination: log.connects.filter((c) => c[0] === "element-source" && c[1] === "destination").length,
  speaker: strip.colourFor(),
}));
"""
    )
    assert out["sourcesMade"] == 1
    assert out["streamSources"] == 0
    assert out["toDestination"] == 1
    assert out["speaker"] == "#e5a042"


def test_the_element_source_reaches_the_speakers_exactly_once() -> None:
    # The element is routed through the audio graph to be read, so that route
    # must reach the destination or playback goes silent.  It must be made once
    # per element, never per clip.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
await strip.resumeFromGesture();
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
await strip.resumeFromGesture();
const el = { src: "", paused: false, play() {} };
for (let i = 0; i < 5; i++) strip.attachAudio(el, "ai");
console.log(JSON.stringify({ live: log.liveBranches }));
"""
    )
    assert out["live"] == 1


def test_a_reply_keeps_its_native_path_while_the_audio_context_cannot_run() -> None:
    # Routing an element into a suspended AudioContext would send its audio to
    # a graph that never reaches the speakers, muting the reply.  Until the
    # coordinator's gesture resumes the context, the element must be left on
    # its native path; it is tapped once the context starts.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
const el = { src: "", paused: false, play() {} };
strip.attachAudio(el, "ai");
const suspended = {
  sources: log.sourcesMade,
  connects: log.connects.length,
  speaker: strip.colourFor(),
  active: strip.isActive(),
};
await strip.resumeFromGesture();       // the Voice click
const running = {
  sources: log.sourcesMade,
  toDestination: log.connects.filter((c) => c[0] === "element-source" && c[1] === "destination").length,
  active: strip.isActive(),
};
console.log(JSON.stringify({ suspended, running }));
"""
    )
    assert out["suspended"] == {
        "sources": 0,
        "connects": 0,
        "speaker": "#e5a042",
        "active": False,
    }
    assert out["running"]["sources"] == 1
    assert out["running"]["toDestination"] == 1
    assert out["running"]["active"] is True


def test_a_broken_audio_graph_never_breaks_the_reply_or_loses_the_speaker() -> None:
    # The strip is a visual.  If the browser refuses the element source (for
    # example the element is already owned by another context), the attach must
    # not throw into the player, and the answer must still be painted in the
    # AI's colour rather than the mic's grey.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
await strip.resumeFromGesture();
elementSourceThrows = true;
let threw = false;
try {
  strip.attachAudio({ src: "", paused: false, play() {} }, "ai");
} catch (e) {
  threw = true;
}
console.log(JSON.stringify({ threw, speaker: strip.colourFor(), active: strip.isActive() }));
"""
    )
    assert out["threw"] is False
    assert out["speaker"] == "#e5a042"
    assert out["active"] is False


def test_a_finished_reply_releases_its_route_to_the_speakers() -> None:
    # A media-element source owns the only route to the speakers, so it must
    # stay connected while the reply plays.  Once the player pauses the element
    # and releases it, the route has to go too, or every reply leaves a live
    # node in the audio graph for the life of the page.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
await strip.resumeFromGesture();
const el = { src: "", paused: false, ended: false, play() {} };
strip.attachAudio(el, "ai");
const playing = log.destLive;
el.paused = true;          // the player pauses before releasing the element
strip.detach("ai");
console.log(JSON.stringify({ playing, after: log.destLive }));
"""
    )
    assert out["playing"] == 1
    assert out["after"] == 0


def test_hiding_the_strip_never_mutes_a_reply_that_is_still_playing() -> None:
    # Turning voice mode off must not cut a reply short: the strip leaves, but
    # a playing element keeps its route until the player reports its release.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
await strip.resumeFromGesture();
const el = { src: "", paused: false, ended: false, play() {} };
strip.attachAudio(el, "ai");
strip.hide();
const hidden = { live: log.destLive, active: strip.isActive() };
el.paused = true;          // the run ends after the strip is gone
strip.detach("ai");
console.log(JSON.stringify({ hidden, after: log.destLive }));
"""
    )
    assert out["hidden"] == {"live": 1, "active": False}
    assert out["after"] == 0


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
console.log(JSON.stringify({ rects: draws.rects, fills: [...new Set(draws.fills)], invalidFills: log.invalidFills }));
"""
    )
    # 32 bars, each with a falling peak marker: 64 rects per frame, all in the
    # accent resolved to a value the canvas can paint.
    assert out["rects"] == 64
    assert out["fills"] == ["#e5a042"]
    assert out["invalidFills"] == []


def test_showing_the_strip_paints_the_resting_floor_line() -> None:
    # An idle-but-shown strip is a flat muted line, not an empty box: it is
    # painted on show, before any source exists, so the documentation's "flat
    # grey line means nobody is talking" is what the user actually sees.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
draws.rects = 0; draws.fills = [];
strip.show();
console.log(JSON.stringify({ rects: draws.rects, fills: [...new Set(draws.fills)] }));
"""
    )
    assert out["rects"] == 64
    assert out["fills"] == ["#8a93ad"]


def test_stop_releases_the_theme_observer() -> None:
    # The observer watches the document root, so it must not outlive the strip
    # any more than the visibility listener does.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
const watching = log.observers.filter((o) => o.active).length;
strip.stop();
console.log(JSON.stringify({ watching, after: log.observers.filter((o) => o.active).length }));
"""
    )
    assert out["watching"] == 1
    assert out["after"] == 0


def test_a_theme_change_repaints_with_the_newly_resolved_tokens() -> None:
    # The tokens are resolved once per theme, not once per page: switching to
    # light must repaint the strip in the light accents.  The observer is what
    # theme.js's data-theme toggle reaches.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
strip.attachStream({ getTracks: () => [] }, "user");
strip.setSpeaker("ai");
draws.fills = [];
for (const f of log.frames.slice()) f();
const dark = [...new Set(draws.fills)];
setTheme("light");
const light = [...new Set(draws.fills.slice(-64))];
console.log(JSON.stringify({ dark, light, invalidFills: log.invalidFills }));
"""
    )
    assert out["dark"] == ["#e5a042"]
    assert out["light"] == ["#745415"]
    assert out["invalidFills"] == []


def test_the_canvas_backing_store_matches_the_css_box_times_the_pixel_ratio() -> None:
    # A canvas starts at 300x150 backing pixels.  Drawing a 48px-tall box in
    # that space squeezes the bars into the top third of the strip and leaves
    # dead space below, so the backing store must follow the CSS box and the
    # device pixel ratio, and the drawing transform must map CSS pixels to it.
    out = _run(
        """
dprValue = 2;
canvasCss = { w: 320, h: 48 };
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
strip.attachStream({ getTracks: () => [] }, "user");
for (const f of log.frames.slice()) f();
const canvas = strip.el.children[0];
console.log(JSON.stringify({
  width: canvas.width,
  height: canvas.height,
  transform: log.transforms[log.transforms.length - 1],
}));
"""
    )
    assert out["width"] == 640
    assert out["height"] == 96
    assert out["transform"] == [2, 0, 0, 2, 0, 0]


def test_the_bars_rise_from_the_bottom_edge_and_fill_the_strip() -> None:
    # The approved strip has a bottom baseline: a loud band reaches the top and
    # every bar's base sits on the floor, with no vertical offset from the
    # canvas' default 300x150 drawing space.
    out = _run(
        """
canvasCss = { w: 240, h: 48 };
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
strip.attachStream({ getTracks: () => [] }, "user");
const analyser = log.analyserList[log.analyserList.length - 1];
analyser.bins = new Uint8Array(4096).fill(255);
draws.rectsGeom = [];
for (const f of log.frames.slice()) f();
const bars = draws.rectsGeom.filter((r) => r.h > 2);
console.log(JSON.stringify({
  bottoms: [...new Set(bars.map((r) => r.y + r.h))],
  tallest: Math.max(...bars.map((r) => r.h)),
  left: Math.min(...bars.map((r) => r.x)),
  right: Math.max(...bars.map((r) => r.x + r.w)),
}));
"""
    )
    assert out["bottoms"] == [48]
    assert out["tallest"] == 48
    assert out["left"] == 0
    assert out["right"] < 240


def test_the_peak_marker_never_dips_to_the_floor_for_one_frame() -> None:
    # The marker falls by a fixed step.  Once that step crosses zero while the
    # bar still stands, an unclamped peak is re-seeded from the bar on the next
    # frame: the marker drops to the floor for exactly one frame and pops back,
    # every other frame.  Measured at ten times the strip height because at 48px
    # the dip is sub-pixel; the algorithm's geometry is scale-independent.
    out = _run(
        """
canvasCss = { w: 240, h: 480 };
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
strip.attachStream({ getTracks: () => [] }, "user");
const analyser = log.analyserList[log.analyserList.length - 1];
analyser.bins = new Uint8Array(4096).fill(255);
for (const f of log.frames.slice()) f();      // a loud frame seeds the peak
analyser.bins = new Uint8Array(4096).fill(5); // a quiet band keeps the bar up
let below = 0, floorDips = 0;
for (let i = 0; i < 80; i++) {
  draws.rectsGeom = [];
  log.frames[log.frames.length - 1]();
  const column = draws.rectsGeom.filter((r) => r.x === 0);
  const bar = column.filter((r) => r.h > 2)[0];
  const marker = column.filter((r) => r.h === 2)[0];
  if (!bar || !marker) continue;
  if (marker.y > bar.y) below++;
  if (marker.y === 476 && bar.y < 470) floorDips++;
}
console.log(JSON.stringify({ below, floorDips }));
"""
    )
    assert out["below"] == 0
    assert out["floorDips"] == 0


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
    assert out["fills"] == ["#8a93ad"]


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


def test_the_bars_keep_falling_to_the_floor_after_the_mic_detaches() -> None:
    # Stopping the recording detaches the mic at once.  The last frame must not
    # be held on screen: the strip keeps animating by itself until the bars and
    # their peak markers have settled, then stops asking for frames.
    out = _run(
        """
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
strip.attachStream({ getTracks: () => [] }, "user");
const analyser = log.analyserList[log.analyserList.length - 1];
analyser.bins = new Uint8Array(4096).fill(255);
log.frames[log.frames.length - 1]();          // one loud frame raises the bars
draws.rectsGeom = [];
strip.detach("user");
const activeOnDetach = strip.isActive();
const heights = [];
for (let i = 0; i < 80; i++) {
  draws.rectsGeom = [];
  log.frames[log.frames.length - 1]();
  const bar = draws.rectsGeom.filter((r) => r.x === 0 && r.h > 2)[0];
  heights.push(bar ? bar.h : 0);
}
const framesAtRest = log.frames.length;
for (let i = 0; i < 10; i++) log.frames[log.frames.length - 1]();
console.log(JSON.stringify({
  activeOnDetach,
  aboveFloor: heights.filter((h) => h > 2).length,
  first: heights[0],
  last: heights[heights.length - 1],
  monotonic: heights.every((h, i) => i === 0 || h <= heights[i - 1] + 1e-9),
  activeAfter: strip.isActive(),
  framesAtRest,
  framesAfter: log.frames.length,
}));
"""
    )
    # Still animating on the detach edge, so this is not the frozen last frame.
    assert out["activeOnDetach"] is True
    # Several frames above the floor: the bars fell, they did not snap in one.
    assert out["aboveFloor"] >= 3
    assert out["first"] > 2
    assert out["monotonic"] is True
    # Once settled the loop stops on its own; a stale callback is a no-op.
    assert out["last"] == 0
    assert out["activeAfter"] is False
    assert out["framesAfter"] == out["framesAtRest"]


def test_reduced_motion_meter_falls_to_the_floor_after_the_mic_detaches() -> None:
    # Reduced motion has no bars to animate, but its level meter is still a
    # live reading and must fall to the floor when the mic closes rather than
    # freezing at the last level.
    out = _run(
        """
fps = ["(prefers-reduced-motion: reduce)"];
const host = document.createElement("div");
const strip = createSpectrumStrip({ host });
strip.show();
strip.attachStream({ getTracks: () => [] }, "user");
const analyser = log.analyserList[log.analyserList.length - 1];
analyser.bins = new Uint8Array(4096).fill(255);
draws.rectsGeom = [];
log.frames[log.frames.length - 1]();
const loud = draws.rectsGeom[0].h;
strip.detach("user");
let lastH = null;
for (let i = 0; i < 80; i++) {
  draws.rectsGeom = [];
  log.frames[log.frames.length - 1]();
  if (draws.rectsGeom[0]) lastH = draws.rectsGeom[0].h;
}
console.log(JSON.stringify({ loud, lastH, active: strip.isActive() }));
"""
    )
    assert out["loud"] > 2
    assert out["lastH"] == 2
    assert out["active"] is False
