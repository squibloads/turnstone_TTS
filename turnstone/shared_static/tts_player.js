// ===========================================================================
//  turnstone read-aloud â€” shared module (shared_static/tts_player.js)
//
//  The speech pipeline lifted out of the interactive pane so the coordinator
//  pane reads replies aloud the same way: the voice-role probe, the pure
//  reply-to-clips walker, the speaker button on finished replies, and the
//  segmented player (one fetch in flight, prefetch one ahead, one reused Audio
//  element, token-based cancellation).
//
//  Transport is one knob: `base` is "" for a same-origin pane and
//  "/node/{id}" when the console proxies a node-hosted session, exactly as the
//  interactive pane's `opts.base`.  Each pane owns its own player, so two panes
//  can hold a stopped/playing state independently.
//
//  ES module.  House style: programmatic DOM, NO innerHTML.
// ===========================================================================

import { authFetch, authGeneration } from "./auth.js";
import { showToast } from "./toast.js";
import { makeAnnouncer } from "./utils.js";
import { ensureMsgActionsBar, findMsgActionsBar } from "./copy_actions.js";
import { openPopupMenu } from "./pane.js";

// Voice-role availability comes from /v1/api/models (stt_default_alias /
// tts_default_alias â€” present only when an audio-capable model role is
// configured).  Memoized so all panes share a single fetch; affordances stay
// hidden until it resolves.
// Memoized per transport base â€” a console pane proxies a node-hosted session,
// so its voice roles come from THAT node's /v1/api/models (base "/node/{id}"),
// not the console's; standalone panes use base "" and share one fetch.
const _voiceRolesPromises = {};

export function getVoiceRoles(base) {
  base = base || "";
  if (!_voiceRolesPromises[base]) {
    _voiceRolesPromises[base] = authFetch(base + "/v1/api/models")
      .then((r) => (r.ok ? r.json() : {}))
      .then((d) => ({
        stt: !!(d && d.stt_default_alias),
        tts: !!(d && d.tts_default_alias),
      }))
      .catch(() => ({ stt: false, tts: false }));
  }
  return _voiceRolesPromises[base];
}

// The speaking styles offered beside the speaker come from
// /v1/api/tts/styles, fetched from the SAME transport base as the TTS POST:
// a console pane proxies a node-hosted session, so its menu must come from
// that node's settings, not the console's.  Memoized per base like the voice
// roles — one probe per page load, no retry storm after a failure.  A failed
// or unusable response resolves to null, and every caller treats that as
// "no picker, no style" rather than an error.
const _ttsStylesPromises = {};

export function getTtsStyles(base) {
  base = base || "";
  if (!_ttsStylesPromises[base]) {
    // Promise.resolve().then keeps a synchronous throw (a degraded page whose
    // auth module never loaded) on the same rejected path as a network error.
    _ttsStylesPromises[base] = Promise.resolve()
      .then(() => authFetch(base + "/v1/api/tts/styles"))
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => _normalizeStyles(d))
      .catch(() => null);
  }
  return _ttsStylesPromises[base];
}

// Keep only what the picker can act on: a model that takes instructions and
// at least one style offered in the menu.  A style is usable only when it is
// an object with a non-empty string name; anything else is a server bug this
// client degrades past rather than a reason to break the reply actions.
function _normalizeStyles(data) {
  if (!data || data.instructions_supported !== true) return null;
  const styles = [];
  for (const item of Array.isArray(data.styles) ? data.styles : []) {
    if (!item || typeof item.name !== "string") continue;
    const name = item.name.trim();
    if (!name) continue;
    styles.push({ name, in_menu: item.in_menu === true });
  }
  if (!styles.some((style) => style.in_menu)) return null;
  return {
    styles,
    default: typeof data.default === "string" ? data.default.trim() : "",
  };
}

