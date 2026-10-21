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

from tests._js_harness_helpers import (
    demodulize,
    extract_braced,
    node_skip,
    run_node_source,
    strip_js_comments,
)

pytestmark = node_skip

_SRC = (
    Path(__file__).resolve().parents[1] / "turnstone" / "shared_static" / "interactive.js"
).read_text(encoding="utf-8")

# The speech pipeline lives in tts_player.js; the pane methods sliced below are
# thin delegates to it, so the module is evaluated in the same context (imports
# stripped: authFetch, Audio, showToast ... are the globals the stubs below set).
_PLAYER_MODULE = demodulize(
    Path(__file__).resolve().parents[1] / "turnstone" / "shared_static" / "tts_player.js"
)

_SIGS = (
    "_extractSpeechSegments(bodyEl) {",
    "_shapeSpeechSegments(raw) {",
    "async _playMessageTTS(el, btn) {",
    "_stopTTS() {",
)
_METHODS = ",\n".join(extract_braced(_SRC, sig) for sig in _SIGS)  # object-literal body
# The real constructor and the lifecycle methods that call _stopTTS() (reset,
# replayHistory; disconnectSSE deliberately does not); the
# player tests build their pane from these so a field the constructor fails to
# initialise breaks them, as it blanked live transcripts on 2026-10-16.
_PANE_SRC = "\n".join(
    extract_braced(_SRC, sig)
    for sig in (
        "constructor(wsId, opts) {",
        "reset() {",
        "disconnectSSE() {",
        "replayHistory(messages) {",
        *_SIGS,
    )
)

# Just enough DOM for the walker: element/text nodes, classList.contains and
# an HTML-subset parser so the cases below read as the markup they model.
_DOM = _PLAYER_MODULE + r"""
// childNodes is NodeList-like as in browsers: iterable, length, item(), and NO
// array methods (no reduce/map/filter), so code that treats it as an Array
// fails here as it does in Chrome.  The parser appends through _kids.
class NodeList_ {
  constructor(a) { this._a = a; }
  get length() { return this._a.length; }
  item(i) { return this._a[i] || null; }
  [Symbol.iterator]() { return this._a[Symbol.iterator](); }
}
class Node_ {
  constructor(nodeType) {
    this.nodeType = nodeType;
    this._kids = [];
    this.childNodes = new NodeList_(this._kids);
  }
  get textContent() {
    return this.nodeType === 3 ? this._text : this._kids.map((k) => k.textContent).join("");
  }
  set textContent(v) { this._text = v; }
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
    if (m[4] !== undefined) top._kids.push(text(m[4]));
    else if (m[1]) stack.pop();
    else {
      const c = /class="([^"]*)"/.exec(m[3]);
      const n = el(m[2], c ? c[1] : "");
      top._kids.push(n);
      stack.push(n);
    }
  }
  return root;
}
"""

