// ===========================================================================
//  turnstone live spectrum strip — shared module (shared_static/voice_viz.js)
//
//  A thin strip above the coordinator's CHILDREN list that shows the live
//  frequency content of whoever is talking in a voice session: the mic while
//  the user speaks, the reply's audio element while the AI speaks.  It is a
//  real frequency response read from an AnalyserNode, not a decorative
//  animation, so a bar moves only when that band carries energy.
//
//  The strip never plays audio.  It only reads from the sources it is handed,
//  so playback stays the player's job and the mic keeps its own recording path.
//
//  ES module.  House style: programmatic DOM, NO innerHTML.
// ===========================================================================

// Bars across the strip.  Thirty-two is what fits the strip's 48px height
// with a visible gap between bars at the sidebar's narrowest width.
export const VIZ_BAR_COUNT = 32;

// The band the bars cover.  80 Hz is the lowest fundamental a voice reaches
// and 8 kHz is where speech intelligibility is already spent.  Above 8 kHz is
// mostly sibilance and room noise, and an uncapped top would crowd the bars
// into the top few octaves so the bars that carry the voice barely move.
export const VIZ_MIN_HZ = 80;
export const VIZ_MAX_HZ = 8000;

// The analyser's FFT size.  Bars are log-spaced, so the lowest bars are only a
// few Hz wide, and a bin is wider than a bar there unless the FFT is long.  At
// 2048 two neighbouring bars share a bin and move together.  8192 gives every
// bar its own bin and keeps the adjacent-bar spacing close to the ideal ratio.
// The cost is window length: ~186 ms at 44.1k, ~171 ms at 48k, which is the
// lag a bar shows behind the sound.  4096 halves that lag but widens the
// spacing at the bottom of the band (adjacent ratios reach ~1.20 against an
// ideal ~1.15).  The trade is deliberate and open to the coordinator.
export const VIZ_FFT_SIZE = 8192;

// Maps each bar to the half-open FFT-bin range [start, end) it reads.
//
// Bars are log-spaced because pitch is perceived logarithmically: equal ratios
// per bar keep each bar a similar musical interval, where linear spacing would
// leave most bars inside the top octave.  Each bar's edge is rounded up to a
// whole bin, and a bar too narrow to contain one still reads the bin at its
// geometric centre, so no bar is ever empty and none goes dead.
export function barBinRanges(bars, sampleRate, fftSize) {
  const binHz = sampleRate / fftSize;
  const ratio = VIZ_MAX_HZ / VIZ_MIN_HZ;
  const out = [];
  for (let i = 0; i < bars; i++) {
    const lo = VIZ_MIN_HZ * Math.pow(ratio, i / bars);
    const hi = VIZ_MIN_HZ * Math.pow(ratio, (i + 1) / bars);
    let start = Math.ceil(lo / binHz);
    let end = Math.ceil(hi / binHz);
    if (end <= start) {
      const mid = Math.round(Math.sqrt(lo * hi) / binHz);
      start = mid;
      end = mid + 1;
    }
    out.push([start, end]);
  }
  return out;
}

// One level per bar from a frequency-domain byte spectrum (0..255), taking the
// loudest bin in each bar's range.  A peak rather than a mean: a mean dilutes a
// single strong harmonic across its neighbours and the bar barely lifts.
export function barLevels(bins, ranges) {
  const levels = new Array(ranges.length);
  for (let i = 0; i < ranges.length; i++) {
    const [start, end] = ranges[i];
    let peak = 0;
    for (let b = start; b < end && b < bins.length; b++) {
      if (bins[b] > peak) peak = bins[b];
    }
    levels[i] = peak / 255;
  }
  return levels;
}

// Colour follows who is speaking.  Both are console tokens from base.css: the
// AI is the accent, the user the muted grey-blue, and silence settles to the
// same muted token as a flat line.  No palette of its own.
const _COLOUR_AI = "var(--accent)";
const _COLOUR_MUTED = "var(--fg-dim)";

function _speakerColour(speaker) {
  return speaker === "ai" ? _COLOUR_AI : _COLOUR_MUTED;
}

// One AudioContext for the strip's life, created on first use and never before:
// a page that never shows the strip never builds an audio graph.  Browsers start
// it suspended, so the coordinator resumes it from the Voice click.
let _sharedContext = null;

function _audioContext() {
  if (!_sharedContext) {
    const Ctor =
      typeof AudioContext !== "undefined" ? AudioContext : globalThis.webkitAudioContext;
    _sharedContext = new Ctor();
  }
  return _sharedContext;
}

