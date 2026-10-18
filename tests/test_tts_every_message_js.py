"""Every finished assistant reply carries its own read-aloud button.

The shipped ``_attachRetryToLastAssistant`` (the busy->idle / replay edge),
``_addTtsActionsToFinished`` and ``_applyVoiceRoles`` run in node against a
small tree DOM (parent/sibling links, ``insertBefore``, class selectors) and
the real ``copy_actions.js`` bar helpers, so the tests cover ordering and
de-duplication as the browser sees them.  The audio player is stubbed: it is
covered by ``test_tts_segments_js``, and here only WHICH reply a click targets
and the single-run toggle matter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import demodulize, extract_braced, node_skip, run_node_source

pytestmark = node_skip

_ROOT = Path(__file__).resolve().parents[1] / "turnstone" / "shared_static"
_SRC = (_ROOT / "interactive.js").read_text(encoding="utf-8")
_COPY = (_ROOT / "copy_actions.js").read_text(encoding="utf-8")


def _method(sig: str) -> str:
    """The shipped method, or a throwing stub when the source lacks it, so a
    test against code that predates the method fails on its assertions."""
    if sig not in _SRC:
        name = sig.split("(")[0].replace("async ", "")
        return f"{name}() {{ throw new Error('missing {name}'); }}"
    return extract_braced(_SRC, sig)


_PANE_METHODS = "\n".join(
    _method(sig)
    for sig in (
        "_attachRetryToLastAssistant() {",
        "_addTtsActionsToFinished() {",
        "_applyVoiceRoles(roles) {",
        "_addTtsAction(el) {",
        "_addRetryAction(el) {",
        "_addCopyAction(el) {",
        "_stopTTS() {",
    )
)
_COPY_FNS = "\n".join(
    extract_braced(_COPY, sig)
    for sig in (
        "export function findMsgActionsBar(el) {",
        "export function ensureMsgActionsBar(el) {",
        "function _icon(cls) {",
        "export function buildMsgRetryButton(onRetry) {",
    )
).replace("export function", "function")

_DOM = r"""
class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null;
    this.className = ""; this.attrs = {}; this.l = {};
    const self = this;
    this.classList = {
      contains: (c) => self.className.split(/\s+/).includes(c),
      add: (...cs) => { for (const c of cs) if (!self.classList.contains(c)) self.className = (self.className + " " + c).trim(); },
      remove: (...cs) => { self.className = self.className.split(/\s+/).filter((c) => c && !cs.includes(c)).join(" "); },
    };
  }
  get lastElementChild() { return this.children[this.children.length - 1] || null; }
  get previousElementSibling() {
    if (!this.parentNode) return null;
    return this.parentNode.children[this.parentNode.children.indexOf(this) - 1] || null;
  }
  get firstChild() { return this.children[0] || null; }
  appendChild(c) { if (c.parentNode) c.remove(); c.parentNode = this; this.children.push(c); return c; }
  insertBefore(c, ref) {
    if (c.parentNode) c.remove();
    c.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    return c;
  }
  remove() { if (this.parentNode) { this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); this.parentNode = null; } }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(n, f) { (this.l[n] ||= []).push(f); }
  click() { (this.l.click || []).forEach((f) => f({ stopPropagation() {} })); }
  _m(sel) { return sel.split(".").filter(Boolean).every((c) => this.classList.contains(c)); }
  querySelectorAll(sel) {
    const sels = sel.split(",").map((s) => s.trim()), out = [];
    const visit = (n) => { for (const c of n.children) { if (sels.some((s) => c._m(s))) out.push(c); visit(c); } };
    visit(this); return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
globalThis.document = { createElement: (t) => new El(t) };
"""

_HELPERS = r"""
function buildMsgCopyButton() { const b = new El("button"); b.className = "msg-action-btn msg-copy-btn"; return b; }
const log = { plays: [] };
class Pane {
  METHODS
}
function mkPane(tts) {
  const T = new Pane();
  T.messagesEl = new El("div");
  T._voiceRoles = { stt: false, tts };
  T._retryHolderEl = null; T.currentAssistantEl = null; T.composer = null; T._micBtn = null;
  T._retryLast = () => {};
  T._ttsBtnActive = null;
  T._ttsPlayer = { stop() { T._ttsBtnActive && T._ttsBtnActive.classList.remove("is-playing"); T._ttsBtnActive = null; } };
  T._playMessageTTS = async function (el, btn) {
    if (this._ttsBtnActive) { const same = this._ttsBtnActive === btn; this._stopTTS(); if (same) return; }
    this._ttsBtnActive = btn; btn.classList.add("is-playing"); log.plays.push(el);
  };
  return T;
}
function bubble(T, cls) {
  const el = new El("div"); el.className = "msg " + (cls || "assistant");
  const body = new El("div"); body.className = "msg-body"; el.appendChild(body);
  if ((cls || "assistant") === "assistant") T._addCopyAction(el);
  T.messagesEl.appendChild(el); return el;
}
const names = (el) => el.children.filter((c) => c.className.includes("msg-actions"))
  .flatMap((bar) => bar.children.map((b) => b.className.split(" ").find((c) => /^msg-(retry|tts|copy)-btn$/.test(c)).slice(4, -4)));
"""

# The speaker walk lives in tts_player.js (the pane methods delegate to it);
# its copy_actions imports resolve to the bar helpers sliced above.
_PLAYER_MODULE = demodulize(_ROOT / "tts_player.js")

_PRELUDE = _DOM + _COPY_FNS + _PLAYER_MODULE + _HELPERS.replace("METHODS", _PANE_METHODS)


def _run(body: str) -> Any:
    src = _PRELUDE + f"\n(async () => {{\n{body}\n}})().catch((e) => {{ console.error(e); process.exit(1); }});"
    r = run_node_source(src)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_every_finished_reply_gets_one_speaker_and_retry_stays_on_the_last() -> None:
    out = _run(
        """
const T = mkPane(true);
const a = bubble(T), u = bubble(T, "user"), b = bubble(T), c = bubble(T);
T._attachRetryToLastAssistant();
T._attachRetryToLastAssistant();  // repeated settle edge: no duplicates
console.log(JSON.stringify([names(a), names(b), names(c), names(u)]));
"""
    )
    assert out == [["tts", "copy"], ["tts", "copy"], ["retry", "tts", "copy"], []]


def test_a_streaming_reply_has_no_speaker_until_it_settles() -> None:
    out = _run(
        """
const T = mkPane(true);
const a = bubble(T), live = bubble(T);
T.currentAssistantEl = live;           // still streaming
T._attachRetryToLastAssistant();
const during = [names(a), names(live)];
T.currentAssistantEl = null;           // stream_end + idle
T._attachRetryToLastAssistant();
console.log(JSON.stringify([during, names(live)]));
"""
    )
    assert out == [[["tts", "copy"], ["retry", "copy"]], ["retry", "tts", "copy"]]


def test_roles_resolving_late_backfill_existing_replies_and_false_adds_none() -> None:
    out = _run(
        """
const T = mkPane(false), F = mkPane(false);
const bs = [bubble(T), bubble(T), bubble(T)];
bubble(F);
T._attachRetryToLastAssistant();
const before = bs.map(names);
T._applyVoiceRoles({ stt: false, tts: true });
T._applyVoiceRoles({ stt: false, tts: true });  // second resolve: no duplicates
F._applyVoiceRoles({ stt: true, tts: false });
console.log(JSON.stringify([before, bs.map(names), F.messagesEl.children.map(names)]));
"""
    )
    assert out == [
        [["copy"], ["copy"], ["retry", "copy"]],
        [["tts", "copy"], ["tts", "copy"], ["retry", "tts", "copy"]],
        [["copy"]],
    ]


def test_moving_retry_keeps_earlier_speakers_and_does_not_stop_playback() -> None:
    out = _run(
        """
const T = mkPane(true);
const a = bubble(T), b = bubble(T);
T._attachRetryToLastAssistant();
const abtn = b.querySelector(".msg-tts-btn");        // b, the retry holder, is playing
abtn.click();
const c = bubble(T);                                  // a later turn settles
T._attachRetryToLastAssistant();
const kept = [names(a), names(b), names(c), abtn.classList.contains("is-playing") && b.querySelector(".msg-tts-btn") === abtn, T._ttsBtnActive === abtn];
c.querySelector(".msg-tts-btn").click();              // switch to c
console.log(JSON.stringify([kept, abtn.classList.contains("is-playing"),
  c.querySelector(".msg-tts-btn").classList.contains("is-playing"), log.plays.length]));
"""
    )
    kept, b_playing, c_playing, plays = out
    assert kept == [["tts", "copy"], ["tts", "copy"], ["retry", "tts", "copy"], True, True]
    assert (b_playing, c_playing, plays) == (False, True, 2)


def test_tool_only_tail_still_speaks_finished_replies_without_retry() -> None:
    out = _run(
        """
const T = mkPane(true);
const a = bubble(T);
const batch = new El("div"); batch.className = "conv-batch"; T.messagesEl.appendChild(batch);
T._attachRetryToLastAssistant();
console.log(JSON.stringify(names(a)));
"""
    )
    assert out == ["tts", "copy"]