_PLAYER = r"""
const log = { announce: 0, toasts: [], created: [], revoked: [], timers: [], audios: [], srcs: [], fetches: [] };
let authGen = 0;
const authGeneration = () => authGen;
let inFlight = 0, maxInFlight = 0, playMode = "ok", ignoreAbort = false;
const tick = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
function voiceAnnounce() { log.announce++; }
function showToast(msg, kind) { log.toasts.push([msg, kind]); }
URL.createObjectURL = (b) => { const u = "blob:" + log.created.length; log.created.push(u); return u; };
URL.revokeObjectURL = (u) => { log.revoked.push(u); };
globalThis.setTimeout = (fn, ms) => { const t = { fn, ms, cleared: false }; log.timers.push(t); return t; };
globalThis.clearTimeout = (t) => { if (t) t.cleared = true; };
globalThis.Audio = class {
  constructor(url) {
    this.paused = false; this.l = {}; log.audios.push(this);
    if (url !== undefined) this.src = url;
  }
  get src() { return this._src; }
  set src(v) { this._src = v; log.srcs.push(v); }
  addEventListener(n, f) { (this.l[n] ||= new Set()).add(f); }
  removeEventListener(n, f) { if (this.l[n]) this.l[n].delete(f); }
  listeners() { return Object.values(this.l).reduce((a, set) => a + set.size, 0); }
  pause() { this.paused = true; }
  play() {
    if (playMode === "ok") return Promise.resolve();
    // playMode doubles as the DOMException name the browser would reject with.
    return Promise.reject(Object.assign(new Error("play() failed"), { name: playMode }));
  }
  fire(n) { [...(this.l[n] || [])].forEach((f) => f()); }
};
// Manual fetch: each call parks until the test settles it; honours abort unless
// ignoreAbort is set, which models a reply already in flight when the run is
// superseded (the response still resolves after the abort).
function authFetch(url, opts) {
  inFlight++; maxInFlight = Math.max(maxInFlight, inFlight);
  return new Promise((resolve, reject) => {
    const f = { url, opts, text: JSON.parse(opts.body).text, done: false };
    const finish = (fn, v) => { if (f.done) return; f.done = true; inFlight--; fn(v); };
    f.reply = () => finish(resolve, { ok: true, blob: async () => "audio" });
    f.refuse = (error) => finish(resolve, { ok: false, json: async () => ({ error }) });
    f.fail = (msg) => finish(reject, new Error(msg));
    // An HTML error page from a proxy in front (a load balancer's 504): no JSON body.
    f.page = () => finish(resolve, { ok: false, json: async () => { throw new SyntaxError("Unexpected token <"); } });
    // A JSON error whose body the test releases later, so a run can be replaced
    // while the player is still reading it.
    f.slowRefuse = (error) => {
      let release;
      const body = new Promise((r) => { release = () => r({ error }); });
      finish(resolve, { ok: false, json: () => body });
      return release;
    };
    opts.signal.addEventListener("abort", () => { if (!ignoreAbort) finish(reject, new Error("aborted")); });
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
// Only what the real constructor, reset(), disconnectSSE() and replayHistory() reach for is
// stubbed; every _tts* field comes from the constructor under test.
let _paneCounter = 0;
const INTERACTIVE_DEFAULT_HOST = {};
const createHistoryHandoffRepair = () => ({});
const createReasoningActivity = () => ({ finish() {} });
const STALE_RETRY_BASE_MS = 0, STALE_RETRY_JITTER_MS = 0, DEGRADED_COOLDOWN_MAX_MS = 0, DEGRADED_COOLDOWN_BASE_MS = 0;
class Pane {
  PANE
  _createDOM() { this.inputEl = {}; this.attachments = { clearChips() {} }; }
  setBusy() {}
  _reconcileSendDisabled() {}
  _stopRecording() {}
  showEmptyState() {}
  _resetStreamingRefs() {}
  _clearAgentTracking() {}
}
const T = new Pane("ws", {});
T._base = "http://x";
// The player state lives in the pane's tts_player instance; these keep the
// assertions below reading it under the names they were written against.
Object.defineProperties(T, {
  _ttsBtnActive: { get: () => T._ttsPlayer.activeButton() },
  _ttsAudio: { get: () => T._ttsPlayer.audio() },
});
T.messagesEl = { scrollTop: 0, replaceChildren() {}, setAttribute() {} };
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


_T_PRELUDE = "const T = {\n" + _METHODS + "\n};\n"


def _extract(*htmls: str) -> list[list[dict[str, Any]]]:
    prelude = _DOM + _T_PRELUDE
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


def test_a_list_plays_as_one_clip_with_no_pause_after_it() -> None:
    assert _segs(
        "<h2>Colors</h2><ul><li>Red</li><li>Blue</li><li>Green</li></ul>"
        "<p>Those are the primary colors.</p>"
    ) == [("Colors. Red. Blue. Green.", 0), ("Those are the primary colors.", 0)]


def test_a_short_lead_in_paragraph_carries_into_the_list_clip() -> None:
    assert _segs(
        "<p>To deploy:</p><ol><li>Pull the image</li><li>Restart the stack</li>"
        "<li>Check the logs</li></ol><p>That's it.</p>"
    ) == [
        ("To deploy: Pull the image. Restart the stack. Check the logs.", 0),
        ("That's it.", 0),
    ]


def test_a_short_lead_in_folds_into_the_first_table_row_only() -> None:
    assert _segs(
        "<p>Choose a plan:</p><table><tr><th>Plan</th><th>Price</th></tr>"
        "<tr><td>Pro</td><td>$9</td></tr></table>"
    ) == [("Choose a plan: Plan, Price.", 150), ("Pro, $9.", 0)]


def test_nested_list_items_join_the_one_list_clip() -> None:
    assert _segs(
        "<ul><li>Parent item text here<ul><li>Child item one is here</li>"
        "<li>Child item two is here</li></ul></li><li>Sibling item text here</li></ul>"
        "<p>A paragraph after the list.</p>"
    ) == [
        (
            "Parent item text here. Child item one is here. Child item two is here."
            " Sibling item text here.",
            0,
        ),
        ("A paragraph after the list.", 0),
    ]


def test_a_long_list_splits_at_item_ends_with_no_pause_between_clips() -> None:
    items = "".join(f"<li>{'x' * 99}</li>" for _ in range(10))
    rows = _extract(f"<ul>{items}</ul><p>A trailing paragraph of ordinary size.</p>")[0]
    assert len(rows) == 3
    assert all(len(r["text"]) <= 600 for r in rows)
    assert " ".join(r["text"] for r in rows[:2]) == " ".join(["x" * 99 + "."] * 10)
    assert [r["pauseMs"] for r in rows] == [0, 0, 0]


def test_a_huge_list_extracts_in_linear_time() -> None:
    """Model output can degenerate into tens of thousands of list items, and
    extraction runs synchronously inside the click.  Each item used to re-scan
    the whole accumulated list (1.5 s at 20,000 items); the time is taken
    inside node, around the extractor alone."""
    n = 20_000
    out = _run(
        _DOM + _T_PRELUDE,
        f"""
const root = parse("<ul>" + "<li>a</li>".repeat({n}) + "</ul>");
const t0 = performance.now();
const segs = T._extractSpeechSegments(root);
const ms = performance.now() - t0;
console.log(JSON.stringify({{ ms, count: segs.length, longest: Math.max(...segs.map((s) => s.text.length)),
  words: segs.map((s) => s.text).join(" ").split(" ").length }}));