// Visually-hidden polite live region for playback status.  Created on first
// use, not at import: the module must load before document.body exists in a
// test or a degraded page, and a pane that never plays never needs one.
// Panes that already own a voice announcer (the interactive pane's mic status)
// pass it in so the two share one region instead of clobbering each other.
let _defaultAnnounce = null;
function _announcePlayback(msg) {
  if (!_defaultAnnounce) _defaultAnnounce = makeAnnouncer();
  _defaultAnnounce(msg);
}

// The speaker button, wired to `onPlay(btn)`.  Same classes and labels on
// every surface so the stylesheet and the aria contract live in one place.
export function buildTtsButton(onPlay) {
  const btn = document.createElement("button");
  btn.className = "msg-action-btn msg-tts-btn";
  btn.title = "Play response aloud";
  btn.setAttribute("aria-label", "Play response aloud");
  btn.setAttribute("aria-pressed", "false");
  const icon = document.createElement("span");
  icon.className = "icon-speaker";
  icon.setAttribute("aria-hidden", "true");
  btn.appendChild(icon);
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    onPlay(btn);
  });
  return btn;
}

// Adds the speaker to one reply's actions bar; a no-op when it already has one.
// Inserted BEFORE the persistent copy button so DOM order, tab order and
// visual order agree: [retry, tts, style, copy], with copy anchoring the
// corner on every bubble.  `base` is the pane's transport prefix (or a
// function returning it), the same one its TTS POST uses; the style probe
// must reach the same node, so it cannot assume a same-origin page.
export function addTtsAction(el, onPlay, base) {
  const bar = ensureMsgActionsBar(el);
  if (bar.querySelector(".msg-tts-btn")) return; // already added
  bar.insertBefore(buildTtsButton(onPlay), bar.querySelector(".msg-copy-btn"));
  _attachStylePicker(bar, base);
}

// ===========================================================================
//  Speaking-style picker
// ===========================================================================

// One browser-wide choice for every reply on the page.  localStorage can
// throw (private mode, quota, a sandboxed frame), so every access is guarded:
// a broken store means Default, never a broken read-aloud button.  The last
// choice is also held in memory, so a store that throws on read OR write
// still keeps the current page consistent with what the user just picked.
const STYLE_STORAGE_KEY = "turnstone.tts.style";
let _styleChoice = "";
// Set when an access throws: the in-memory choice is then authoritative for
// this page (a store that rejected our write still holds the OLD value, and
// reading it back would silently undo what the user just picked).
let _styleStoreUnavailable = false;

function _readStoredStyle() {
  if (_styleStoreUnavailable) return _styleChoice;
  try {
    const value = localStorage.getItem(STYLE_STORAGE_KEY);
    _styleChoice = typeof value === "string" ? value : "";
  } catch (e) {
    _styleStoreUnavailable = true;
  }
  return _styleChoice;
}

function _writeStoredStyle(name) {
  _styleChoice = name || "";
  try {
    localStorage.setItem(STYLE_STORAGE_KEY, _styleChoice);
  } catch (e) {
    _styleStoreUnavailable = true;
  }
}

// Only menu styles are selectable: a stored name that has left the menu (or
// the styles list) falls back to Default, matching what the picker can show.
function _menuStyles(config) {
  return config.styles.filter((style) => style.in_menu);
}

function _findStyle(config, name) {
  const wanted = (name || "").trim().toLowerCase();
  if (!wanted) return null;
  for (const style of _menuStyles(config)) {
    if (style.name.toLowerCase() === wanted) return style;
  }
  return null;
}

function _currentStyle(config) {
  return _findStyle(config, _readStoredStyle());
}

function _defaultStyleLabel(config) {
  return "Default (" + (config.default || "Plain") + ")";
}

// Every live picker repaints when one changes: the choice is browser-wide, so
// leaving other replies showing the old name would misreport what a click
// there will send.
const _stylePickers = new Set();

// A rebuilt transcript detaches whole bubbles; drop their pickers rather than
// pinning their DOM for the life of the page.  (A test DOM without
// isConnected reports `undefined`, which must read as connected.)
function _pruneStylePickers() {
  for (const picker of _stylePickers) {
    if (picker.btn.isConnected === false) _stylePickers.delete(picker);
  }
}

