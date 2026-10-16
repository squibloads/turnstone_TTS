"""Read-aloud speaks a reply as block-level segments (interactive.js).

``_extractSpeechSegments`` turns the rendered message body into
``{text, pauseMs}`` rows; ``_playMessageTTS`` fetches and plays them one at a
time.  Both are sliced out of the real source and evaluated in node against a
small HTML-parsing stub DOM (the shared ``FakeElement`` has no text nodes) and
controllable ``authFetch`` / ``Audio`` / timer fakes, so the tests drive the
shipped methods rather than a copy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source

pytestmark = node_skip

_SRC = (
    Path(__file__).resolve().parents[1] / "turnstone" / "shared_static" / "interactive.js"
).read_text(encoding="utf-8")

_METHODS = ",\n".join(
    extract_braced(_SRC, sig)
    for sig in (
        "_extractSpeechSegments(bodyEl) {",
        "_shapeSpeechSegments(raw) {",
        "async _playMessageTTS(el, btn) {",
        "_stopTTS() {",
    )
)

# Just enough DOM for the walker: element/text nodes, classList.contains and
# an HTML-subset parser so the cases below read as the markup they model.
_DOM = r"""
class Node_ {
  constructor(nodeType) { this.nodeType = nodeType; this.childNodes = []; }
}
function el(tag, cls) {
  const n = new Node_(1);
  n.tagName = tag.toUpperCase();
  const names = (cls || "").split(/\s+/).filter(Boolean);
  n.classList = { contains: (c) => names.includes(c) };
  n.querySelector = () => null;
  return n;
}
function text(s) { const n = new Node_(3); n.textContent = s; return n; }
function parse(html, cls) {
  const root = el("div", cls);
  const stack = [root];
  const re = /<(\/?)([a-z0-9]+)([^>]*)>|([^<]+)/gi;
  let m;
  while ((m = re.exec(html))) {
    const top = stack[stack.length - 1];
    if (m[4] !== undefined) top.childNodes.push(text(m[4]));
    else if (m[1]) stack.pop();
    else {
      const c = /class="([^"]*)"/.exec(m[3]);
      const n = el(m[2], c ? c[1] : "");
      top.childNodes.push(n);
      stack.push(n);
    }
  }
  return root;
}
"""

_PLAYER = r"""
const log = { announce: 0, toasts: [], created: [], revoked: [], timers: [], audios: [], fetches: [] };
let inFlight = 0, maxInFlight = 0, playMode = "ok";
const tick = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
function voiceAnnounce() { log.announce++; }
function showToast(msg, kind) { log.toasts.push([msg, kind]); }
URL.createObjectURL = (b) => { const u = "blob:" + log.created.length; log.created.push(u); return u; };
URL.revokeObjectURL = (u) => { log.revoked.push(u); };
globalThis.setTimeout = (fn, ms) => { const t = { fn, ms, cleared: false }; log.timers.push(t); return t; };
globalThis.clearTimeout = (t) => { if (t) t.cleared = true; };
globalThis.Audio = class {
  constructor(url) { this.src = url; this.paused = false; this.l = {}; log.audios.push(this); }
  addEventListener(n, f) { this.l[n] = f; }
  pause() { this.paused = true; }
  play() { return playMode === "ok" ? Promise.resolve() : Promise.reject(new Error("NotAllowedError")); }
  fire(n) { this.l[n](); }
};
// Manual fetch: each call parks until the test settles it; honours abort.
function authFetch(url, opts) {
  inFlight++; maxInFlight = Math.max(maxInFlight, inFlight);
  return new Promise((resolve, reject) => {
    const f = { url, opts, text: JSON.parse(opts.body).text, done: false };
    const finish = (fn, v) => { if (f.done) return; f.done = true; inFlight--; fn(v); };
    f.reply = () => finish(resolve, { ok: true, blob: async () => "audio" });
    f.refuse = (error) => finish(resolve, { ok: false, json: async () => ({ error }) });
    f.fail = (msg) => finish(reject, new Error(msg));
    opts.signal.addEventListener("abort", () => finish(reject, new Error("aborted")));
    log.fetches.push(f);
  });
}
function button() {
  const cls = new Set(), attrs = {};
  return {
    disabled: false, attrs, cls,
    classList: { add: (c) => cls.add(c), remove: (...cs) => cs.forEach((c) => cls.delete(c)), contains: (c) => cls.has(c) },
    setAttribute: (k, v) => { attrs[k] = v; },
  };
}
const T = {
  _base: "http://x", _ttsAudio: null, _ttsAbort: null, _ttsTimer: null, _ttsUrls: new Set(), _ttsBtnActive: null, _ttsSeq: 0,
  METHODS
};
// Three paragraphs long enough to avoid the short-segment merge.
const msg = () => parse(
  "<p>First paragraph is long enough.</p><p>Second paragraph is long enough.</p><p>Third paragraph is long enough.</p>",
  "msg-body");
