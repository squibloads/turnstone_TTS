"""Node stubs shared by the read-aloud harness suites (coordinator pane and the
``tts_player.js`` module): a small tree DOM with text nodes, and controllable
``authFetch`` / ``Audio`` / toast fakes.

The shipped ``tts_player.js`` is evaluated against these (imports stripped by
``demodulize``, so ``authFetch`` & co. resolve to the globals below), which is
why the suites exercise the real player rather than a copy.
"""

from __future__ import annotations

# Tree DOM: parent/sibling links, insertBefore, class selectors, and text nodes
# so the speech walker has something to read.  ``childNodes`` is NodeList-like
# (iterable, length, item(), NO array methods) as in browsers, so code that
# treats it as an Array fails here the way it did in Chrome.
TREE_DOM = r"""
class NodeList_ {
  constructor(a) { this._a = a; }
  get length() { return this._a.length; }
  item(i) { return this._a[i] || null; }
  [Symbol.iterator]() { return this._a[Symbol.iterator](); }
}
class Text_ {
  constructor(s) { this.nodeType = 3; this.textContent = s; this.parentNode = null; this.children = []; }
  _m() { return false; }
  _md() { return false; }
  remove() { if (this.parentNode) { this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); this.parentNode = null; } }
}
class El {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null;
    this.className = ""; this.attrs = {}; this.l = {}; this.style = {};
    const self = this;
    this.classList = {
      contains: (c) => self.className.split(/\s+/).includes(c),
      add: (...cs) => { for (const c of cs) if (!self.classList.contains(c)) self.className = (self.className + " " + c).trim(); },
      remove: (...cs) => { self.className = self.className.split(/\s+/).filter((c) => c && !cs.includes(c)).join(" "); },
    };
  }
  // Real connectivity: a reply the suite puts in document.body is connected,
  // a detached one is not — openPopupMenu gates its aria-expanded reset on it.
  get isConnected() {
    let n = this;
    while (n.parentNode) n = n.parentNode;
    return n === document.documentElement;
  }
  append(...nodes) { for (const n of nodes) this.appendChild(n); }
  contains(node) { for (let n = node; n; n = n.parentNode) if (n === this) return true; return false; }
  focus() { document.activeElement = this; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; }
  getBoundingClientRect() { return { left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 }; }
  get childNodes() { return new NodeList_(this.children); }
  get textContent() { return this.children.map((k) => k.textContent).join(""); }
  set textContent(v) {
    for (const c of [...this.children]) c.remove();
    if (v !== "" && v != null) {
      const t = new Text_(String(v));
      t.parentNode = this;
      this.children.push(t);
    }
  }
  get lastElementChild() { return this.children[this.children.length - 1] || null; }
  get previousElementSibling() {
    if (!this.parentNode) return null;
    return this.parentNode.children[this.parentNode.children.indexOf(this) - 1] || null;
  }
  get firstChild() { return this.children[0] || null; }
  appendChild(c) { if (c.parentNode) c.remove(); c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { c.remove(); return c; }
  insertBefore(c, ref) {
    if (c.parentNode) c.remove();
    c.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    return c;
  }
  remove() { if (this.parentNode) { this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); this.parentNode = null; } }
  replaceChildren() { [...this.children].forEach((c) => c.remove()); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(n, f) { (this.l[n] ||= []).push(f); }
  click() { (this.l.click || []).forEach((f) => f({ stopPropagation() {} })); }
  _m(sel) { return sel.split(".").filter(Boolean).every((c) => this.classList.contains(c)); }
  // "a b" is a descendant selector: the last part on the node, the rest on an ancestor.
  _md(sel) {
    const parts = sel.split(/\s+/);
    if (!this._m(parts.pop())) return false;
    for (let n = this.parentNode; parts.length && n; n = n.parentNode) {
      if (n._m && n._m(parts[parts.length - 1])) parts.pop();
    }
    return !parts.length;
  }
  querySelectorAll(sel) {
    const sels = sel.split(",").map((s) => s.trim()), out = [];
    const visit = (n) => { for (const c of n.children) { if (sels.some((s) => c._md(s))) out.push(c); visit(c); } };
    visit(this); return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
// The popup-menu helper (shared with pane.js) needs a body, document-level
// keydown/mousedown listeners, focus tracking and viewport dimensions.
const _docRoot = new El("html");
const _docListeners = {};
globalThis.document = {
  documentElement: _docRoot,
  body: _docRoot,
  activeElement: null,
  createElement: (t) => new El(t),
  addEventListener: (n, f) => { (_docListeners[n] ||= []).push(f); },
  removeEventListener: (n, f) => { _docListeners[n] = (_docListeners[n] || []).filter((x) => x !== f); },
};
function fireDocument(name, event) {
  for (const fn of [...(_docListeners[name] || [])]) fn(event);
}
globalThis.window = { innerWidth: 1200, innerHeight: 800 };
"""