function _paintStylePicker(picker) {
  const current = _currentStyle(picker.config);
  const name = current ? current.name : "Default";
  picker.label.textContent = name;
  const label = "Speaking style: " + name;
  picker.btn.title = label;
  picker.btn.setAttribute("aria-label", label);
}

function _paintStylePickers() {
  _pruneStylePickers();
  for (const picker of _stylePickers) _paintStylePicker(picker);
}

function _chooseStyle(name) {
  _writeStoredStyle(name);
  _paintStylePickers();
}

// One open picker at a time.  The shared popup helper owns the chrome
// (.tab-menu), positioning, outside-dismissal, arrow roving and the
// focus-returning Escape; this wrapper adds the radio semantics and the
// check glyph that a single-choice menu needs.
let _openStyleMenu = null;

function _toggleStyleMenu(picker) {
  if (_openStyleMenu && _openStyleMenu.picker === picker) {
    _openStyleMenu.handle.close();
    return;
  }
  if (_openStyleMenu) _openStyleMenu.handle.close();
  const menuStyles = _menuStyles(picker.config);
  const current = _currentStyle(picker.config);
  const items = [
    {
      label: _defaultStyleLabel(picker.config),
      key: current ? "" : "✓",
      action: () => _chooseStyle(""),
    },
  ];
  for (const style of menuStyles) {
    items.push({
      label: style.name,
      key: current && current.name === style.name ? "✓" : "",
      action: () => _chooseStyle(style.name),
    });
  }
  let handle = null;
  handle = openPopupMenu(picker.btn, items, {
    label: "Speaking style",
    cls: "msg-style-menu",
    expandEl: picker.btn,
    returnFocusEl: picker.btn,
    ignoreEl: picker.btn,
    onClose: () => {
      // The actions bar is hover/focus-revealed; focus lives in the menu now,
      // so pin the bar visible until the menu closes.
      picker.bar.classList.remove("is-style-menu-open");
      if (_openStyleMenu && _openStyleMenu.handle === handle) {
        _openStyleMenu = null;
      }
    },
  });
  picker.bar.classList.add("is-style-menu-open");
  // The helper builds generic menu items; a single-choice list must announce
  // itself as radio items with a checked state.
  const nodes = Array.from(handle.menu.querySelectorAll(".tab-menu-item"));
  const found = current
    ? menuStyles.findIndex((style) => style.name === current.name)
    : -1;
  const checkedIndex = found < 0 ? 0 : found + 1; // 0 = Default
  nodes.forEach((node, i) => {
    node.setAttribute("role", "menuitemradio");
    node.setAttribute("aria-checked", i === checkedIndex ? "true" : "false");
  });
  // The helper focuses the first item; a radio menu opens on the current one.
  if (nodes[checkedIndex]) nodes[checkedIndex].focus();
  _openStyleMenu = { picker, handle };
}

function _buildStylePicker(bar, config) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "msg-action-btn msg-style-btn";
  btn.setAttribute("aria-haspopup", "menu");
  btn.setAttribute("aria-expanded", "false");
  const label = document.createElement("span");
  label.className = "msg-style-label";
  btn.appendChild(label);
  const caret = document.createElement("span");
  caret.className = "msg-style-caret";
  caret.setAttribute("aria-hidden", "true");
  btn.appendChild(caret);
  const picker = { btn, label, bar, config };
  _paintStylePicker(picker);
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    _toggleStyleMenu(picker);
  });
  return picker;
}

// The styles probe is shared by every reply's bar, so the first action render
// starts it and the rest attach from the memoized response.  A failed probe
// adds nothing: the picker simply never appears, with no error surface.
function _attachStylePicker(bar, base) {
  if (bar.querySelector(".msg-style-btn")) return;
  const resolved = typeof base === "function" ? base() : base;
  getTtsStyles(resolved).then((config) => {
    if (!config) return;
    if (bar.querySelector(".msg-style-btn")) return;
    // A bar whose speaker was torn down has no anchor to sit beside.
    if (!bar.querySelector(".msg-tts-btn")) return;
    const picker = _buildStylePicker(bar, config);
    _pruneStylePickers();
    _stylePickers.add(picker);
    bar.insertBefore(picker.btn, bar.querySelector(".msg-copy-btn"));
  });
}