"""


def _run(prelude: str, body: str) -> Any:
    source = f"{prelude}\n(async () => {{\n{body}\n}})().catch((e) => {{ console.error(e); process.exit(1); }});"
    result = run_node_source(source)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _extract(*htmls: str) -> list[list[dict[str, Any]]]:
    prelude = _DOM + "const T = {\n" + _METHODS + "\n};\n"
    body = f"console.log(JSON.stringify({json.dumps(list(htmls))}.map((h) => T._extractSpeechSegments(parse(h)))));"
    return _run(prelude, body)


def _segs(html: str) -> list[tuple[str, int]]:
    return [(s["text"], s["pauseMs"]) for s in _extract(html)[0]]


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


def test_headings_and_paragraphs_become_segments_with_pauses() -> None:
    assert _segs(
        "<h1>Title of the document</h1><p>First paragraph is here.</p>"
        "<p>Second one ends without a period</p>"
    ) == [
        ("Title of the document.", 600),
        ("First paragraph is here.", 500),
        ("Second one ends without a period.", 0),
    ]


def test_existing_terminal_punctuation_is_not_doubled() -> None:
    assert _segs(
        "<p>What is going on over here?</p><p>Note the following items:</p>"
        '<p>Well that was something!</p><p>And then it trailed off…</p><p>He said "stop."</p>'
    ) == [
        ("What is going on over here?", 500),
        ("Note the following items:", 500),
        ("Well that was something!", 500),
        ("And then it trailed off…", 500),
        ('He said "stop."', 0),
    ]


def test_list_items_are_separate_segments_with_item_pause() -> None:
    assert _segs(
        "<ul><li>First item in the list</li><li>Second item in the list</li></ul>"
        "<p>Closing paragraph for the list.</p>"
    ) == [
        ("First item in the list.", 300),
        ("Second item in the list.", 300),
        ("Closing paragraph for the list.", 0),
    ]


def test_nested_list_items_become_their_own_segments() -> None:
    assert _segs(
        "<ul><li>Parent item text here<ul><li>Child item one is here</li>"
        "<li>Child item two is here</li></ul></li><li>Sibling item text here</li></ul>"
    ) == [
        ("Parent item text here.", 300),
        ("Child item one is here.", 300),
        ("Child item two is here.", 300),
        ("Sibling item text here.", 0),
    ]


def test_table_rows_join_cells_with_commas() -> None:
    assert _segs(
        "<table><thead><tr><th>Column name</th><th>Column value</th></tr></thead>"
        "<tbody><tr><td>alpha first</td><td>beta second</td></tr></tbody></table>"
    ) == [("Column name, Column value.", 300), ("alpha first, beta second.", 0)]


def test_blockquote_and_definition_lists_are_segments() -> None:
    assert _segs(
        "<blockquote>A quoted sentence of some length.</blockquote>"
        "<dl><dt>The term being defined</dt><dd>Its definition goes right here.</dd></dl>"
    ) == [
        ("A quoted sentence of some length.", 500),
        ("The term being defined.", 300),
        ("Its definition goes right here.", 0),
    ]


def test_fenced_code_is_one_omitted_segment_but_inline_code_is_spoken() -> None:
    assert _segs(
        "<p>Run this command now:</p><pre><code>ls -la\nrm -rf /</code></pre>"
        "<p>Call the <code>foo_bar</code> function afterwards.</p>"
    ) == [
        ("Run this command now:", 500),
        ("Code omitted.", 400),
        ("Call the foo_bar function afterwards.", 0),
    ]


def test_katex_is_removed() -> None:
    assert _segs(
        '<p>The value <span class="katex"><span>x squared annotation</span></span> is quite large.</p>'
        '<div class="katex-display"><span class="katex">E equals m c squared</span></div>'
        "<p>Done with the math now.</p>"
    ) == [("The value is quite large.", 500), ("Done with the math now.", 0)]


def test_inline_markup_and_whitespace_are_flattened() -> None:
    assert _segs("<p>  Some <strong>bold</strong>\n and   <em>italic</em>\n text   here  </p>") == [
        ("Some bold and italic text here.", 0)
    ]


def test_short_segment_merges_into_the_following_one() -> None:
    assert _segs("<h2>Summary</h2><p>This paragraph is long enough to stand.</p>") == [
        ("Summary. This paragraph is long enough to stand.", 0)
    ]
    # A chain of short segments collapses into the first long one.
    assert _segs("<p>One.</p><p>Two.</p><p>Three is rather a lot longer.</p>") == [
        ("One. Two. Three is rather a lot longer.", 0)
    ]


def test_short_final_segment_and_code_marker_stay_alone() -> None:
    assert _segs("<p>This paragraph is long enough.</p><p>Short.</p>") == [
        ("This paragraph is long enough.", 500),
        ("Short.", 0),
    ]
    assert _segs("<p>Hi.</p><pre>x</pre><p>Long enough paragraph follows.</p>") == [
        ("Hi.", 500),
        ("Code omitted.", 400),
        ("Long enough paragraph follows.", 0),
    ]


def test_long_segment_splits_at_sentence_boundaries() -> None:
    para = " ".join(["x" * 99 + "."] * 10)  # 1009 chars: two pieces of five sentences
    rows = _extract(f"<p>{para}</p><p>A trailing paragraph of ordinary size.</p>")[0]
    assert len(rows) == 3
    assert all(len(r["text"]) <= 600 for r in rows[:2])
    assert " ".join(r["text"] for r in rows[:2]) == para
    # Only the last piece carries the paragraph's pause.
    assert [r["pauseMs"] for r in rows] == [0, 500, 0]


def test_a_single_sentence_over_the_limit_stays_whole() -> None:
    word_run = " ".join(["word"] * 200) + "."  # 1000 chars, no inner boundary
    rows = _extract(f"<p>{word_run}</p>")[0]
    assert [r["text"] for r in rows] == [word_run]


def test_plain_text_body_falls_back_to_one_flattened_segment() -> None:
    assert _segs("  hello   world \n there ") == [("hello world there", 0)]
    assert _segs("Plain <b>bold</b> words, no blocks") == [("Plain bold words, no blocks", 0)]


def test_empty_bodies_yield_no_segments() -> None:
    assert _extract("", "   ", "<p> </p><ul></ul>", '<span class="katex">x</span>') == [
        [],
        [],
        [],
        [],
    ]


# --------------------------------------------------------------------------
# Player
# --------------------------------------------------------------------------


def _play(body: str) -> Any:
    return _run(_DOM + _PLAYER.replace("METHODS", _METHODS), body)


def test_clips_play_in_order_with_one_fetch_in_flight_and_a_pause_between() -> None:
    out = _play(
        """