""",
    )
    print(f"extract of {n} list items: {out['ms']:.1f} ms")
    assert out["ms"] < 200
    assert out["words"] == n and out["longest"] <= 600


def test_table_rows_join_cells_with_commas() -> None:
    assert _segs(
        "<table><thead><tr><th>Column name</th><th>Column value</th></tr></thead>"
        "<tbody><tr><td>alpha first</td><td>beta second</td></tr></tbody></table>"
    ) == [("Column name, Column value.", 150), ("alpha first, beta second.", 0)]


def test_empty_and_padded_table_cells_do_not_leave_stray_separators() -> None:
    assert _segs(
        "<table><tr><td>first cell</td><td></td><td> </td><td>last cell</td></tr>"
        "<tr><th> Plan </th><th> Price </th></tr></table>"
    ) == [("first cell, last cell.", 150), ("Plan, Price.", 0)]


def test_a_trailing_empty_cell_does_not_leak_a_separator_past_the_row() -> None:
    assert _segs(
        "<table><tr><td>Pro</td><td></td></tr></table>"
        "<ul><li>Red item here</li><li>Blue item here</li></ul>"
    ) == [("Pro.", 150), ("Red item here. Blue item here.", 0)]
    assert _segs(
        "<table><tr><td>Pro</td><td></td></tr></table><div>Trailing div text here</div>"
    ) == [("Pro.", 150), ("Trailing div text here.", 0)]


def test_table_rows_inside_a_list_item_each_start_cleanly() -> None:
    assert _segs(
        "<ul><li>Intro text here.<table><tr><th>Plan</th><th>Price</th></tr>"
        "<tr><td>Pro</td><td>$9</td></tr><tr><td>Max</td><td>$20</td></tr></table>"
        "</li><li>Closing item text</li></ul>"
    ) == [("Intro text here. Plan, Price. Pro, $9. Max, $20. Closing item text.", 0)]


def test_every_long_code_cell_in_a_row_keeps_its_marker() -> None:
    long_ = "x" * 40
    code = f"<td><code>{long_}</code></td>"
    assert _segs(f"<table><tr><td>x</td>{code}{code}<td>y</td></tr></table>") == [
        ("x, Code omitted., Code omitted., y.", 0)
    ]
    assert _segs(f"<table><tr><td>x</td>{code}{code}</tr></table>") == [
        ("x, Code omitted., Code omitted.", 0)
    ]
    # Within one cell two adjacent long spans are still one omission.
    assert _segs(f"<table><tr><td>x</td><td><code>{long_}</code><code>{long_}</code></td></tr></table>") == [
        ("x, Code omitted.", 0)
    ]


def test_short_table_rows_are_never_merged() -> None:
    assert _segs(
        "<table><tr><th>Plan</th><th>Price</th></tr><tr><td>Pro</td><td>$9</td></tr>"
        "<tr><td>Max</td><td>$20</td></tr></table><p>Prices are monthly.</p>"
    ) == [
        ("Plan, Price.", 150),
        ("Pro, $9.", 150),
        ("Max, $20.", 150),
        ("Prices are monthly.", 0),
    ]


def test_blockquote_is_a_segment_and_a_definition_list_is_one_clip() -> None:
    assert _segs(
        "<blockquote>A quoted sentence of some length.</blockquote>"
        "<dl><dt>The term being defined</dt><dd>Its definition goes right here.</dd>"
        "<dt>Another term</dt><dd>And its own definition.</dd></dl>"
        "<p>Closing paragraph for the list.</p>"
    ) == [
        ("A quoted sentence of some length.", 500),
        (
            "The term being defined. Its definition goes right here. Another term."
            " And its own definition.",
            0,
        ),
        ("Closing paragraph for the list.", 0),
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


# Markup states renderer.js produces for a ```mermaid fence: before
# postRenderMermaid runs it is <pre><code class="language-mermaid">; the
# replacement div is .mermaid-container + .mermaid-loading, then
# .mermaid-rendered (innerHTML = the SVG) or .mermaid-error (message div + a
# <pre><code> holding the source).
_MERMAID_SVG = (
    '<svg id="mermaid-1" class="flowchart"><style>#mermaid-1 .label { color: red; }</style>'
    '<g class="nodes"><g class="node"><foreignObject><div><span class="nodeLabel"><p>llm-gateway:8316</p></span></div></foreignObject></g>'
    '<g class="node"><text>llama-swap:8080</text></g></g>'
    '<g class="edgeLabels"><g class="label"><foreignObject><span class="edgeLabel">chat completions</span></foreignObject></g></g></svg>'
)
_LABELS = ("llm-gateway", "llama-swap", "chat completions", "color: red")


def test_a_rendered_mermaid_container_is_one_diagram_marker_not_its_labels() -> None:
    segs = _segs(
        "<p>The gateway chain is as follows:</p>"
        f'<div class="mermaid-container mermaid-rendered" role="region">{_MERMAID_SVG}</div>'
        "<ul><li>First item of the list</li><li>Second item of the list</li></ul>"
        "<p>Closing paragraph after the list.</p>"
    )
    assert segs == [
        ("The gateway chain is as follows:", 500),
        ("Diagram omitted.", 400),
        ("First item of the list. Second item of the list.", 0),
        ("Closing paragraph after the list.", 0),
    ]
    assert not any(label in t for t, _ in segs for label in _LABELS)


def test_loading_and_error_mermaid_containers_are_also_one_marker() -> None:
    assert _segs('<div class="mermaid-container mermaid-loading"></div>') == [("Diagram omitted.", 0)]
    err = (
        '<div class="mermaid-container mermaid-error"><div class="mermaid-error-msg">Parse error on line 2</div>'
        "<pre><code>flowchart LR\n A --> llm-gateway</code></pre></div>"
    )
    segs = _segs(err)
    assert segs == [("Diagram omitted.", 0)]  # the last clip never pauses


def test_an_unrendered_mermaid_fence_says_diagram_not_code() -> None:
    assert _segs(
        '<p>Here is the chain:</p><pre><code class="language-mermaid">flowchart LR\n A --> B</code></pre>'
        "<p>Back to normal prose now.</p>"
    ) == [
        ("Here is the chain:", 500),
        ("Diagram omitted.", 400),
        ("Back to normal prose now.", 0),
    ]


def test_consecutive_diagram_markers_merge_but_not_into_a_code_marker() -> None:
    d = '<div class="mermaid-container mermaid-loading"></div>'
    assert _segs(d + d + "<pre>x</pre>" + d) == [
        ("Diagram omitted.", 400),
        ("Code omitted.", 400),
        ("Diagram omitted.", 0),
    ]


def test_a_diagram_inside_a_list_item_keeps_the_list_one_clip() -> None:
    segs = _segs(
        "<ul><li>Before the diagram comes here"
        f'<div class="mermaid-container mermaid-rendered">{_MERMAID_SVG}</div></li>'
        "<li>After the diagram comes here</li></ul>"
    )
    assert not any(label in t for t, _ in segs for label in _LABELS)
    assert [t for t, _ in segs] == [
        "Before the diagram comes here.",
        "Diagram omitted.",
        "After the diagram comes here.",
    ]


def test_inline_svg_and_media_elements_are_skipped_silently() -> None:
    assert _segs(
        "<p>Click the <svg><title>gear icon</title><text>cog</text></svg> button to open settings.</p>"
        "<p>See <img alt=\"chart of results\"></img> the picture and <canvas>fallback text</canvas> "
        "<video>no video support</video><audio>no audio support</audio> for details.</p>"
    ) == [
        ("Click the button to open settings.", 500),
        ("See the picture and for details.", 0),
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


def test_a_sentence_over_the_limit_is_wrapped_at_the_last_comma() -> None:
    para = ", ".join(["word"] * 600) + "."  # 3,598 chars, commas but no sentence end
    rows = _extract(f"<p>{para}</p>")[0]
    assert len(rows) > 5
    assert all(len(r["text"]) <= 600 for r in rows)
    assert all(r["text"].endswith(",") for r in rows[:-1])
    assert " ".join(r["text"] for r in rows) == para
    assert [r["pauseMs"] for r in rows] == [0] * len(rows)  # the last piece's pause is dropped


def test_a_sentence_over_the_limit_without_commas_is_wrapped_at_a_space() -> None:
    words = " ".join(["word"] * 700)  # 3,499 chars, no punctuation at all
    rows = _extract(f"<p>{words}</p><p>A trailing paragraph of ordinary size.</p>")[0]
    assert all(len(r["text"]) <= 600 for r in rows)
    assert " ".join(r["text"] for r in rows[:-1]) == words + "."
    assert rows[-2]["pauseMs"] == 500  # the wrapped paragraph keeps its pause on its last piece


def test_an_unbroken_run_over_the_limit_is_hard_cut() -> None:
    rows = _extract("<p>" + "x" * 1300 + "</p>")[0]
    assert [len(r["text"]) for r in rows] == [600, 600, 101]  # 100 x's plus the added period


def test_a_hard_cut_never_splits_a_surrogate_pair() -> None:
    # "x" shifts the run so the 600-unit cut lands between a pair's halves.
    rows = _extract("<p>x" + "😀" * 400 + "</p>")[0]
    assert len(rows) > 1
    for r in rows:  # a lone surrogate would encode to U+FFFD on the wire
        r["text"].encode("utf-8")
    joined = "".join(r["text"] for r in rows)
    assert joined.count("😀") == 400
    assert all(len(r["text"]) <= 600 for r in rows)


def test_plain_text_body_falls_back_to_one_flattened_segment() -> None:
    assert _segs("  hello   world \n there ") == [("hello world there.", 0)]
    assert _segs("Plain <b>bold</b> words, no blocks") == [("Plain bold words, no blocks.", 0)]


def test_block_less_text_is_capped_like_any_other_reply() -> None:
    rows = _extract(("word " * 300).strip())[0]
    assert len(rows) > 1
    assert all(len(r["text"]) <= 600 for r in rows)


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
    return _run(_DOM + _PLAYER.replace("PANE", _PANE_SRC), body)


def test_clips_play_in_order_with_one_fetch_in_flight_and_a_pause_between() -> None:
    out = _play(
        """
