// ===========================================================================
//  turnstone read-aloud â€” shared module (shared_static/tts_player.js)
//
//  The speech pipeline lifted out of the interactive pane so the coordinator
//  pane reads replies aloud the same way: the voice-role probe, the pure
//  reply-to-clips walker, the speaker button on finished replies, the
//  segmented player (one fetch in flight, prefetch one ahead, one reused Audio
//  element, token-based cancellation), and the streaming feeder that reads a
//  live reply a completed sentence at a time.
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

function _ttsConfig(base) {
  base = base || "";
  if (!_ttsStylesPromises[base]) {
    // Promise.resolve().then keeps a synchronous throw (a degraded page whose
    // auth module never loaded) on the same rejected path as a network error.
    _ttsStylesPromises[base] = Promise.resolve()
      .then(() => authFetch(base + "/v1/api/tts/styles"))
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null);
  }
  return _ttsStylesPromises[base];
}

const _ttsStyleMenus = {};

export function getTtsStyles(base) {
  base = base || "";
  if (!_ttsStyleMenus[base]) {
    _ttsStyleMenus[base] = _ttsConfig(base).then((d) => _normalizeStyles(d));
  }
  return _ttsStyleMenus[base];
}

// The admin playback rate (audio.tts_playback_rate) for this base.  It rides
// the styles probe but is independent of instruction support: speed applies
// to every model.  A failed probe or an out-of-range value plays at 1.
export function getTtsPlaybackRate(base) {
  return _ttsConfig(base).then((d) => {
    const rate = d && d.playback_rate;
    return typeof rate === "number" && rate >= 0.5 && rate <= 2 ? rate : 1;
  });
}

// The read-aloud profile (audio.tts_profiles, keyed by the TTS role's model
// alias) for this base.  It rides the styles probe like the playback rate:
// one fetch per base per page.  Every field is always present in the result;
// a missing profile, a failed probe and a malformed field each read as this
// module's built-in defaults, which are the constants the player used before
// profiles existed — a model with no profile sounds exactly as it did.
export function getTtsProfile(base) {
  return _ttsConfig(base).then((d) => _normalizeProfile(d && d.profile));
}

// Whether read-aloud speaks a reply while it is still being written
// (audio.tts_stream).  It rides the styles probe like the playback rate and
// the profile: one read-aloud config fetch per base per page.  A failed probe,
// a server that predates the field and an explicit false all read as off, so
// the old finished-reply behaviour is the fallback, never a broken player.
export function getTtsStreamEnabled(base) {
  return _ttsConfig(base).then((d) => !!(d && d.stream === true));
}

// The built-in profile.  Bounds, defaults and choices mirror TtsProfile,
// DEFAULT_PROFILE and PROFILE_FIELDS in turnstone/core/voice_config.py — that
// table is the source of truth; keep this copy in step.  Bounds are enforced
// here as well as on the server because a stale node, a hand-edited JSON
// setting or a future release's value must not reach the walker as a broken
// number (a negative pause is a TypeError inside setTimeout).
const MAX_MARKER_CHARS = 60;
const DEFAULT_TTS_PROFILE = Object.freeze({
  pause_heading_ms: 600,
  pause_paragraph_ms: 500,
  pause_table_row_ms: 150,
  pause_code_ms: 400,
  pause_list_item_ms: 0, // 0 = a whole list is one clip
  table_cell_joiner: "comma",
  table_header_mode: "row",
  inline_code_max_chars: 40,
  code_marker: "Code omitted.", // "" = skip silently
  diagram_marker: "Diagram omitted.", // "" = skip silently
  clip_min_chars: 20,
  clip_max_chars: 600,
  max_clips: 200,
});
const TTS_PROFILE_FIELDS = Object.freeze({
  pause_heading_ms: { kind: "int", min: 0, max: 5000 },
  pause_paragraph_ms: { kind: "int", min: 0, max: 5000 },
  pause_table_row_ms: { kind: "int", min: 0, max: 5000 },
  pause_code_ms: { kind: "int", min: 0, max: 5000 },
  pause_list_item_ms: { kind: "int", min: 0, max: 5000 },
  table_cell_joiner: { kind: "choice", choices: ["comma", "semicolon", "pause"] },
  table_header_mode: { kind: "choice", choices: ["row", "prefix", "skip"] },
  inline_code_max_chars: { kind: "int", min: 0, max: 500 },
  code_marker: { kind: "str" },
  diagram_marker: { kind: "str" },
  clip_min_chars: { kind: "int", min: 0, max: 200 },
  clip_max_chars: { kind: "int", min: 100, max: 4000 },
  max_clips: { kind: "int", min: 1, max: 1000 },
});