const btn = button();
T._playMessageTTS(msg(), btn);
await tick();
const s0 = { fetches: log.fetches.length, busy: btn.cls.has("is-busy"), disabled: btn.disabled };
log.fetches[0].reply(); await tick();
const s1 = { fetches: log.fetches.length, audios: log.audios.length,
  playing: btn.cls.has("is-playing"), busy: btn.cls.has("is-busy"),
  pressed: btn.attrs["aria-pressed"], announce: log.announce };
log.audios[0].fire("ended"); await tick();
// Clip 0 ended: its URL is gone, the 500 ms pause is pending, clip 1 has not started.
const s2 = { audios: log.audios.length, timers: log.timers.map((t) => t.ms), revoked: [...log.revoked] };
log.timers[0].fn(); await tick();
// Pause over but fetch 1 still outstanding: wait for it.
const s3 = { audios: log.audios.length };
log.fetches[1].reply(); await tick();
const s4 = { audios: log.audios.length, fetches: log.fetches.length, announce: log.announce };
log.audios[1].fire("ended"); await tick(); log.timers[1].fn(); await tick();
log.fetches[2].reply(); await tick();
log.audios[2].fire("ended"); await tick();
console.log(JSON.stringify({ s0, s1, s2, s3, s4, maxInFlight, timers: log.timers.map((t) => t.ms),
  texts: log.fetches.map((f) => f.text), url: log.fetches[0].url, method: log.fetches[0].opts.method,
  headers: log.fetches[0].opts.headers, revoked: log.revoked, announce: log.announce,
  end: { playing: btn.cls.has("is-playing"), busy: btn.cls.has("is-busy"), disabled: btn.disabled,
         pressed: btn.attrs["aria-pressed"], active: T._ttsBtnActive }, toasts: log.toasts }));
