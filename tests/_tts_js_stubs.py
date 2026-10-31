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
    this.className = ""; this.attrs = {}; this.l = {};
    const self = this;
    this.classList = {
      contains: (c) => self.className.split(/\s+/).includes(c),
      add: (...cs) => { for (const c of cs) if (!self.classList.contains(c)) self.className = (self.className + " " + c).trim(); },
      remove: (...cs) => { self.className = self.className.split(/\s+/).filter((c) => c && !cs.includes(c)).join(" "); },
    };
  }
  get childNodes() { return new NodeList_(this.children); }
  get textContent() { return this.children.map((k) => k.textContent).join(""); }
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
globalThis.document = { createElement: (t) => new El(t) };
"""

# Fakes for everything the player reaches for.  authFetch answers every clip at
# once (ok + blob) unless `fetchMode` says otherwise; Audio never fires "ended",
# so a started run parks on its first clip, which is what the tests want to
# inspect.  `authGen` is the knob behind authGeneration().
STUBS = r"""
const log = { toasts: [], fetches: [], announce: [], audios: [], revoked: [] };
let authGen = 0, fetchMode = "ok";
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
  const f = { url, method: opts && opts.method, text: opts && opts.body ? JSON.parse(opts.body).text : null };
  log.fetches.push(f);
  if (fetchMode === "down") return Promise.reject(new Error("down"));
  if (url.endsWith("/v1/api/models")) {
    return Promise.resolve({ ok: true, json: async () => ({ tts_default_alias: fetchMode === "tts" ? "tts-1" : undefined, stt_default_alias: "stt-1" }) });
  }
  return Promise.resolve({ ok: true, blob: async () => "audio" });
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
