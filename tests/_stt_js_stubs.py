"""Node stubs shared by the dictation-family harness suites (``stt_mic.js``,
the coordinator pane that mounts it, and ``stt_frames.js``).

``MIC_DOM`` / ``MIC_STUBS`` layer on ``_tts_js_stubs.TREE_DOM`` (the tree DOM):
they add the few element features the mic touches (``style``, ``disabled``,
``classList.toggle``, ``dispatchEvent``, ``focus``) and fakes for
``getUserMedia`` / ``MediaRecorder`` / ``authFetch`` whose behaviour each test
steers through ``fake``.  The shipped ``stt_mic.js`` is evaluated against these
(imports stripped by ``demodulize``), so the suites exercise the real
controller.

``FRAME_AUDIO_STUBS`` is the browser-audio half for frame capture: an
``AudioContext`` whose worklet module load the caller can fail, an
``AudioWorkletNode`` whose port the test can feed, and a Blob-URL registry so
a suite can see the worklet source URL being revoked.  It needs no DOM.
"""

from __future__ import annotations

# Re-declares El/document on top of the tree DOM so every element the module
# or a test creates carries the extra surface.
MIC_DOM = r"""
const _TreeEl = El;
class MicEl extends _TreeEl {
  constructor(tag) {
    super(tag);
    this.style = {}; this.disabled = false; this.title = ""; this.value = ""; this.focused = false;
    const cl = this.classList;
    cl.toggle = (c, on) => { if (on === undefined) on = !cl.contains(c); if (on) cl.add(c); else cl.remove(c); return on; };
  }
  dispatchEvent(e) { (this.l[e.type] || []).forEach((f) => f(e)); return true; }
  focus() { this.focused = true; }
}
El = MicEl;
globalThis.document = { createElement: (t) => new MicEl(t) };
"""

# `fake` is the knob panel: getUserMedia answers per `gum`, MediaRecorder
# supports the types in `supported`, the transcription route answers per
# `stt` ("stream" | "empty" | "error" | "hang"), and the dictation settings
# route answers per `sttConfig` (an object, "error" for a failed response).
# `log` records what happened; config probes land in their own list so the
# upload assertions keep indexing `log.fetches` by upload.
MIC_STUBS = r"""
const log = { toasts: [], announce: [], fetches: [], configFetches: [], tracks: [], recorders: [], timers: [], cleared: [] };
const fake = {
  gum: "ok",              // "ok" | "deny" | "pending"
  supported: ["audio/webm;codecs=opus", "audio/webm"],
  stt: "stream", chunks: ["hello ", "world"], error: "STT is not configured",
  sttConfig: { auto_stop: false, silence_ms: 900, vad_threshold: 0.5 },
  pendingGum: null,
};
const tick = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r)); };
const makeAnnouncer = () => (msg) => { log.announce.push(msg); };
function showToast(msg, kind) { log.toasts.push([msg, kind]); }
globalThis.window = {
  setInterval: (f, ms) => { log.timers.push([f, ms]); return log.timers.length; },
  clearInterval: (id) => { log.cleared.push(id); },
};
function fakeStream() {
  const track = { stopped: false, stop() { this.stopped = true; } };
  log.tracks.push(track);
  return { getTracks: () => [track] };
}
Object.defineProperty(globalThis, "navigator", {
  configurable: true,
  value: {
    mediaDevices: {
      getUserMedia: () => {
        if (fake.gum === "deny") return Promise.reject(new Error("denied"));
        if (fake.gum === "pending") return new Promise((res) => { fake.pendingGum = () => res(fakeStream()); });
        return Promise.resolve(fakeStream());
      },
    },
  },
});
globalThis.MediaRecorder = class {
  constructor(stream, opts) {
    this.stream = stream; this.state = "inactive"; this.l = {};
    this.mimeType = (opts && opts.mimeType) || ""; this.requested = opts || null;
    log.recorders.push(this);
  }
  static isTypeSupported(t) { return fake.supported.includes(t); }
  addEventListener(n, f) { (this.l[n] ||= []).push(f); }
  start() { this.state = "recording"; }
  stop() {
    this.state = "inactive";
    queueMicrotask(() => {                   // real recorders fire these asynchronously
      (this.l.dataavailable || []).forEach((f) => f({ data: new Blob(["audio-bytes"]) }));
      (this.l.stop || []).forEach((f) => f());
    });
  }
};
function authFetch(url, opts) {
  if (url.indexOf("/stt/config") !== -1) {
    log.configFetches.push(url);
    if (fake.sttConfig === "error") return Promise.resolve({ ok: false, json: async () => ({}) });
    return Promise.resolve({ ok: true, json: async () => fake.sttConfig });
  }
  const f = { url, method: opts && opts.method, body: opts && opts.body, signal: opts && opts.signal };
  log.fetches.push(f);
  if (fake.stt === "hang") {
    return new Promise((_res, rej) => f.signal.addEventListener("abort", () => rej(new Error("aborted"))));
  }
  if (fake.stt === "error") return Promise.resolve({ ok: false, json: async () => ({ error: fake.error }) });
  const parts = fake.stt === "empty" ? [] : fake.chunks.map((c) => new TextEncoder().encode(c));
  let i = 0;
  return Promise.resolve({ ok: true, body: { getReader: () => ({
    read: async () => (i < parts.length ? { done: false, value: parts[i++] } : { done: true }),
  }) } });
}
// A Composer stand-in with the surface the mic uses: an action row holding a
// send button, and a textarea.
function makeComposer(value) {
  const row = new El("div"), send = new El("button"), input = new El("textarea");
  send.className = "composer-send"; input.value = value || "";
  input.inputs = 0; input.addEventListener("input", () => { input.inputs++; });
  row.appendChild(send);
  return { actionsRowEl: row, sendBtn: send, inputEl: input };
}
"""