"""
    )
    assert out["s0"] == {"fetches": 1, "busy": True, "disabled": True}
    # Clip 1 is prefetched as soon as clip 0 starts, never earlier.
    assert out["s1"] == {
        "fetches": 2,
        "audios": 1,
        "playing": True,
        "busy": False,
        "pressed": "true",
        "announce": 1,
    }
    assert out["s2"] == {"audios": 1, "timers": [500], "revoked": ["blob:0"]}
    assert out["s3"] == {"audios": 1}
    assert out["s4"] == {"audios": 2, "fetches": 3, "announce": 1}
    assert out["maxInFlight"] == 1
    assert out["timers"] == [500, 500]  # the last segment has no trailing pause
    assert out["texts"] == [
        "First paragraph is long enough.",
        "Second paragraph is long enough.",
        "Third paragraph is long enough.",
    ]
    assert out["url"] == "http://x/v1/api/tts"
    assert out["method"] == "POST"
    assert out["headers"] == {"Content-Type": "application/json"}
    assert out["revoked"] == ["blob:0", "blob:1", "blob:2"]
    assert out["end"] == {
        "playing": False,
        "busy": False,
        "disabled": False,
        "pressed": "false",
        "active": None,
    }
    assert out["toasts"] == []


def test_stop_during_playback_cancels_fetch_and_revokes_urls() -> None:
    out = _play(
        """
const btn = button();
T._playMessageTTS(msg(), btn);
await tick(); log.fetches[0].reply(); await tick();
const pending = log.fetches[1];
T._stopTTS();
await tick();
pending.reply(); await tick();
console.log(JSON.stringify({ paused: log.audios[0].paused, aborted: pending.opts.signal.aborted,
  audios: log.audios.length, fetches: log.fetches.length, created: log.created, revoked: log.revoked,
  playing: btn.cls.has("is-playing"), pressed: btn.attrs["aria-pressed"], toasts: log.toasts,
  inFlight }));
"""
    )
    assert out == {
        "paused": True,
        "aborted": True,
        "audios": 1,
        "fetches": 2,
        "created": ["blob:0"],
        "revoked": ["blob:0"],
        "playing": False,
        "pressed": "false",
        "toasts": [],
        "inFlight": 0,
    }


def test_stop_during_a_pause_cancels_the_timer_and_the_next_clip() -> None:
    out = _play(
        """