// Speaker on every finished assistant reply, not just the retry holder, so
// an earlier answer can be read without regenerating the latest one.
// Walks back from the tail and stops at the first reply that already has
// its speaker: replies are only ever added at the tail, so everything
// before it was handled by an earlier pass (the full walk happens once per
// replay or role resolution, when no reply has one yet).  `skipEl` is the
// bubble still streaming, so half a reply is never read; the settle edge
// calls this again and picks it up.  `onPlay(el, btn)` is the pane's play;
// `base` rides through to the style probe, exactly as it reaches the player.
export function addTtsActionsToFinished(messagesEl, skipEl, onPlay, base) {
  if (!messagesEl) return;
  for (
    let el = messagesEl.lastElementChild;
    el;
    el = el.previousElementSibling
  ) {
    if (!el.classList.contains("msg") || !el.classList.contains("assistant"))
      continue;
    if (el === skipEl) continue;
    const bar = findMsgActionsBar(el);
    if (bar && bar.querySelector(".msg-tts-btn")) break;
    addTtsAction(el, (btn) => onPlay(el, btn), base);
  }
}

// The speakable reply as [{text, pauseMs}], one row per block element so a
// heading, paragraph or table row gets its own clip and a pause instead of
// running into the next sentence.  A whole list (UL, OL or DL, nested lists
// included) is ONE row: each item gets a full stop if it lacks terminal
// punctuation and the items are joined with a space, with no pause between
// items and none after the list (listening tests: the model's own
// sentence-end pause was enough).  Table rows keep one clip each with a
// 150 ms pause; a table row is never itself folded forward, but a short
// lead-in paragraph can fold into the first row.  Fenced code becomes a marker, a diagram
// (rendered, loading, errored or unrendered mermaid) becomes its own
// "Diagram omitted." marker, SVG/img/canvas/video/audio are skipped, and rendered
// math (KaTeX's accessibility text) is dropped.  Inline code is spoken when
// short (an identifier or flag) and replaced by the marker from
// INLINE_CODE_MAX characters up (a command line or expression is noise
// read aloud), in place so the paragraph around it stays one clip.
export function extractSpeechSegments(bodyEl) {
  const INLINE_CODE_MAX = 40;
  const PAUSE = {
    H1: 600,
    H2: 600,
    H3: 600,
    H4: 600,
    H5: 600,
    H6: 600,
    P: 500,
    BLOCKQUOTE: 500,
    TR: 150,
  };
  const CODE_OMITTED = "Code omitted.";
  const DIAGRAM_OMITTED = "Diagram omitted.";
  // Elements with no speakable text of their own: an inline SVG icon or a
  // chart would otherwise leak its <title>, <text> and <style> content.
  const SILENT = new Set(["SVG", "IMG", "CANVAS", "VIDEO", "AUDIO"]);
  const raw = [];
  // A segment keeps its text as parts plus a count of non-space characters,
  // so the list and cell logic below looks only at the tail and never
  // re-flattens the whole accumulated text (a degenerate reply can be tens of
  // thousands of list items).
  const newSeg = (pauseMs, extra) => {
    const seg = { parts: [], sig: 0, marker: false, pauseMs, ...extra };
    raw.push(seg);
    return seg;
  };
  const put = (seg, str, isMarker) => {
    const n = str.replace(/\s+/g, "").length;
    const p = seg.parts;
    if (!n && p.length && !p[p.length - 1].trim()) p[p.length - 1] += str;
    else p.push(str);
    if (n) {
      seg.sig += n;
      seg.marker = !!isMarker;
    }
  };
  const trimEnd = (seg) => {
    const p = seg.parts;
    while (p.length) {
      const t = p[p.length - 1].trimEnd();
      if (t) {
        p[p.length - 1] = t;
        return;
      }
      p.pop();
    }
  };
  const tail = (seg, n) => {
    let t = "";
    for (let i = seg.parts.length - 1; i >= 0 && t.length < n; i--) {
      t = seg.parts[i].slice(-(n - t.length)) + t;
    }
    return t;
  };
  let cur = null; // segment taking inline text; null until the next text
  let listDepth = 0; // inside a list everything folds into the one list row
  // Written with hex escapes for the quote characters; see shapeSpeechSegments.
  const ENDS = /[.!?:…][\x22\x27)\]”’]*$/;
  const itemBreak = () => {
    // Looked at before anything is trimmed: a blank item leaves the text as is.
    const p = cur.parts;
    let i = p.length - 1;
    while (i >= 0 && !p[i].trim()) i--;
    const comma = i >= 0 && p[i].trimEnd().endsWith(",");
    if (cur.sig - (comma ? 1 : 0) <= 0) return;
    trimEnd(cur);
    if (comma) {
      p[p.length - 1] = p[p.length - 1].slice(0, -1);
      cur.sig--;
    }
    put(cur, ENDS.test(tail(cur, 16)) ? " " : ". ");
  };
  // A cell after the first sets cellSep; the ", " is written only once the
  // cell has text, so an empty cell adds no separator and padding around
  // the cell text does not leave "Plan , Price".
  let cellSep = false;
  let rowBase = 0; // non-space count at the start of the current row
  const addText = (str, isMarker) => {
    if (!cur) cur = newSeg(500);
    if (!cellSep) {
      put(cur, str, isMarker);
      return;
    }
    if (!str.trim()) return;
    trimEnd(cur);
    put(cur, ", ");
    put(cur, str.trimStart(), isMarker);
    cellSep = false;
  };
  const walk = (node) => {
    for (const child of node.childNodes) {
      if (child.nodeType === 3) {
        addText(child.textContent);
        continue;
      }
      if (child.nodeType !== 1) continue;
      const cls = child.classList;
      if (cls.contains("katex") || cls.contains("katex-display")) continue;
      if (child.tagName === "CODE" && child.textContent.length >= INLINE_CODE_MAX) {
        // Adjacent markers collapse, except across a cell boundary: each
        // long-code cell of a row keeps its own.
        if (!(cur && cur.marker && !cellSep)) addText(" " + CODE_OMITTED + " ", true);
        continue;
      }
      // SVG elements report a lowercase tagName, so compare upper-cased.
      if (SILENT.has(child.tagName.toUpperCase())) continue;
      // renderer.js turns a mermaid fence into .mermaid-container (loading,
      // rendered SVG, or error message plus a <pre> of the source); until
      // mermaid loads it is still <pre><code class="language-mermaid">.
      // Every state is one diagram marker, never the node and edge labels.
      const isDiagram =
        cls.contains("mermaid-container") ||
        (child.tagName === "PRE" &&
          Array.from(child.childNodes).some(
            (c) => c.nodeType === 1 && c.classList.contains("language-mermaid"),
          ));
      if (isDiagram || child.tagName === "PRE") {
        put(newSeg(400, { code: true }), isDiagram ? DIAGRAM_OMITTED : CODE_OMITTED);
        cur = listDepth ? newSeg(0) : null;
        continue;
      }
      const tag = child.tagName;
      if (tag === "UL" || tag === "OL" || tag === "DL") {
        if (listDepth) itemBreak();
        else cur = newSeg(0);
        listDepth++;
        walk(child);
        listDepth--;
        if (listDepth) itemBreak();
        else cur = null;
        continue;
      }
      if (listDepth && (tag === "LI" || tag === "DT" || tag === "DD" || PAUSE[tag] !== undefined)) {
        // A table row inside a list item is a sentence of the list clip and
        // starts clean: its first cell must not see the previous row's text.
        const isRow = tag === "TR";
        itemBreak();
        if (isRow) {
          cellSep = false;
          rowBase = cur.sig;
        }
        walk(child);
        itemBreak();
        if (isRow) cellSep = false;
        continue;
      }
      const isCell = child.tagName === "TD" || child.tagName === "TH";
      if (isCell && cur && cur.sig > rowBase) cellSep = true;
      const pauseMs = PAUSE[child.tagName];
      if (pauseMs === undefined) {
        walk(child);
        if (child.tagName === "TABLE") cellSep = false;
        continue;
      }
      cellSep = false;
      rowBase = 0;
      cur = newSeg(pauseMs, { table: child.tagName === "TR" });
      walk(child);
      cur = null; // a nested block ends this one; later text starts anew
      cellSep = false; // a trailing empty cell must not arm the next block's text
    }
  };
  walk(bodyEl);
  // Plain text (no block structure) is shaped like any other reply, so the
  // clip cap applies to it too.
  return shapeSpeechSegments(
    raw.map((s) => ({ text: s.parts.join(""), pauseMs: s.pauseMs, code: s.code, table: s.table })),
  );
}