const btn = button();
T._playMessageTTS(msg(), btn);
await tick();
const s0 = { fetches: log.fetches.length, busy: btn.cls.has("is-busy"), disabled: btn.disabled };
log.fetches[0].reply(); await tick();
const s1 = { fetches: log.fetches.length, audios: log.srcs.length,
  playing: btn.cls.has("is-playing"), busy: btn.cls.has("is-busy"),
  pressed: btn.attrs["aria-pressed"], announce: log.announce };
log.audios[0].fire("ended"); await tick();
// Clip 0 ended: its URL is gone, the 500 ms pause is pending, clip 1 has not started.
const s2 = { audios: log.srcs.length, timers: log.timers.map((t) => t.ms), revoked: [...log.revoked] };
log.timers[0].fn(); await tick();
// Pause over but fetch 1 still outstanding: wait for it.
const s3 = { audios: log.srcs.length };
log.fetches[1].reply(); await tick();
const s4 = { audios: log.srcs.length, fetches: log.fetches.length, announce: log.announce };
log.audios[0].fire("ended"); await tick(); log.timers[1].fn(); await tick();
log.fetches[2].reply(); await tick();
log.audios[0].fire("ended"); await tick();
console.log(JSON.stringify({ s0, s1, s2, s3, s4, maxInFlight, timers: log.timers.map((t) => t.ms),
  texts: log.fetches.map((f) => f.text), url: log.fetches[0].url, method: log.fetches[0].opts.method,
  headers: log.fetches[0].opts.headers, revoked: log.revoked, announce: log.announce,
  end: { playing: btn.cls.has("is-playing"), busy: btn.cls.has("is-busy"), disabled: btn.disabled,
         pressed: btn.attrs["aria-pressed"], active: T._ttsBtnActive }, toasts: log.toasts }));