const btn = button();
T._playMessageTTS(msg(), btn);
await tick(); log.fetches[0].reply(); await tick();
log.fetches[1].reply(); await tick();
log.audios[0].fire("ended"); await tick();
const timer = log.timers[0];
T._stopTTS();
timer.fn(); await tick();
console.log(JSON.stringify({ cleared: timer.cleared, audios: log.audios.length,
  fetches: log.fetches.length, created: log.created, revoked: log.revoked }));
"""
    )
    assert out["cleared"] is True
    assert out["audios"] == 1
    assert out["fetches"] == 2
    # Clip 1 was fetched (URL created) before the stop and must still be revoked.
    assert sorted(out["revoked"]) == sorted(out["created"])


def test_a_superseded_run_is_ignored_and_the_active_button_toggles_off() -> None:
    out = _play(
        """
const a = button(), b = button();
T._playMessageTTS(msg(), a);
await tick();
const stale = log.fetches[0];
T._playMessageTTS(msg(), b);            // another button supersedes run A
await tick();
stale.reply(); await tick();             // A's late reply must not play or leak
const afterStale = { audios: log.audios.length, created: log.created.length,
  aBusy: a.cls.has("is-busy"), aDisabled: a.disabled, bBusy: b.cls.has("is-busy") };
log.fetches[1].reply(); await tick();
T._playMessageTTS(msg(), b);            // clicking the active button stops
await tick();
console.log(JSON.stringify({ afterStale, audios: log.audios.length, fetches: log.fetches.length,
  active: T._ttsBtnActive, paused: log.audios[0].paused, revoked: log.revoked }));
"""
    )
    assert out["afterStale"] == {
        "audios": 0,
        "created": 0,
        "aBusy": False,
        "aDisabled": False,
        "bBusy": True,
    }
    assert out["audios"] == 1
    assert out["paused"] is True
    assert out["fetches"] == 3  # b's two clips; the toggle-off click started nothing new
    assert out["active"] is None
    assert out["revoked"] == ["blob:0"]


def test_failed_fetches_stop_playback_and_show_a_toast() -> None:
    out = _play(
        """
const res = {};
const btn = button();
T._playMessageTTS(msg(), btn); await tick();
log.fetches[0].refuse("TTS model is not loaded"); await tick();
res.refused = { toasts: [...log.toasts], busy: btn.cls.has("is-busy"), disabled: btn.disabled,
  active: T._ttsBtnActive };
log.toasts.length = 0;
T._playMessageTTS(msg(), btn); await tick();
log.fetches[1].reply(); await tick();             // clip 0 plays, clip 1 is prefetched
log.fetches[2].fail("network down"); await tick();
// The failure is held until the next clip is due: the current clip is not cut off.
res.during = { toasts: log.toasts.length, paused: log.audios[0].paused };
log.audios[0].fire("ended"); await tick(); log.timers[0].fn(); await tick();
res.network = { toasts: [...log.toasts], paused: log.audios[0].paused, active: T._ttsBtnActive,
  playing: btn.cls.has("is-playing") };
console.log(JSON.stringify(res));
"""
    )
    assert out["refused"] == {
        "toasts": [["TTS model is not loaded", "error"]],
        "busy": False,
        "disabled": False,
        "active": None,
    }
    assert out["during"] == {"toasts": 0, "paused": False}
    assert out["network"] == {
        "toasts": [["network down", "error"]],
        "paused": True,
        "active": None,
        "playing": False,
    }


def test_rejected_play_shows_a_toast_and_stops() -> None:
    out = _play(
        """
playMode = "reject";
const btn = button();
T._playMessageTTS(msg(), btn); await tick();
log.fetches[0].reply(); await tick();
console.log(JSON.stringify({ toasts: log.toasts, created: log.created, revoked: log.revoked,
  fetches: log.fetches.length, playing: btn.cls.has("is-playing"), active: T._ttsBtnActive }));
"""
    )
    assert out["toasts"] == [["Playback was blocked by the browser", "error"]]
    assert out["revoked"] == out["created"] == ["blob:0"]
    assert out["fetches"] == 2  # the prefetch was in flight; it is aborted, not awaited
    assert out["playing"] is False
    assert out["active"] is None