// Normalise raw block text into clips a TTS model reads well: terminal
// punctuation so it pauses and drops pitch, fragments folded into the next
// clip (a five-word clip sounds clipped), long blocks cut at sentence ends
// (long inputs raise latency and fail on some backends, and the server
// rejects more than 8000 characters).  A sentence that is itself longer than
// that is wrapped at its last comma, else its last space.
export function shapeSpeechSegments(raw) {
  const MIN_CHARS = 20;
  const MAX_CHARS = 600;
  // Closing quotes/brackets may follow the mark.  The quotes are written as
  // hex escapes (x22 and x27) because the test harness slices methods with a
  // brace walker that treats a quote character inside a regex literal as the
  // start of a string.
  const ENDS_SENTENCE = /[.!?:\u2026][\x22\x27)\]\u201d\u2019]*$/;
  const rows = [];
  for (const r of raw) {
    const text = r.text.replace(/\s+/g, " ").trim().replace(/,$/, "");
    if (!text) continue;
    const end = ENDS_SENTENCE.test(text) ? "" : ".";
    // Adjacent identical markers are one omission, not one request each.
    if (r.code && rows.length && rows[rows.length - 1].code && rows[rows.length - 1].text === text + end) continue;
    rows.push({ text: text + end, pauseMs: r.pauseMs, code: !!r.code, table: !!r.table });
  }
  const merged = [];
  let carry = "";
  rows.forEach((r, i) => {
    const next = rows[i + 1];
    const text = carry ? carry + " " + r.text : r.text;
    if (!r.code && !r.table && text.length < MIN_CHARS && next && !next.code) {
      carry = text;
      return;
    }
    carry = "";
    merged.push({ text, pauseMs: r.pauseMs });
  });
  const wrapLong = (s) => {
    const parts = [];
    while (s.length > MAX_CHARS) {
      const comma = s.lastIndexOf(",", MAX_CHARS - 1);
      const space = s.lastIndexOf(" ", MAX_CHARS);
      let cut = comma > 0 ? comma + 1 : space > 0 ? space : MAX_CHARS;
      // A hard cut must not land between the halves of a surrogate pair.
      if (cut === MAX_CHARS && comma <= 0 && space <= 0) {
        const hi = s.charCodeAt(cut - 1);
        if (hi >= 0xd800 && hi <= 0xdbff) cut--;
      }
      parts.push(s.slice(0, cut).trim());
      s = s.slice(cut).trim();
    }
    parts.push(s);
    return parts;
  };
  const out = [];
  for (const seg of merged) {
    // Lookbehind is avoided: an unsupported regex fails the whole script.
    let piece = "";
    const sentences = seg.text
      .replace(/([.!?])\s+/g, "$1\n")
      .split("\n")
      .flatMap(wrapLong);
    for (const s of sentences) {
      if (piece && piece.length + 1 + s.length > MAX_CHARS) {
        out.push({ text: piece, pauseMs: 0 });
        piece = s;
      } else {
        piece = piece ? piece + " " + s : s;
      }
    }
    out.push({ text: piece, pauseMs: seg.pauseMs });
  }
  if (out.length) out[out.length - 1].pauseMs = 0;
  return out;
}

