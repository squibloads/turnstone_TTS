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