// One profile with every field present: valid overrides are kept, anything
// missing or unusable reads as its default.  Per-field tolerance matches
// parse_profiles (one stale choice must not reset the operator's other
// tuning); the clip_min/clip_max pair falls back together when inverted.
function _normalizeProfile(value) {
  const out = { ...DEFAULT_TTS_PROFILE };
  if (!value || typeof value !== "object" || Array.isArray(value)) return out;
  for (const key of Object.keys(TTS_PROFILE_FIELDS)) {
    if (!Object.prototype.hasOwnProperty.call(value, key)) continue;
    const spec = TTS_PROFILE_FIELDS[key];
    const raw = value[key];
    if (spec.kind === "int") {
      if (
        typeof raw !== "number" ||
        !Number.isInteger(raw) ||
        raw < spec.min ||
        raw > spec.max
      ) {
        continue;
      }
    } else if (spec.kind === "choice") {
      if (typeof raw !== "string" || !spec.choices.includes(raw)) continue;
    } else {
      if (typeof raw !== "string") continue;
      const text = raw.trim();
      if (text.length > MAX_MARKER_CHARS) continue;
      out[key] = text;
      continue;
    }
    out[key] = raw;
  }
  if (out.clip_min_chars >= out.clip_max_chars) {
    out.clip_min_chars = DEFAULT_TTS_PROFILE.clip_min_chars;
    out.clip_max_chars = DEFAULT_TTS_PROFILE.clip_max_chars;
  }
  return out;
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
// running into the next sentence.  Every pacing and structure decision comes
// from *profile* — a TtsProfile the caller resolved through getTtsProfile;
// omitted or unusable fields read as this module's built-in defaults, so a
// caller that predates profiles gets exactly the old reading.
//
// A whole list (UL, OL or DL, nested lists included) is ONE row while
// pause_list_item_ms is 0: each item gets a full stop if it lacks terminal
// punctuation and the items are joined with a space, with no pause between
// items and none after the list (listening tests: the model's own
// sentence-end pause was enough).  Above 0 every item is its own clip
// followed by that pause.  Table rows keep one clip each, separated by
// pause_table_row_ms; table_cell_joiner decides what goes between cells
// (comma or semicolon) or makes each cell its own clip.  table_header_mode
// decides whether the header row is read like any other row, spoken as
// "<header>: <cell>" prefixes, or skipped.  A short lead-in paragraph can
// fold into the first row.  Fenced code becomes code_marker, a diagram
// (rendered, loading, errored or unrendered mermaid) becomes diagram_marker,
// SVG/img/canvas/video/audio are skipped, and rendered math (KaTeX's
// accessibility text) is dropped.  Inline code is spoken when short (an
// identifier or flag) and replaced by the marker from inline_code_max_chars
// up (a command line or expression is noise read aloud), in place so the
// paragraph around it stays one clip.  An empty marker skips the element
// silently: no marker clip and no marker pause.
export function extractSpeechSegments(bodyEl, profile) {
  const p = _normalizeProfile(profile);
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
  const listItemMs = p.pause_list_item_ms;
  const listPerItem = listItemMs > 0;
  const PAUSE = {
    H1: p.pause_heading_ms,
    H2: p.pause_heading_ms,
    H3: p.pause_heading_ms,
    H4: p.pause_heading_ms,
    H5: p.pause_heading_ms,
    H6: p.pause_heading_ms,
    P: p.pause_paragraph_ms,
    BLOCKQUOTE: p.pause_paragraph_ms,
    TR: p.pause_table_row_ms,
  };
  let cur = null; // segment taking inline text; null until the next text
  let listDepth = 0; // inside a list everything folds into the one list row
  // Written with hex escapes for the quote characters; see shapeSpeechSegments.
  const ENDS = /[.!?:…][\x22\x27)\]”’]*$/;
  const itemBreak = () => {
    if (!cur) return;
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
  // A cell after the first arms cellSep; the joiner is written only once the
  // cell has text, so an empty cell adds no separator and padding around the
  // cell text does not leave "Plan , Price".  In prefix mode cellPrefix is the
  // same deferral for "Header: ", consumed by the cell's first text.
  let cellSep = "";
  let cellPrefix = "";
  let cellIndex = 0; // column of the cell being walked, for header prefixes
  let rowBase = 0; // non-space count at the start of the current row
  const addText = (str, isMarker) => {
    if (!cur) cur = newSeg(listDepth && listPerItem ? listItemMs : p.pause_paragraph_ms);
    if (cellPrefix) {
      if (!str.trim()) return;
      const prefix = cellPrefix;
      cellPrefix = "";
      if (cellSep) {
        trimEnd(cur);
        put(cur, cellSep);
        cellSep = "";
      }
      put(cur, prefix + ": ");
      put(cur, str.trimStart(), isMarker);
      return;
    }
    if (!cellSep) {
      put(cur, str, isMarker);
      return;
    }
    if (!str.trim()) return;
    trimEnd(cur);
    put(cur, cellSep);
    cellSep = "";
    put(cur, str.trimStart(), isMarker);
  };
  const cellsOf = (row) => {
    const cells = [];
    for (const c of row.childNodes) {
      if (c.nodeType === 1 && (c.tagName === "TD" || c.tagName === "TH")) cells.push(c);
    }
    return cells;
  };
  // The table's header row and the per-column texts a prefix mode speaks.  A
  // THEAD row is a header; without one, a first row made only of TH is (both
  // shapes renderers emit for a markdown table).  Row mode never reads any of
  // it, so the scan is skipped there: a 500-row table stays one linear walk.
  const tableInfo = (tableEl) => {
    const mode = p.table_header_mode;
    const info = { mode, joiner: p.table_cell_joiner, headers: [], headerRows: new Set() };
    if (mode === "row") return info;
    const rows = [];
    const collect = (node, inHead) => {
      for (const c of node.childNodes) {
        if (c.nodeType !== 1) continue;
        if (c.tagName === "TR") rows.push({ el: c, inHead });
        else if (c.tagName !== "TABLE") collect(c, inHead || c.tagName === "THEAD");
      }
    };
    collect(tableEl, false);
    for (const row of rows) if (row.inHead) info.headerRows.add(row.el);
    let headerRow = rows.find((r) => r.inHead);
    if (!headerRow && rows.length) {
      const cells = cellsOf(rows[0].el);
      if (cells.length && cells.every((c) => c.tagName === "TH")) headerRow = rows[0];
    }
    if (headerRow) {
      info.headerRows.add(headerRow.el);
      info.headers = cellsOf(headerRow.el).map((c) =>
        c.textContent.replace(/\s+/g, " ").trim(),
      );
    }
    return info;
  };
  // One table row.  Row mode folds into an enclosing list clip (a row is a
  // sentence of the list); otherwise the row is its own clip carrying the
  // table-row pause.  A header row is silent in prefix/skip mode, and the
  // pause joiner gives every cell its own clip instead.
  const walkRow = (row, info) => {
    if (info && info.mode !== "row" && info.headerRows.has(row)) return;
    cellIndex = 0;
    cellSep = "";
    if (info && info.joiner === "pause") {
      walk(row, info);
      cellSep = "";
      return;
    }
    if (info && listDepth && cur) {
      // A row inside a list item is a sentence of that item's clip: it starts
      // and ends a sentence, and starts clean so its first cell never sees
      // the previous row's text.
      itemBreak();
      rowBase = cur.sig;
      walk(row, info);
      itemBreak();
      cellSep = "";
      return;
    }
    rowBase = 0;
    cur = newSeg(p.pause_table_row_ms, { table: true });
    walk(row, info);
    cur = null;
    cellSep = "";
  };
  // One list, nested ones included.  In per-item mode the enclosing item's
  // clip ends where the nested list starts; otherwise every item is a
  // sentence of the one enclosing clip (a whole list, or the paragraph that
  // introduced it).
  const walkList = (listEl, table) => {
    if (listPerItem) cur = null;
    else if (listDepth) itemBreak();
    else cur = newSeg(0);
    listDepth++;
    walk(listEl, table);
    listDepth--;
    if (listPerItem) cur = null;
    else if (listDepth) itemBreak();
    else cur = null;
  };
  const walk = (node, table) => {
    for (const child of node.childNodes) {
      if (child.nodeType === 3) {
        addText(child.textContent);
        continue;
      }
      if (child.nodeType !== 1) continue;
      const cls = child.classList;
      if (cls.contains("katex") || cls.contains("katex-display")) continue;
      if (child.tagName === "CODE" && child.textContent.length >= p.inline_code_max_chars) {
        // Adjacent markers collapse, except across a cell boundary: each
        // long-code cell of a row keeps its own.  An empty marker skips the
        // code silently.
        if (p.code_marker && !(cur && cur.marker && !cellSep)) {
          addText(" " + p.code_marker + " ", true);
        }
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
        const marker = isDiagram ? p.diagram_marker : p.code_marker;
        if (marker) put(newSeg(p.pause_code_ms, { code: true }), marker);
        cur = listDepth ? newSeg(listPerItem ? listItemMs : 0) : null;
        continue;
      }
      const tag = child.tagName;
      if (tag === "TABLE") {
        cellSep = "";
        rowBase = 0;
        walk(child, tableInfo(child));
        cellSep = "";
        rowBase = 0;
        continue;
      }
      if (tag === "UL" || tag === "OL" || tag === "DL") {
        walkList(child, table);
        continue;
      }
      if (tag === "TR") {
        // Handled before the list branch so a table inside a list item reads
        // its header row and columns the same way a top-level table does.
        walkRow(child, table);
        continue;
      }
      if (listDepth && (tag === "LI" || tag === "DT" || tag === "DD" || PAUSE[tag] !== undefined)) {
        if (listPerItem && (tag === "LI" || tag === "DT" || tag === "DD")) {
          cur = newSeg(listItemMs, { item: true });
          walk(child, table);
          cur = null;
          continue;
        }
        itemBreak();
        walk(child, table);
        itemBreak();
        continue;
      }
      const isCell = tag === "TD" || tag === "TH";
      if (isCell) {
        const prefix = table && table.mode === "prefix" ? table.headers[cellIndex] || "" : "";
        if (table && table.joiner === "pause") {
          // Each cell its own clip; the row pause separates cells and rows.
          cur = newSeg(p.pause_table_row_ms, { table: true });
          if (prefix) put(cur, prefix + ": ");
          walk(child, table);
          cur = null;
          cellSep = "";
          cellIndex++;
          continue;
        }
        if (cur && cur.sig > rowBase) {
          cellSep = table && table.joiner === "semicolon" ? "; " : ", ";
        }
        if (prefix) cellPrefix = prefix;
        cellIndex++;
        walk(child, table);
        cellPrefix = "";
        continue;
      }
      const pauseMs = PAUSE[tag];
      if (pauseMs === undefined) {
        walk(child, table);
        continue;
      }
      cellSep = "";
      rowBase = 0;
      cur = newSeg(pauseMs);
      walk(child, table);
      cur = null; // a nested block ends this one; later text starts anew
      cellSep = ""; // a trailing empty cell must not arm the next block's text
    }
  };
  // The root is normally .msg-body (a div), but the walker also accepts a
  // fragment that IS the table or the list, as the pure-function tests pass
  // it: without this a root table is walked without its header information.
  const rootTag = bodyEl.tagName;
  if (rootTag === "TABLE") {
    cellSep = "";
    rowBase = 0;
    walkRow(bodyEl, tableInfo(bodyEl));
  } else if (rootTag === "UL" || rootTag === "OL" || rootTag === "DL") {
    walkList(bodyEl, null);
  } else {
    walk(bodyEl, null);
  }
  // Plain text (no block structure) is shaped like any other reply, so the
  // clip cap applies to it too.
  return shapeSpeechSegments(
    raw.map((s) => ({
      text: s.parts.join(""),
      pauseMs: s.pauseMs,
      code: s.code,
      table: s.table,
      item: s.item,
    })),
    p,
  );
}