"""
    )
    assert out["s0"] == {"fetches": 1, "busy": True, "disabled": False}
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
  audios: log.srcs.length, fetches: log.fetches.length, created: log.created, revoked: log.revoked,
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
console.log(JSON.stringify({ cleared: timer.cleared, audios: log.srcs.length,
  fetches: log.fetches.length, created: log.created, revoked: log.revoked }));
"""
    )
    assert out["cleared"] is True
    assert out["audios"] == 1
    assert out["fetches"] == 2
    # Whatever was minted before the stop (the playing clip) must be revoked.
    assert sorted(out["revoked"]) == sorted(out["created"])


def test_a_superseded_run_is_ignored_and_the_active_button_toggles_off() -> None:
    out = _play(
        """
ignoreAbort = true;                      // A's reply still lands after the abort
const a = button(), b = button();
T._playMessageTTS(msg(), a);
await tick();
const stale = log.fetches[0];
T._playMessageTTS(msg(), b);            // another button supersedes run A
await tick();
stale.reply(); await tick();             // A's late reply must not play or leak
const afterStale = { audios: log.srcs.length, created: log.created.length,
  aBusy: a.cls.has("is-busy"), aDisabled: a.disabled, bBusy: b.cls.has("is-busy") };
log.fetches[1].reply(); await tick();
T._playMessageTTS(msg(), b);            // clicking the active button stops
await tick();
console.log(JSON.stringify({ afterStale, audios: log.srcs.length, fetches: log.fetches.length,
  active: T._ttsBtnActive, paused: log.audios[1].paused, revoked: log.revoked }));
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


def test_a_newer_play_aborts_the_one_still_being_synthesized() -> None:
    # The abort is what reaches the console and the node, which stop
    # synthesizing a clip nobody will hear (#1414); a replaced run says nothing.
    out = _play(
        """
const a = button(), b = button();
T._playMessageTTS(msg(), a); await tick();
const first = log.fetches[0];
T._playMessageTTS(msg(), b); await tick();
console.log(JSON.stringify({ aborted: first.opts.signal.aborted,
  live: log.fetches[1].opts.signal.aborted, toasts: log.toasts }));
"""
    )
    assert out == {"aborted": True, "live": False, "toasts": []}


def test_an_error_page_that_is_not_json_still_shows_a_message() -> None:
    out = _play(
        """
const btn = button();
T._playMessageTTS(msg(), btn); await tick();
log.fetches[0].page(); await tick(); await tick();
console.log(JSON.stringify({ toasts: log.toasts, active: T._ttsBtnActive,
  busy: btn.cls.has("is-busy") }));
"""
    )
    assert out == {
        "toasts": [["Speech synthesis failed", "error"]],
        "active": None,
        "busy": False,
    }


def test_replacing_a_play_while_its_error_is_read_shows_nothing() -> None:
    # The error belongs to a run the user already replaced; showing it would
    # blame the play that is working.
    out = _play(
        """
const a = button(), b = button();
T._playMessageTTS(msg(), a); await tick();
const release = log.fetches[0].slowRefuse("TTS model is not loaded"); await tick();
T._playMessageTTS(msg(), b); await tick();
release(); await tick(); await tick();
console.log(JSON.stringify({ toasts: log.toasts, active: T._ttsBtnActive === b }));
"""
    )
    assert out == {"toasts": [], "active": True}


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
res.during = { toasts: log.toasts.length, paused: log.audios[1].paused };
log.audios[1].fire("ended"); await tick(); log.timers[0].fn(); await tick();
res.network = { toasts: [...log.toasts], audio: T._ttsAudio, active: T._ttsBtnActive,
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
        "audio": None,  # the finished clip was released, not left dangling
        "active": None,
        "playing": False,
    }


def test_auth_sentinels_end_the_run_without_a_toast() -> None:
    """authFetch rejects with 'auth' (a 401 it could not recover) or 'auth
    changed' (the login moved mid-request); app.js treats both as a silent stop,
    and a sign-out while a clip loop waits is routine, not an error."""
    for sentinel in ("auth changed", "auth"):
        out = _play(
            f"""
const btn = button();
T._playMessageTTS(msg(), btn); await tick();
log.fetches[0].fail({json.dumps(sentinel)}); await tick();
const first = {{ toasts: [...log.toasts], active: T._ttsBtnActive, busy: btn.cls.has("is-busy") }};
T._playMessageTTS(msg(), btn); await tick();
log.fetches[1].reply(); await tick();
log.fetches[2].fail({json.dumps(sentinel)}); await tick();         // the prefetch fails while clip 0 plays
log.audios[1].fire("ended"); await tick(); log.timers[0].fn(); await tick();
console.log(JSON.stringify({{ first, toasts: log.toasts, active: T._ttsBtnActive,
  playing: btn.cls.has("is-playing") }}));
"""
        )
        assert out["first"] == {"toasts": [], "active": None, "busy": False}, sentinel
        assert out["toasts"] == [] and out["active"] is None and out["playing"] is False, sentinel


def test_a_login_change_mid_run_stops_playback_at_the_next_clip_boundary() -> None:
    out = _play(
        """
const btn = button();
T._playMessageTTS(msg(), btn); await tick();
log.fetches[0].reply(); await tick();
authGen++;  // another account signed in while clip 0 plays
log.audios[0].fire("ended"); await tick(); log.timers[0].fn(); await tick();
console.log(JSON.stringify({ audios: log.srcs.length, fetches: log.fetches.length,
  fetch1Aborted: log.fetches[1] ? log.fetches[1].done : null,
  playing: btn.cls.has("is-playing"), active: T._ttsBtnActive, toasts: log.toasts }));
"""
    )
    assert out["audios"] == 1  # clip 1 never played
    assert out["fetches"] == 2 and out["fetch1Aborted"] is True  # the prefetch was aborted
    assert out["playing"] is False and out["active"] is None and out["toasts"] == []


def test_a_login_change_while_the_next_clip_is_fetching_stops_before_it_plays() -> None:
    """The check after each fetch is separate from the one after each pause:
    the login can change while clip 1 is still in flight, and its reply must
    not play under the previous session."""
    out = _play(
        """
const btn = button();
T._playMessageTTS(msg(), btn); await tick();
log.fetches[0].reply(); await tick();
log.audios[0].fire("ended"); await tick();
log.timers[0].fn(); await tick();
authGen++;                       // the login changes while clip 1 is still fetching
log.fetches[1].reply(); await tick();
console.log(JSON.stringify({ audios: log.srcs.length, created: log.created.length,
  playing: btn.cls.has("is-playing"), active: T._ttsBtnActive, toasts: log.toasts }));
"""
    )
    assert out == {"audios": 1, "created": 1, "playing": False, "active": None, "toasts": []}


def test_an_audio_error_event_mid_clip_shows_a_toast_and_resets_the_button() -> None:
    out = _play(
        """
const btn = button();
T._playMessageTTS(msg(), btn); await tick();
log.fetches[0].reply(); await tick();
const before = btn.cls.has("is-playing");
log.audios[0].fire("error"); await tick();
console.log(JSON.stringify({ before, toasts: log.toasts, playing: btn.cls.has("is-playing"),
  pressed: btn.attrs["aria-pressed"], active: T._ttsBtnActive, audio: T._ttsAudio,
  fetches: log.fetches.length }));
"""
    )
    assert out["before"] is True
    assert out["toasts"] == [["Audio playback failed", "error"]]
    assert out["playing"] is False and out["pressed"] == "false" and out["active"] is None
    assert out["audio"] is None and out["fetches"] == 2  # no third clip was requested


def test_play_blocked_by_autoplay_policy_shows_a_toast_and_stops() -> None:
    out = _play(
        """
playMode = "NotAllowedError";
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


def test_real_pane_survives_reset_and_disconnect_before_any_playback() -> None:
    """reset() and replayHistory() call _stopTTS() on every pane before any
    playback.  Build the pane from the shipped constructor rather than a
    pre-populated fake, so a pane that only creates its player on first play
    (and so throws here) fails this test."""
    out = _play(
        """
const had = T._ttsPlayer !== undefined && T._ttsPlayer !== null;
T.reset(); T.disconnectSSE();
T.replayHistory([]);
console.log(JSON.stringify({ had, active: T._ttsBtnActive, playing: T._ttsPlayer.isPlaying() }));
"""
    )
    assert out == {"had": True, "active": None, "playing": False}


def test_one_audio_element_is_made_in_the_click_and_reused_for_every_clip() -> None:
    """iOS Safari only lets an element that was unlocked inside the user's
    gesture play later from a timer or an await, so the element is created
    synchronously in the click handler and each clip just swaps its src."""
    out = _play(
        """
const btn = button();
T._playMessageTTS(msg(), btn);
const atClick = log.audios.length;       // before the first await settles
await tick(); log.fetches[0].reply(); await tick();
const clip0 = { src: log.audios[0].src, listeners: log.audios[0].listeners() };
log.audios[0].fire("ended"); await tick(); log.timers[0].fn(); await tick();
log.fetches[1].reply(); await tick();
const clip1 = { src: log.audios[0].src, listeners: log.audios[0].listeners() };
log.audios[0].fire("ended"); await tick(); log.timers[1].fn(); await tick();
log.fetches[2].reply(); await tick();
log.audios[0].fire("ended"); await tick();
console.log(JSON.stringify({ atClick, elements: log.audios.length, srcs: log.srcs, clip0, clip1,
  listeners: log.audios[0].listeners(), revoked: log.revoked, toasts: log.toasts }));
"""
    )
    assert out["atClick"] == 1 and out["elements"] == 1
    assert out["srcs"] == ["blob:0", "blob:1", "blob:2"]
    # Per-clip handlers are removed, not stacked on the shared element.
    assert out["clip0"] == {"src": "blob:0", "listeners": 2}
    assert out["clip1"] == {"src": "blob:1", "listeners": 2}
    assert out["listeners"] == 0
    assert out["revoked"] == ["blob:0", "blob:1", "blob:2"]
    assert out["toasts"] == []


def test_a_prefetched_clip_is_a_blob_until_it_plays() -> None:
    out = _play(
        """
const btn = button();
T._playMessageTTS(msg(), btn);
await tick(); log.fetches[0].reply(); await tick();
log.fetches[1].reply(); await tick();          // clip 1 arrives while clip 0 plays
const whilePlaying = { created: [...log.created], audios: log.srcs.length };
log.audios[0].fire("ended"); await tick(); log.timers[0].fn(); await tick();
console.log(JSON.stringify({ whilePlaying, created: log.created, revoked: log.revoked,
  src: log.audios[0].src }));
"""
    )
    assert out["whilePlaying"] == {"created": ["blob:0"], "audios": 1}
    assert out["created"] == ["blob:0", "blob:1"]
    assert out["revoked"] == ["blob:0"]
    assert out["src"] == "blob:1"


_PLAYING = """
const btn = button();
T._playMessageTTS(msg(), btn);
await tick(); log.fetches[0].reply(); await tick();
const state = () => ({ playing: btn.cls.has("is-playing"), active: T._ttsBtnActive === btn,
  paused: log.audios[0].paused });
"""


def test_hiding_the_tab_does_not_stop_read_aloud() -> None:
    """The visibility handler closes the stream with disconnectSSE(); listening
    to a reply in another tab is the point of audio, so it must keep playing."""
    out = _play(
        _PLAYING
        + """
T.disconnectSSE();
console.log(JSON.stringify({ state: state(), toasts: log.toasts }));
"""
    )
    assert out == {"state": {"playing": True, "active": True, "paused": False}, "toasts": []}


def test_a_history_rebuild_stops_read_aloud_before_the_button_is_detached() -> None:
    out = _play(
        _PLAYING
        + """
T.replayHistory([]);
console.log(JSON.stringify(state()));
"""
    )
    assert out == {"playing": False, "active": False, "paused": True}


def test_reset_stops_read_aloud() -> None:
    out = _play(
        _PLAYING
        + """
T.reset();
console.log(JSON.stringify(state()));
"""
    )
    assert out == {"playing": False, "active": False, "paused": True}


def test_destroy_stops_read_aloud() -> None:
    """destroy() is a closure in createInteractivePane, so its real source is
    run against the real Pane: it was covered only through disconnectSSE(),
    which no longer stops playback."""
    destroy = strip_js_comments(extract_braced(_SRC, "    destroy() {"))
    out = _play(
        _PLAYING
        + """
let active = true, connected = true, recoverTimer = null;
const pane = T;
pane._historyRepair = { clear() {} };
pane._removeVisibilityHandler = () => {};
pane._scrollFollow = { destroy() {} };
pane.el = null;
const ctl = {
"""
        + destroy
        + """
};
ctl.destroy();
console.log(JSON.stringify(state()));
"""
    )
    assert out == {"playing": False, "active": False, "paused": True}


def test_an_undecodable_clip_is_not_reported_as_an_autoplay_block() -> None:
    out = _play(
        """
playMode = "NotSupportedError";
const btn = button();
T._playMessageTTS(msg(), btn); await tick();
log.fetches[0].reply(); await tick();
console.log(JSON.stringify({ toasts: log.toasts, active: T._ttsBtnActive }));
"""
    )
    assert out == {"toasts": [["Audio playback failed", "error"]], "active": None}


def test_the_button_can_stop_a_run_during_the_first_fetch() -> None:
    out = _play(
        """
const btn = button();
T._playMessageTTS(msg(), btn); await tick();
const first = log.fetches[0];
const during = { busy: btn.cls.has("is-busy"), disabled: btn.disabled };
T._playMessageTTS(msg(), btn); await tick();   // the click lands while the first clip is pending
first.reply(); await tick();
console.log(JSON.stringify({ during, aborted: first.opts.signal.aborted, active: T._ttsBtnActive,
  busy: btn.cls.has("is-busy"), audios: log.srcs.length, fetches: log.fetches.length, toasts: log.toasts }));
"""
    )
    assert out == {
        "during": {"busy": True, "disabled": False},
        "aborted": True,
        "active": None,
        "busy": False,
        "audios": 0,
        "fetches": 1,
        "toasts": [],
    }


def test_an_extraction_failure_shows_the_error_toast() -> None:
    """The recursive walker can throw (RangeError on pathological nesting); that
    must surface as a toast, not an unhandled rejection with no state change."""
    out = _play(
        """
const btn = button();
const bad = { querySelector: () => null, get childNodes() { throw new RangeError("too deeply nested"); } };
let rejected = false;
await T._playMessageTTS(bad, btn).catch(() => { rejected = true; });
console.log(JSON.stringify({ rejected, toasts: log.toasts, active: T._ttsBtnActive,
  busy: btn.cls.has("is-busy"), fetches: log.fetches.length }));
"""
    )
    assert out == {
        "rejected": False,
        "toasts": [["too deeply nested", "error"]],
        "active": None,
        "busy": False,
        "fetches": 0,
    }


def test_an_empty_reply_leaves_the_button_idle() -> None:
    out = _play(
        """
const btn = button();
await T._playMessageTTS(parse(""), btn);
console.log(JSON.stringify({ active: T._ttsBtnActive, busy: btn.cls.has("is-busy"),
  fetches: log.fetches.length, toasts: log.toasts }));
"""
    )
    assert out == {"active": None, "busy": False, "fetches": 0, "toasts": []}


def test_inline_code_under_the_threshold_is_spoken_and_longer_is_omitted() -> None:
    short = "a" * 39
    long_ = "a" * 40
    assert _segs(f"<p>Call the <code>{short}</code> function afterwards.</p>") == [
        (f"Call the {short} function afterwards.", 0)
    ]
    # At the threshold the code is replaced in place; the paragraph is not split.
    assert _segs(f"<p>Call the <code>{long_}</code> function afterwards.</p>") == [
        ("Call the Code omitted. function afterwards.", 0)
    ]


def test_inline_code_length_counts_nested_markup() -> None:
    parts = "<b>" + "b" * 20 + "</b>" + "c" * 20
    assert _segs(f"<p>Use <code>{parts}</code> to proceed now.</p>") == [
        ("Use Code omitted. to proceed now.", 0)
    ]


def test_consecutive_code_markers_collapse_into_one() -> None:
    assert _segs(
        "<p>Run these two commands now:</p><pre>a</pre>\n<pre>b</pre><pre>c</pre>"
        "<p>Then continue with the rest.</p>"
    ) == [
        ("Run these two commands now:", 500),
        ("Code omitted.", 400),
        ("Then continue with the rest.", 0),
    ]
    long_ = "x" * 40
    assert _segs(f"<p>See <code>{long_}</code> <code>{long_}</code> for details here.</p>") == [
        ("See Code omitted. for details here.", 0)
    ]


def test_a_reply_over_the_clip_cap_plays_the_first_200_and_says_so() -> None:
    out = _play(
        """
const html = Array.from({ length: 250 }, (_, i) => "<p>Paragraph number " + i + " is long enough.</p>").join("");
const btn = button();
T._playMessageTTS(parse(html, "msg-body"), btn);
await tick();
const toastsAtStart = [...log.toasts];
for (let i = 0; ; i++) {
  if (!log.fetches[i]) break;
  log.fetches[i].reply(); await tick();
  log.audios[0].fire("ended"); await tick();
  if (log.timers[i]) log.timers[i].fn();
  await tick();
}
console.log(JSON.stringify({ toastsAtStart, fetches: log.fetches.length, audios: log.srcs.length,
  last: log.fetches[log.fetches.length - 1].text, timers: log.timers.length, toasts: log.toasts, active: T._ttsBtnActive }));
"""
    )
    assert out["fetches"] == out["audios"] == 200
    assert out["last"] == "Paragraph number 199 is long enough."
    assert out["timers"] == 199  # no trailing pause after the last clip played
    assert out["toastsAtStart"] == out["toasts"]
    assert len(out["toasts"]) == 1
    message, kind = out["toasts"][0]
    assert kind == "warn"
    assert "200" in message and "skipped" in message
    assert out["active"] is None