// Reduced motion gets a single level meter, not the bar field: the bars are the
// part that moves, and a level still tells the user the mic or the voice is live.
function _reducedMotion() {
  return (
    typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

// The strip for one coordinator pane.  opts:
//   host    the element the strip mounts into.
//   before  the element it mounts ahead of (the CHILDREN section).
// Returns { show, hide, attachStream, attachAudio, detach, setSpeaker,
// colourFor, isActive, isReducedMotion, barCount, contextState,
// resumeFromGesture, stop }.
export function createSpectrumStrip(opts) {
  const host = opts.host;
  const before = opts.before || null;
  const reduced = _reducedMotion();
  const barCount = reduced ? 1 : VIZ_BAR_COUNT;

  const el = document.createElement("div");
  el.className = "voice-viz";
  el.setAttribute("aria-hidden", "true");
  const canvas = document.createElement("canvas");
  canvas.className = "voice-viz-canvas";
  el.appendChild(canvas);

  // source -> { node, analyser, ranges, bins } for each attached source.  The
  // key is the speaker, so one source per speaker is live at a time.
  const sources = new Map();
  const tapped = new WeakMap();
  let shown = false;
  let speaker = null;
  let frameId = null;
  let levels = new Array(barCount).fill(0);

  function _analyse(node) {
    const ctx = _audioContext();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = VIZ_FFT_SIZE;
    analyser.smoothingTimeConstant = 0.6;
    // Analysed but never connected to the destination: the analyser is a
    // branch off the source, so the mic is read without being heard back.
    node.connect(analyser);
    return analyser;
  }

  function _binsFor(analyser) {
    return new Uint8Array(analyser.frequencyBinCount);
  }

  function _attachSource(key, node) {
    detach(key);
    const analyser = _analyse(node);
    const ctx = _audioContext();
    sources.set(key, {
      node,
      analyser,
      bins: _binsFor(analyser),
      ranges: barBinRanges(barCount, ctx.sampleRate, VIZ_FFT_SIZE),
    });
    _scheduleFrame();
  }

  // Releases the analyser branch this strip hung off the source.  The source
  // itself may be shared with the speaker route (a reply's element source), so
  // only the branch is disconnected, never the source.
  function _releaseBranch(src) {
    try {
      src.node.disconnect(src.analyser);
    } catch (e) {
      /* the branch was already released */
    }
  }

  function attachStream(stream, key) {
    const ctx = _audioContext();
    _attachSource(key, ctx.createMediaStreamSource(stream));
    setSpeaker(key);
  }

  // A reply's element.  captureStream taps a copy of the element's output, so
  // playback is untouched.  Without it, the element has to be routed through a
  // media-element source, which is only allowed once per element and must reach
  // the destination or the reply goes silent.
  function attachAudio(el2, key) {
    const ctx = _audioContext();
    if (typeof el2.captureStream === "function") {
      const captured = el2.captureStream();
      _attachSource(key, ctx.createMediaStreamSource(captured));
    } else {
      let node = tapped.get(el2);
      if (!node) {
        node = ctx.createMediaElementSource(el2);
        node.connect(ctx.destination);
        tapped.set(el2, node);
      }
      _attachSource(key, node);
    }
    setSpeaker(key);
  }

  function detach(key) {
    const src = sources.get(key);
    if (!src) return;
    _releaseBranch(src);
    sources.delete(key);
    if (speaker === key) speaker = null;
    if (sources.size === 0) _cancelFrame();
  }

  function setSpeaker(next) {
    speaker = next || null;
  }

  function colourFor() {
    return _speakerColour(speaker);
  }

  function isActive() {
    return frameId !== null;
  }

  function _visible() {
    return typeof document === "undefined" || document.visibilityState !== "hidden";
  }

  function _scheduleFrame() {
    if (frameId !== null || !shown || sources.size === 0 || !_visible()) return;
    frameId = requestAnimationFrame(_frame);
  }

  function _cancelFrame() {
    if (frameId === null) return;
    cancelAnimationFrame(frameId);
    frameId = null;
  }

  function _frame() {
    frameId = null;
    if (!shown || sources.size === 0 || !_visible()) return;
    _draw();
    _scheduleFrame();
  }

  // One bar per band, painted in the speaker's colour.  Silence still paints
  // every bar at its floor, so an idle strip is a visible baseline, not a gap.
  // Reduced motion paints a single meter bar that spans the strip's width.
  function _draw() {
    for (const src of sources.values()) {
      src.analyser.getByteFrequencyData(src.bins);
      levels = barLevels(src.bins, src.ranges);
    }
    const ctx2d = canvas.getContext("2d");
    if (!ctx2d) return;
    const w = canvas.clientWidth || 0;
    const h = canvas.clientHeight || 0;
    ctx2d.clearRect(0, 0, w, h);
    ctx2d.fillStyle = colourFor();
    const count = levels.length;
    if (reduced) {
      const level = levels[0] || 0;
      ctx2d.fillRect(0, h - Math.max(2, level * h), w, Math.max(2, level * h));
      return;
    }
    const slot = w / count;
    const gap = Math.max(1, slot * 0.25);
    for (let i = 0; i < count; i++) {
      const bar = Math.max(2, levels[i] * h);
      ctx2d.fillRect(i * slot, h - bar, slot - gap, bar);
    }
  }

  function _onVisibility() {
    if (!_visible()) _cancelFrame();
    else _scheduleFrame();
  }

  function show() {
    if (shown) return;
    shown = true;
    if (!el.parentNode) host.insertBefore(el, before);
    if (typeof document !== "undefined" && document.addEventListener) {
      document.addEventListener("visibilitychange", _onVisibility);
    }
    _scheduleFrame();
  }

  function hide() {
    if (!shown) return;
    shown = false;
    _cancelFrame();
    if (typeof document !== "undefined" && document.removeEventListener) {
      document.removeEventListener("visibilitychange", _onVisibility);
    }
    for (const key of [...sources.keys()]) detach(key);
    if (el.parentNode) el.parentNode.removeChild(el);
  }

  function contextState() {
    return _sharedContext ? _sharedContext.state : "suspended";
  }

  async function resumeFromGesture() {
    const ctx = _audioContext();
    if (ctx.state !== "running") await ctx.resume();
  }

  return {
    show,
    hide,
    attachStream,
    attachAudio,
    detach,
    setSpeaker,
    colourFor,
    isActive,
    isReducedMotion: () => reduced,
    barCount: () => barCount,
    contextState,
    resumeFromGesture,
    stop: hide,
    el,
  };
}