# Fakes for everything the player reaches for.  authFetch answers every clip at
# once (ok + blob) unless `fetchMode` says otherwise; Audio never fires "ended",
# so a started run parks on its first clip, which is what the tests want to
# inspect.  `authGen` is the knob behind authGeneration().
STUBS = r"""
const log = { toasts: [], fetches: [], announce: [], audios: [], revoked: [] };
let authGen = 0, fetchMode = "ok";
// Speaking-style probe: "off" (instructions unsupported), "ok" (three menu
// styles + one hidden), "empty" (supported but nothing in the menu) or "down".
let stylesMode = "off";
// Playback rate the styles probe reports; undefined omits the field (pre-rate server).
let stubRate;
// Read-aloud profile the styles probe reports; undefined omits the field
// (pre-profile server), which must read as the module's built-in defaults.
let stubProfile;
// audio.tts_stream as the styles probe reports it; undefined omits the field
// (pre-streaming server), which the module must read as off.
let stubStream;
const _store = new Map();
let storageGetThrows = false, storageSetThrows = false;
globalThis.localStorage = {
  getItem(k) { if (storageGetThrows) throw new Error("get blocked"); return _store.has(k) ? _store.get(k) : null; },
  setItem(k, v) { if (storageSetThrows) throw new Error("set blocked"); _store.set(k, String(v)); },
  removeItem(k) { if (storageSetThrows) throw new Error("remove blocked"); _store.delete(k); },
};
const authGeneration = () => authGen;
const makeAnnouncer = () => (msg) => { log.announce.push(msg); };
const tick = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
function showToast(msg, kind) { log.toasts.push([msg, kind]); }
URL.createObjectURL = () => "blob:" + log.fetches.length;
URL.revokeObjectURL = (u) => { log.revoked.push(u); };
globalThis.Audio = class {
  constructor() { this.paused = false; this.l = {}; log.audios.push(this); }
  addEventListener(n, f) { (this.l[n] ||= new Set()).add(f); }
  removeEventListener(n, f) { if (this.l[n]) this.l[n].delete(f); }
  pause() { this.paused = true; }
  play() { return Promise.resolve(); }
  fire(n) { [...(this.l[n] || [])].forEach((f) => f()); }
};
function authFetch(url, opts) {
  const body = opts && opts.body ? JSON.parse(opts.body) : null;
  const f = { url, method: opts && opts.method, body, raw: opts && opts.body, text: body ? body.text : null };
  log.fetches.push(f);
  if (fetchMode === "down") return Promise.reject(new Error("down"));
  if (url.endsWith("/v1/api/tts/styles")) {
    if (stylesMode === "down") return Promise.reject(new Error("down"));
    return _stylesResponse().then((r) => ({ ok: r.ok, json: async () => {
      const d = await r.json();
      const extra = {};
      if (stubRate !== undefined) extra.playback_rate = stubRate;
      if (stubProfile !== undefined) extra.profile = stubProfile;
      if (stubStream !== undefined) extra.stream = stubStream;
      return { ...d, ...extra };
    } }));
  }
  if (url.endsWith("/v1/api/models")) {
    return Promise.resolve({ ok: true, json: async () => ({ tts_default_alias: fetchMode === "tts" ? "tts-1" : undefined, stt_default_alias: "stt-1" }) });
  }
  return Promise.resolve({ ok: true, blob: async () => "audio" });
}
function _stylesResponse() {
  {
    if (stylesMode === "empty") {
      return Promise.resolve({ ok: true, json: async () => ({ styles: [{ name: "Hidden", in_menu: false }], default: "", instructions_supported: true }) });
    }
    if (stylesMode === "ok") {
      return Promise.resolve({ ok: true, json: async () => ({
        styles: [
          { name: "Professional", in_menu: true },
          { name: "Social", in_menu: true },
          { name: "Plain", in_menu: true },
          { name: "Hidden", in_menu: false },
        ],
        default: "Professional",
        instructions_supported: true,
      }) });
    }
    // The default for suites that predate the picker: a usable response with
    // instructions unsupported, so addTtsAction adds no style button.
    return Promise.resolve({ ok: true, json: async () => ({ styles: [], default: "", instructions_supported: false }) });
  }
}
// A finished reply: a bubble with a .msg-body holding one paragraph of text.
function reply(text) {
  const el = new El("div"); el.className = "msg assistant";
  const body = new El("div"); body.className = "msg-body";
  const p = new El("p"); p.appendChild(new Text_(text || "This is the reply that gets read aloud."));
  body.appendChild(p); el.appendChild(body);
  return el;
}
"""