// The style a request should name, resolved against THIS base's menu.  ""
// means Default: the caller omits the field entirely, so the body is
// byte-identical to a client that predates styles.  A stored name this base
// no longer offers, a model without instruction support, and a failed probe
// all resolve to Default rather than sending a name the server would ignore.
async function _requestStyle(base) {
  const stored = _readStoredStyle();
  if (!stored) return "";
  const config = await getTtsStyles(base);
  if (!config) return "";
  const style = _findStyle(config, stored);
  return style ? style.name : "";
}

// One pane's read-aloud player.  Options:
//   base            transport prefix ("" or "/node/{id}"), or a function
//                   returning it when the pane's base can change after
//                   construction.
//   announce        live-region announcer; defaults to a module-owned region.
//   authGeneration  returns a token that changes on logout or account switch;
//                   defaults to auth.js's.  A run holding an older token stops.
// Returns { play(el, btn), stop(), isPlaying(), activeButton(), audio() }.
export function createTtsPlayer(opts) {
  opts = opts || {};
  const MAX_CLIPS = 200;
  const baseOf = typeof opts.base === "function" ? opts.base : () => opts.base || "";
  const announce = opts.announce || _announcePlayback;
  const generationOf = opts.authGeneration || authGeneration;
  // seq is the cancellation token: stop() bumps it, and a run that wakes up
  // holding an older one neither plays nor leaks a blob.
  let seq = 0;
  let abortCtl = null;
  let timer = null;
  let audioEl = null; // the element, set only while a clip is loaded
  let activeBtn = null;

  function stop() {
    seq++; // invalidate any run in progress
    if (abortCtl) {
      abortCtl.abort();
      abortCtl = null;
    }
    clearTimeout(timer);
    if (audioEl) {
      try {
        audioEl.pause();
      } catch (e) {
        /* ignore */
      }
      URL.revokeObjectURL(audioEl.src);
      audioEl = null;
    }
    const btn = activeBtn;
    if (btn) {
      btn.classList.remove("is-busy", "is-playing");
      btn.setAttribute("aria-pressed", "false");
      activeBtn = null;
    }
  }

  async function play(el, btn) {
    // Toggle: clicking the active button (or any while playing) stops first.
    if (activeBtn) {
      const wasThis = activeBtn === btn;
      stop();
      if (wasThis) return;
    }
    // Serialize: stop() bumps the token, so a superseded run notices at its
    // next await and neither plays nor leaks a blob.  Taken before the try so
    // the catch can tell a failure of this run from one a stop already ended.
    const token = ++seq;
    const abort = new AbortController();
    abortCtl = abort;
    activeBtn = btn;
    // Not disabled: a click during the first fetch must still stop the run.
    btn.classList.add("is-busy");
    // One element for the whole run, made here while the click's user gesture
    // is still live: iOS Safari lets only an element unlocked by a gesture play
    // later from an await or a timer, so each clip swaps its src rather than
    // constructing a new Audio.
    const audio = new Audio();
    // Many self-hosted backends synthesize one request at a time, so exactly
    // one fetch is ever in flight: clip i+1 is requested once clip i starts.
    const fetchClip = async (text, style) => {
      const r = await authFetch(baseOf() + "/v1/api/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // Default (""): omit the field, byte-identical to the pre-styles body.
        body: JSON.stringify(style ? { text, style } : { text }),
        signal: abort.signal,
      });
      if (!r.ok) {
        const b = await r.json().catch(() => null);
        throw new Error((b && b.error) || "Speech synthesis failed");
      }
      return r.blob();
    };
    try {
      // Inside the try: the walker is recursive and can throw on pathological
      // nesting, which must reach the toast rather than reject silently.
      const segments = extractSpeechSegments(
        el.querySelector(".msg-body") || el,
      );
      if (!segments.length) {
        stop();
        return;
      }
      // One request per clip, so a table or list of hundreds of rows would
      // queue hundreds of syntheses; read the first MAX_CLIPS and say so.
      if (segments.length > MAX_CLIPS) {
        segments.length = MAX_CLIPS;
        segments[MAX_CLIPS - 1].pauseMs = 0;
        showToast(
          "Read-aloud stopped after the first " +
            MAX_CLIPS +
            " sections; the rest of this reply was skipped.",
          "warn",
        );
      }
      // Clips are authenticated one by one; a logout or account switch mid-run
      // must not keep speaking (or fetching) the previous session's reply.
      const generation = generationOf();
      // Stops the pane's CURRENT run, so a caller must already know it is this
      // run (the token test) or it would silence a newer one.
      const stopIfAuthChanged = () => {
        if (generation === generationOf()) return false;
        stop();
        return true;
      };
      // Resolved once per run: every clip of a segmented reply carries the
      // same style, and a change mid-run cannot split one reading in two.
      let requestStyle = "";
      try {
        requestStyle = await _requestStyle(baseOf());
      } catch (e) {
        requestStyle = ""; // a failed probe is Default, never a broken play
      }
      if (token !== seq) return;
      if (stopIfAuthChanged()) return;
      let next = fetchClip(segments[0].text, requestStyle);
      for (let i = 0; i < segments.length; i++) {
        const blob = await next;
        if (token !== seq) return; // superseded: nothing minted, nothing leaked
        if (stopIfAuthChanged()) return;
        if (i + 1 < segments.length) {
          next = fetchClip(segments[i + 1].text, requestStyle);
          next.catch(() => {}); // surfaced when awaited; not unhandled meanwhile
        }
        // Minted only now so a prefetched clip holds no object URL; stop()
        // revokes the playing clip's, the "ended" path below the finished one.
        const url = URL.createObjectURL(blob);
        audioEl = audio;
        if (i === 0) {
          btn.classList.remove("is-busy");
          btn.classList.add("is-playing");
          btn.setAttribute("aria-pressed", "true");
          announce("Playing response.");
        }
        // A stopped clip never fires "ended", so a stopped run just stays
        // parked here (and at the pause below) until it is collected.
        await new Promise((resolve, reject) => {
          // The element outlives the clip, so this clip's handlers come off
          // when it settles instead of stacking up on the next one.
          const settle = (fn, arg) => {
            audio.removeEventListener("ended", onEnded);
            audio.removeEventListener("error", onError);
            fn(arg);
          };
          const onEnded = () => settle(resolve);
          const onError = () => settle(reject, new Error("Audio playback failed"));
          audio.addEventListener("ended", onEnded);
          audio.addEventListener("error", onError);
          audio.src = url;
          // NotAllowedError is the autoplay policy; anything else (an
          // undecodable clip) must not be blamed on the browser.
          audio
            .play()
            .catch((e) =>
              settle(
                reject,
                new Error(
                  e && e.name === "NotAllowedError"
                    ? "Playback was blocked by the browser"
                    : "Audio playback failed",
                ),
              ),
            );
        });
        URL.revokeObjectURL(url);
        audioEl = null;
        if (segments[i].pauseMs) {
          await new Promise((resolve) => {
            timer = setTimeout(resolve, segments[i].pauseMs);
          });
        }
        if (token === seq && stopIfAuthChanged()) return;
      }
      stop();
    } catch (err) {
      if (token !== seq) return; // aborted by a stop or a newer run
      // authFetch's sentinels for a lost or changed login end the run quietly,
      // as every other caller does; the login screen is the message.
      if (err && (err.message === "auth" || err.message === "auth changed")) {
        stop();
        return;
      }
      showToast(
        err && err.message ? err.message : "Speech synthesis failed",
        "error",
      );
      stop();
    }
  }

  return {
    play,
    stop,
    isPlaying: () => activeBtn !== null,
    activeButton: () => activeBtn,
    audio: () => audioEl,
  };
}
