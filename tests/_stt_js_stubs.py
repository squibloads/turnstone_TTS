"""Node stubs shared by the dictation-mic harness suites (``stt_mic.js`` itself
and the coordinator pane that mounts it).

Layered on ``_tts_js_stubs.TREE_DOM`` (the tree DOM): it adds the few element
features the mic touches (``style``, ``disabled``, ``classList.toggle``,
``dispatchEvent``, ``focus``) and fakes for ``getUserMedia`` /
``MediaRecorder`` / ``authFetch`` whose behaviour each test steers through
``fake``.  The shipped ``stt_mic.js`` is evaluated against these (imports
stripped by ``demodulize``), so the suites exercise the real controller.
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
# supports the types in `supported`, and the transcription route answers per
# `stt` ("stream" | "empty" | "error" | "hang").  `log` records what happened.
MIC_STUBS = r"""
const log = { toasts: [], announce: [], fetches: [], tracks: [], recorders: [], timers: [], cleared: [] };
const fake = {
  gum: "ok",              // "ok" | "deny" | "pending"
  supported: ["audio/webm;codecs=opus", "audio/webm"],
  stt: "stream", chunks: ["hello ", "world"], error: "STT is not configured",
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