// Normalise raw block text into clips a TTS model reads well: terminal
// punctuation so it pauses and drops pitch, fragments folded into the next
// clip (a five-word clip sounds clipped), long blocks cut at sentence ends
// (long inputs raise latency and fail on some backends, and the server
// rejects more than 8000 characters).  A sentence that is itself longer than
// that is wrapped at its last comma, else its last space.
export function shapeSpeechSegments(raw, profile) {
  const p = _normalizeProfile(profile);
  const minChars = p.clip_min_chars;
  const maxChars = p.clip_max_chars;
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
    rows.push({ text: text + end, pauseMs: r.pauseMs, code: !!r.code, table: !!r.table, item: !!r.item });
  }
  const merged = [];
  let carry = "";
  rows.forEach((r, i) => {
    const next = rows[i + 1];
    const text = carry ? carry + " " + r.text : r.text;
    // A code marker, a table row or a per-item list clip stands alone: the
    // profile asked for its own clip and its own pause.
    if (!r.code && !r.table && !r.item && text.length < minChars && next && !next.code) {
      carry = text;
      return;
    }
    carry = "";
    merged.push({ text, pauseMs: r.pauseMs });
  });
  const wrapLong = (s) => {
    const parts = [];
    while (s.length > maxChars) {
      const comma = s.lastIndexOf(",", maxChars - 1);
      const space = s.lastIndexOf(" ", maxChars);
      let cut = comma > 0 ? comma + 1 : space > 0 ? space : maxChars;
      // A hard cut must not land between the halves of a surrogate pair.
      if (cut === maxChars && comma <= 0 && space <= 0) {
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
      if (piece && piece.length + 1 + s.length > maxChars) {
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

// ===========================================================================
//  Streaming read-aloud: deltas -> completed clips
// ===========================================================================

// Where a streaming reply may be cut.  These are the marks the shaper treats
// as terminal punctuation (ENDS_SENTENCE); the feeder only decides WHERE a
// sentence ends, and shapeSpeechSegments still does the cleanup, the added
// period and the long-clip wrapping.  A line break is a clip too: markdown
// headings, list items and table rows end on one, and waiting for the next
// paragraph's period would keep the first audio silent.
const STREAM_MARKS = ".!?:\u2026";
const STREAM_CLOSERS = "\x22\x27)\\]\u201d\u2019";
// A period inside an abbreviation or an initial is not a sentence end.  The
// test is deliberately conservative: a false negative only folds two clips
// together (still correct speech), while a false positive cuts "e.g." or
// "Dr." into a clip of its own.
const STREAM_ABBREV =
  /(?:^|[\s(\x22\x27\[\u201c\u2018])(?:mr|mrs|ms|dr|prof|sr|jr|st|vs|etc|fig|no|approx|dept|est|inc|ltd|co|corp|mt|ft|al|ca|cf)\.$/i;
const STREAM_INITIAL = /(?:^|[\s(\x22\x27\[\u201c\u2018])[a-z]\.$/i;
const STREAM_DOTTED = /(?:^|[\s(\x22\x27\[\u201c\u2018])(?:[a-z]\.)+[a-z]\.$/i;

function _streamAbbreviation(prefix) {
  return (
    STREAM_DOTTED.test(prefix) ||
    STREAM_INITIAL.test(prefix) ||
    STREAM_ABBREV.test(prefix)
  );
}

// The index just past the first completed sentence in *text*, or 0 when the
// buffer ends inside one.  A mark only completes a sentence when whitespace
// follows it: that one rule keeps "3.14" whole and holds a delta that stops on
// "3." or "Dr." until the next delta shows what the mark belongs to.
function _streamCut(text, minChars) {
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === "\n") return i + 1;
    if (!STREAM_MARKS.includes(ch)) continue;
    // A run of marks ("!?", "...") is one sentence end, and closing quotes or
    // brackets ride along with it.
    let j = i + 1;
    while (j < text.length && STREAM_MARKS.includes(text[j])) j++;
    while (j < text.length && STREAM_CLOSERS.includes(text[j])) j++;
    if (j >= text.length) return 0; // the next delta decides
    if (!/\s/.test(text[j])) {
      i = j - 1; // a decimal, version or file name, not a sentence end
      continue;
    }
    if (ch === "." && _streamAbbreviation(text.slice(0, i + 1))) {
      i = j - 1;
      continue;
    }
    // A colon usually introduces a list; a one- or two-word lead-in ("Note:")
    // sounds clipped on its own, so it waits for enough text to be a clip.
    if (ch === ":" && text.slice(0, i).replace(/\s+/g, "").length < minChars) {
      i = j - 1;
      continue;
    }
    return j;
  }
  return 0;
}

// A streamed delta is raw model text, not the rendered bubble the finished-
// reply path reads, so Markdown markers would be spoken literally ("hash hash
// summary").  Drop them per completed sentence: links keep their label, code
// keeps its text, and emphasis, headings, bullets and fences lose their marks.
function _streamPlainText(text) {
  return text
    .replace(/```[^\n]*/g, " ")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/https?:\/\/\S+/g, " ")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s*(?:[-*+]|\d+[.)])\s+/gm, "")
    .replace(/^\s*>\s?/gm, "")
    .replace(/(\*\*|__|\*|_|~~|`)(?=\S)([^]*?\S)\1/g, "$2")
    .replace(/[*_`#~]/g, "");
}

// Turns a live reply's text deltas into clips for a streaming run.  Each
// delta is appended to a buffer; every completed sentence is shaped and
// handed to onClip immediately, while the trailing partial sentence waits for
// the deltas that finish it.  end() flushes the remainder, cancel() drops it.
//
// Shaping stays in shapeSpeechSegments: this is one boundary scan, not a
// second segmenter.  A completed sentence is one row, so the shaper still
// normalises whitespace, supplies a terminal period and wraps a very long
// sentence at the profile's clip_max_chars.
export function createStreamFeeder(opts) {
  opts = opts || {};
  const profile = _normalizeProfile(opts.profile);
  const onClip = opts.onClip || (() => {});
  let pending = "";
  let closed = false;
  const emit = (raw) => {
    const text = _streamPlainText(raw);
    if (!text.trim()) return;
    for (const clip of shapeSpeechSegments([{ text, pauseMs: 0 }], profile)) {
      if (clip.text) onClip(clip);
    }
  };
  return {
    push(delta) {
      if (closed) return;
      pending += delta || "";
      for (;;) {
        const cut = _streamCut(pending, profile.clip_min_chars);
        if (!cut) return;
        const text = pending.slice(0, cut);
        pending = pending.slice(cut);
        emit(text);
      }
    },
    end() {
      if (closed) return;
      closed = true;
      emit(pending);
      pending = "";
      if (opts.onEnd) opts.onEnd();
    },
    cancel() {
      closed = true;
      pending = "";
    },
  };
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

// The stop() of every player with a run in progress on this page.  Each pane
// owns its own player, so without this a second session's play would talk over
// the first; starting a run stops the others through their own stop(), which
// also resets their buttons.  A run leaves the set in stop(), the one exit
// every way of ending a run goes through.
const _activeTtsRuns = new Set();

// One pane's read-aloud player.  Options:
//   base            transport prefix ("" or "/node/{id}"), or a function
//                   returning it when the pane's base can change after
//                   construction.
//   announce        live-region announcer; defaults to a module-owned region.
//   authGeneration  returns a token that changes on logout or account switch;
//                   defaults to auth.js's.  A run holding an older token stops.
// Returns { play(el, btn), stream(), stop(), isPlaying(), activeButton(),
// audio() }.
export function createTtsPlayer(opts) {
  opts = opts || {};
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
  // The live streaming run, if any.  It has no button, but it owns the same
  // one-voice-per-page slot and the same stop().
  let streamRun = null;
  // Settles the finished-reply run in progress, if any.  stop() is the one
  // exit every way of ending a run goes through, but a stopped clip never
  // fires "ended", so a run parked on one would otherwise wait forever.
  let playSettle = null;

  function stop() {
    _activeTtsRuns.delete(stop);
    seq++; // invalidate any run in progress
    const finish = playSettle;
    playSettle = null;
    if (finish) finish("cancelled");
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
    // Dropping the queued clips is what makes a stop immediate: the current
    // clip is paused above, and nothing already spoken-over may follow it.
    const stream = streamRun;
    if (stream) {
      streamRun = null;
      stream._drop();
    }
  }

  // A finished reply's run handle: the run's own promise (as play() always
  // returned — click handlers await it for the end of the run and attach
  // .catch to it) carrying `done`, which settles once with {reason, heard} —
  // "finished" after the last clip's audio ends (or immediately when there is
  // nothing speakable), "cancelled" when a stop ends the run first.  Toggling
  // the playing button off starts no run and returns nothing.
  function play(el, btn) {
    // Toggle: clicking the active button (or any while playing) stops first.
    if (activeBtn) {
      const wasThis = activeBtn === btn;
      stop();
      if (wasThis) return;
    }
    let settled = false;
    let settleDone;
    const heard = [];
    const done = new Promise((resolve) => {
      settleDone = resolve;
    });
    const finish = (reason) => {
      if (settled) return;
      settled = true;
      if (playSettle === finish) playSettle = null;
      settleDone({ reason, heard: heard.slice() });
    };
    // The body runs to its first await synchronously, so it owns the voice
    // slot before this returns.  The catch settles a run that threw before its
    // own try (a DOM or Audio constructor), which must not leave the
    // hands-free loop waiting; the returned promise keeps rejecting for a
    // caller that awaits it, exactly as before.
    const run = playRun(el, btn, heard, finish);
    run.catch(() => finish("cancelled"));
    run.heard = heard;
    run.done = done;
    return run;
  }

  async function playRun(el, btn, heard, finish) {
    // An explicit click supersedes a pending or playing streaming run.  It is
    // not necessarily in _activeTtsRuns yet — a run that has not reached its
    // first clip is still waiting on the config probe — so stop it by hand or
    // its first sentence would silence the reply the user just chose.
    if (streamRun) streamRun.cancel();
    // One voice per page: another pane's run ends before this one starts.
    for (const other of [..._activeTtsRuns]) other();
    _activeTtsRuns.add(stop);
    // Only now, after the supersession calls above (a stream cancel runs this
    // player's own stop()), so our own teardown cannot settle the run it is
    // starting.  From here on stop() ends this run, so it settles it.
    playSettle = finish;
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
      // same style and the same pacing, and a change mid-run cannot split one
      // reading in two.  All three ride the one memoized styles probe; a
      // failed probe is Default, normal speed and the built-in profile.
      let requestStyle = "";
      try {
        requestStyle = await _requestStyle(baseOf());
      } catch (e) {
        requestStyle = ""; // a failed probe is Default, never a broken play
      }
      let rate = 1;
      try {
        rate = await getTtsPlaybackRate(baseOf());
      } catch (e) {
        rate = 1; // a failed probe plays at normal speed
      }
      let profile = DEFAULT_TTS_PROFILE;
      try {
        profile = await getTtsProfile(baseOf());
      } catch (e) {
        profile = DEFAULT_TTS_PROFILE; // a failed probe reads like the old constants
      }
      // Inside the try: the walker is recursive and can throw on pathological
      // nesting, which must reach the toast rather than reject silently.
      const segments = extractSpeechSegments(
        el.querySelector(".msg-body") || el,
        profile,
      );
      if (!segments.length) {
        finish("finished"); // nothing to say: done, not cancelled
        stop();
        return;
      }
      // One request per clip, so a table or list of hundreds of rows would
      // queue hundreds of syntheses; read the first max_clips and say so.
      const maxClips = profile.max_clips;
      if (segments.length > maxClips) {
        segments.length = maxClips;
        segments[maxClips - 1].pauseMs = 0;
        showToast(
          "Read-aloud stopped after the first " +
            maxClips +
            " sections; the rest of this reply was skipped.",
          "warn",
        );
      }
      // preservesPitch is the browsers' default; set it so a faster read
      // never turns into a chipmunk on an engine that defaults otherwise.
      audio.preservesPitch = true;
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
          // Loading a src resets playbackRate to defaultPlaybackRate, so set
          // both, every clip: otherwise speed snaps back at each paragraph.
          audio.defaultPlaybackRate = rate;
          audio.src = url;
          audio.playbackRate = rate;
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
        // Like a streaming run: the token test comes before heard, so an
        // "ended" that lands after a stop is not an acknowledgement of a clip
        // this run played.
        if (token !== seq) return;
        heard.push(segments[i].text);
        URL.revokeObjectURL(url);
        audioEl = null;
        if (segments[i].pauseMs) {
          await new Promise((resolve) => {
            timer = setTimeout(resolve, segments[i].pauseMs);
          });
        }
        if (token === seq && stopIfAuthChanged()) return;
      }
      // The reply ran out and its last clip ended: settle BEFORE stop(), whose
      // teardown is also every cancelled path's exit and would otherwise
      // report this completed run as cancelled.
      finish("finished");
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

  // Read a reply aloud while it is still being written.  Deltas go into a
  // feeder, which hands over one completed sentence at a time; those clips
  // are fetched and played with the same one-in-flight/prefetch-one-ahead
  // rules as play().  Returns the run: push(delta), end() to flush the tail,
  // cancel() to stop and drop everything queued, and `heard` — the clips
  // whose audio actually fired "ended" (a fetched or prefetched clip is not
  // heard).
  //
  // audio.tts_stream is resolved once per run from the memoized styles probe.
  // Off, a run holds its text, resolves to nothing and never fetches a clip,
  // so the panes can feed deltas unconditionally and a deployment that has
  // not enabled streaming behaves exactly as it did before this existed.
  function stream() {
    // One streaming run per player: a new reply ends the old run and drops the
    // clips it had queued but not yet spoken.
    if (streamRun) streamRun.cancel();
    const run = { heard: [] };
    // The hands-free loop waits on this: one settlement per run, first value
    // wins, so a teardown that follows a natural finish cannot relabel it.
    let settled = false;
    let settleDone;
    run.done = new Promise((resolve) => {
      settleDone = resolve;
    });
    const finish = (reason) => {
      if (settled) return;
      settled = true;
      settleDone({ reason, heard: run.heard.slice() });
    };
    let inbox = ""; // deltas held until the config probe resolves
    let feeder = null;
    let queue = [];
    let ended = false; // the reply's text is complete (stream_end, or end())
    let started = false;
    let cancelled = false;
    let capped = false;
    let emitted = 0;
    let wake = null;
    let ready = false;
    let enabled = false;
    let announced = false;
    let style = "";
    let rate = 1;
    let profile = DEFAULT_TTS_PROFILE;
    let token = 0;
    let abort = null;
    let audio = null;

    const wakeLoop = () => {
      const resolve = wake;
      wake = null;
      if (resolve) resolve();
    };
    const drop = () => {
      cancelled = true;
      queue = [];
      inbox = "";
      if (feeder) feeder.cancel();
      wakeLoop();
      // Every cancelled exit funnels through here (stop(), cancel(), a
      // superseding run); a run that already settled keeps its value.
      finish("cancelled");
    };
    // Queueing a clip also starts the run, so a streaming run enters
    // _activeTtsRuns at the same moment a clicked play() does — not when the
    // feeder is created, which would silence another pane before this one has
    // anything to say.
    const enqueue = (clip) => {
      if (cancelled) return;
      // One request per clip, exactly like a finished reply: a runaway reply
      // must not queue hundreds of syntheses after the cap said stop.
      if (emitted >= profile.max_clips) {
        if (!capped) {
          capped = true;
          showToast(
            "Read-aloud stopped after the first " +
              profile.max_clips +
              " sections; the rest of this reply was skipped.",
            "warn",
          );
        }
        return;
      }
      emitted++;
      queue.push(clip);
      if (started) wakeLoop();
      else start();
    };
    const fetchClip = async (text) => {
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
    const playLoop = async () => {
      let next = null; // {text, promise}: the one prefetched clip
      try {
        // Clips are authenticated one by one; a logout or account switch
        // mid-run must not keep speaking the previous session's reply.
        const generation = generationOf();
        const stopIfAuthChanged = () => {
          if (generation === generationOf()) return false;
          stop();
          return true;
        };
        for (;;) {
          if (token !== seq) return;
          if (!queue.length) {
            if (ended) break;
            await new Promise((resolve) => {
              wake = resolve;
            });
            continue;
          }
          const clip = queue.shift();
          let blob;
          if (next && next.text === clip.text) {
            blob = await next.promise;
            next = null;
          } else {
            blob = await fetchClip(clip.text);
          }
          if (token !== seq) return;
          if (stopIfAuthChanged()) return;
          // Prefetch one ahead once this clip's blob has landed, never
          // earlier: one synthesis is in flight at a time.
          if (queue.length && !next) {
            next = { text: queue[0].text, promise: fetchClip(queue[0].text) };
            next.promise.catch(() => {}); // surfaced when awaited, not before
          }
          const url = URL.createObjectURL(blob);
          audioEl = audio;
          if (!announced) {
            announced = true;
            announce("Playing response.");
          }
          await new Promise((resolve, reject) => {
            const settle = (fn, arg) => {
              audio.removeEventListener("ended", onEnded);
              audio.removeEventListener("error", onError);
              fn(arg);
            };
            const onEnded = () => settle(resolve);
            const onError = () => settle(reject, new Error("Audio playback failed"));
            audio.addEventListener("ended", onEnded);
            audio.addEventListener("error", onError);
            // Loading a src resets playbackRate to defaultPlaybackRate, so set
            // both, every clip: otherwise speed snaps back at each sentence.
            audio.defaultPlaybackRate = rate;
            audio.src = url;
            audio.playbackRate = rate;
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
          // The token test comes BEFORE heard: an "ended" that lands after a
          // stop is not an acknowledgement of a clip this run played.
          if (token !== seq) return;
          run.heard.push(clip.text);
          URL.revokeObjectURL(url);
          audioEl = null;
        }
        // The reply's text ended and its last queued clip's audio ended: this
        // is a completed run, settled BEFORE stop(), whose teardown is also
        // every cancelled path's exit.
        finish("finished");
        stop();
      } catch (err) {
        if (token !== seq) return; // aborted by a stop or a newer run
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
    };
    const start = () => {
      if (started || cancelled) return;
      started = true;
      // One voice per page.  Detach this run while our own stop() runs (it
      // must not cancel the run it is starting), then reattach it so every
      // later stop() — this pane's or another pane's — drops the queue.
      streamRun = null;
      for (const other of [..._activeTtsRuns]) other();
      _activeTtsRuns.add(stop);
      token = ++seq;
      abort = new AbortController();
      abortCtl = abort;
      // One element for the whole run: iOS Safari only lets an element that a
      // gesture unlocked play later, and swapping src keeps that one element.
      audio = new Audio();
      audio.preservesPitch = true;
      streamRun = run;
      playLoop();
    };
    run.push = (delta) => {
      if (cancelled || ended) return;
      const text = delta || "";
      if (!ready) {
        inbox += text;
        return;
      }
      if (enabled && feeder) feeder.push(text);
    };
    run.end = () => {
      if (cancelled || ended) return;
      ended = true;
      if (ready && enabled && feeder) feeder.end();
      // No audio will ever play — the setting is off, or the text shaped to
      // nothing — so the run is over the moment its text is, and the voice
      // loop must not wait for an "ended" that cannot come.  A started run is
      // settled by playLoop once its queue drains.
      if (ready && !started) finish("finished");
    };
    run.cancel = () => {
      if (streamRun === run) stop(); // clears the player and drops the queue
      else drop();
    };
    run._drop = drop;
    streamRun = run;
    // All four ride the one memoized styles probe.  A failed probe, a server
    // that predates a field and an unusable value each read as the defaults —
    // the same degradation a finished reply gets.
    Promise.all([
      getTtsStreamEnabled(baseOf()),
      _requestStyle(baseOf()).catch(() => ""),
      getTtsPlaybackRate(baseOf()).catch(() => 1),
      getTtsProfile(baseOf()).catch(() => DEFAULT_TTS_PROFILE),
    ]).then(([on, chosenStyle, chosenRate, chosenProfile]) => {
      if (cancelled) return;
      enabled = on === true;
      style = chosenStyle;
      rate = chosenRate;
      profile = chosenProfile;
      ready = true;
      if (!enabled) {
        inbox = ""; // streaming is off: nothing was fetched and nothing plays
        if (ended) finish("finished");
        return;
      }
      feeder = createStreamFeeder({
        profile,
        onClip: enqueue,
        onEnd: () => {
          ended = true;
          wakeLoop();
        },
      });
      const text = inbox;
      inbox = "";
      if (text) feeder.push(text);
      if (ended) feeder.end();
      // end() landed while the probe was still out; if it shaped to no clip at
      // all, nothing above started a loop to settle this run.
      if (ended && !started) finish("finished");
    }).catch(() => {
      // A run that cannot even start (a throwing base() or Audio constructor)
      // is a cancelled run: settle it rather than leave the voice loop waiting.
      run.cancel();
    });
    return run;
  }

  return {
    play,
    stop,
    stream,
    isPlaying: () => activeBtn !== null,
    activeButton: () => activeBtn,
    audio: () => audioEl,
  };
}