# `frameFake` steers the audio fakes: `worklet` false is a browser without
# AudioWorklet; `failModule` is an error message `addModule` rejects with;
# `sampleRate` is the context rate the capture must resample from.  `frameLog`
# records the graph so a suite can assert the teardown (disconnected nodes,
# closed context, revoked worklet URL) without a real browser.
FRAME_AUDIO_STUBS = r"""
const frameLog = { contexts: [], nodes: [], sources: [], modules: [], urls: [], revoked: [] };
const frameFake = { worklet: true, failModule: null, sampleRate: 48000, moduleGate: null };
const frameTick = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r)); };
let _frameUrlSeq = 0;
// Subclass the real URL rather than replacing it, so a harness that also
// parses URLs keeps working; only the Blob-URL pair is recorded.
const _FrameURL = globalThis.URL;
globalThis.URL = class extends _FrameURL {
  static createObjectURL(blob) {
    const url = "blob:frame-" + (++_frameUrlSeq);
    frameLog.urls.push({ url, blob });
    return url;
  }
  static revokeObjectURL(url) { frameLog.revoked.push(url); }
};
class FakeAudioWorkletNode {
  constructor(context, name, options) {
    this.context = context; this.name = name; this.options = options;
    this.connected = []; this.disconnected = false; this.portOpen = true;
    const self = this;
    this.port = {
      onmessage: null,
      postMessage() {},
      close() { self.portOpen = false; },
      // The worklet posts mono chunks; a test stands in for the processor by
      // handing the main-thread handler the samples it would have posted.
      deliver(samples) { self.port.onmessage({ data: { type: "samples", samples } }); },
    };
    frameLog.nodes.push(this);
  }
  connect(target) { this.connected.push(target); }
  disconnect() { this.disconnected = true; this.connected = []; }
}
globalThis.AudioWorkletNode = FakeAudioWorkletNode;
class FakeAudioContext {
  constructor() {
    this.sampleRate = frameFake.sampleRate;
    this.state = "suspended";
    this.destination = { kind: "destination" };
    this.audioWorklet = frameFake.worklet
      ? {
          addModule: async (url) => {
            if (frameFake.moduleGate) await frameFake.moduleGate;
            if (frameFake.failModule) throw new Error(frameFake.failModule);
            frameLog.modules.push(url);
          },
        }
      : undefined;
    frameLog.contexts.push(this);
  }
  createMediaStreamSource(stream) {
    const source = {
      stream, disconnected: false,
      connect(node) { this.node = node; },
      disconnect() { this.disconnected = true; },
    };
    frameLog.sources.push(source);
    return source;
  }
  async resume() { this.state = "running"; }
  close() { this.state = "closed"; return Promise.resolve(); }
}
globalThis.AudioContext = FakeAudioContext;
"""
