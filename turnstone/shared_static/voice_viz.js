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

// How a bar moves between frames.  A bar rises at once with the sound but falls
// by a fraction of its height each frame, so it settles to the floor rather
// than snapping; the falling peak marker holds the recent maximum and drops more
// slowly still, so a burst stays readable after the bar has fallen.  The fall
// rates are per-frame fractions: about 60 frames is a second at refresh rate.
const VIZ_BAR_FALL = 0.18;
const VIZ_PEAK_FALL = 0.02;

// Colour follows who is speaking.  Both are console tokens from base.css: the
// AI is the accent, the user the muted grey-blue, and silence settles to the
// same muted token as a flat line.  No palette of its own.
//
// The token NAMES are stored, not their values.  A canvas cannot paint a
// custom-property expression: assigning "var(--accent)" to fillStyle is
// invalid, the assignment is ignored, and the canvas keeps its previous fill
// (black).  Each colour is resolved from computed styles before it is painted.
const _COLOUR_AI = "--accent";
const _COLOUR_MUTED = "--fg-dim";

// A last-resort CSS colour keyword for a page that does not define the console
// tokens.  "currentColor" is always resolvable, so the strip stays visible
// instead of painting black; it is never the intended colour.
const _COLOUR_FALLBACK = "currentColor";

// The computed values, or null when the element is not in a document yet (a
// detached element has no computed custom properties) or the page has no
// tokens.  Null is not cached: the next frame tries again.
function _resolveColours(el) {
  if (typeof getComputedStyle !== "function") return null;
  let styles;
  try {
    styles = getComputedStyle(el);
  } catch (e) {
    return null;
  }
  if (!styles) return null;
  const accent = styles.getPropertyValue(_COLOUR_AI).trim();
  const muted = styles.getPropertyValue(_COLOUR_MUTED).trim();
  return accent && muted ? { accent, muted } : null;
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
  // element -> its media-element source.  A source is allowed only once per
  // element and the player reuses one element for every clip of a run, so the
  // source lives as long as the element, never as long as the clip.
  const elementSources = new WeakMap();
  // A reply's element waiting for the shared context to start.  Routing it
  // into a suspended context would send its audio to a graph that is not
  // running, muting the reply, so native playback is left alone until then.
  let pendingAudio = null;
  let stateListener = null;
  let shown = false;
  let speaker = null;
  let frameId = null;
  let levels = new Array(barCount).fill(0);
  // The falling peak markers, one per bar, carried across frames.
  let peaks = new Array(barCount).fill(0);
  // Resolved colour values for the current theme, and the observer that
  // notices a theme switch.  Both are created lazily: a page with no strip
  // pays neither.
  let colours = null;
  let themeObserver = null;

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
    try {
      const ctx = _audioContext();
      _attachSource(key, ctx.createMediaStreamSource(stream));
    } catch (e) {
      // A closed or trackless stream leaves the strip silent, never the mic.
    } finally {
      setSpeaker(key);
    }
  }

  // Stops waiting for the context to start.  Called when the wait resolves and
  // when the source that wanted it is detached or the strip is hidden.
  function _clearPendingAudio() {
    pendingAudio = null;
    if (!stateListener) return;
    const ctx = _sharedContext;
    if (ctx && typeof ctx.removeEventListener === "function") {
      ctx.removeEventListener("statechange", stateListener);
    }
    stateListener = null;
  }

  function _tapElement(el2, key) {
    const ctx = _audioContext();
    let node = elementSources.get(el2);
    if (!node) {
      if (ctx.state !== "running") {
        // The context cannot process anything yet.  Leave the element on its
        // native playback path and tap it once the Voice click (or any other
        // gesture the coordinator uses) resumes the context.
        _clearPendingAudio();
        pendingAudio = { el: el2, key };
        if (typeof ctx.addEventListener === "function") {
          stateListener = () => {
            const pending = pendingAudio;
            _clearPendingAudio();
            if (!pending || ctx.state !== "running") return;
            try {
              _tapElement(pending.el, pending.key);
            } catch (e) {
              /* see attachAudio */
            }
          };
          ctx.addEventListener("statechange", stateListener);
        }
        setSpeaker(key);
        return;
      }
      node = ctx.createMediaElementSource(el2);
      // Required, not optional: a media-element source detaches the element
      // from the output, so this graph is then the only route to the speakers.
      node.connect(ctx.destination);
      elementSources.set(el2, node);
    }
    _attachSource(key, node);
    setSpeaker(key);
  }

  // A reply's audio element, which is the one thing that actually plays.  The
  // player reports a fresh element before its first src exists, so tapping
  // captureStream() here would hand createMediaStreamSource an empty track
  // list (Chromium throws; Firefox has no captureStream on media elements at
  // all).  The element itself is the source: it sees every clip, including the
  // src change between them.
  function attachAudio(el2, key) {
    try {
      _tapElement(el2, key);
    } catch (e) {
      // The strip is a visual; a refused graph must not break playback.
      setSpeaker(key);
    }
  }

  function detach(key) {
    if (pendingAudio && pendingAudio.key === key) _clearPendingAudio();
    const src = sources.get(key);
    if (src) {
      _releaseBranch(src);
      sources.delete(key);
      if (sources.size === 0) _cancelFrame();
    }
    if (speaker === key) speaker = null;
  }

  function setSpeaker(next) {
    speaker = next || null;
  }

  // The resolved colours for this theme, falling back to a paintable keyword
  // on a page that has not defined the console tokens.
  function _colours() {
    if (!colours) colours = _resolveColours(el);
    return colours || { accent: _COLOUR_FALLBACK, muted: _COLOUR_FALLBACK };
  }

  function _speakerColour(next) {
    const resolved = _colours();
    return next === "ai" ? resolved.accent : resolved.muted;
  }

  // A theme switch changes what the tokens compute to; the canvas must repaint
  // with the new values immediately, even while no source is attached (the
  // frame loop is stopped then, so the resting floor would keep the old paint).
  function _invalidateColours() {
    colours = null;
    if (shown) _draw();
  }

  function _watchTheme() {
    if (themeObserver || typeof MutationObserver !== "function") return;
    if (typeof document === "undefined" || !document.documentElement) return;
    themeObserver = new MutationObserver(_invalidateColours);
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
  }

  function _unwatchTheme() {
    if (!themeObserver) return;
    themeObserver.disconnect();
    themeObserver = null;
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

  // The drawing surface, in CSS pixels, with a backing store sized for the
  // device pixel ratio.  A canvas left at its 300x150 default stretches that
  // drawing space over the strip: the bars land in the top third with dead
  // space below, and nothing lines up with the 48px design.  Resizing the
  // backing store also resets the context, so the transform is applied here,
  // every frame, rather than once.
  function _surface() {
    const w = canvas.clientWidth || 0;
    const h = canvas.clientHeight || 0;
    if (w <= 0 || h <= 0) return null;
    const ratio =
      typeof devicePixelRatio === "number" && devicePixelRatio > 0 ? devicePixelRatio : 1;
    const pixelW = Math.max(1, Math.round(w * ratio));
    const pixelH = Math.max(1, Math.round(h * ratio));
    if (canvas.width !== pixelW || canvas.height !== pixelH) {
      canvas.width = pixelW;
      canvas.height = pixelH;
    }
    const ctx2d = canvas.getContext("2d");
    if (!ctx2d) return null;
    ctx2d.setTransform(ratio, 0, 0, ratio, 0, 0);
    return { ctx2d, w, h };
  }

  // One bar per band, painted in the speaker's colour.  Silence still paints
  // every bar at its floor, so an idle strip is a visible baseline, not a gap.
  // Reduced motion paints a single meter bar that spans the strip's width.
  function _draw() {
    // The live reading for this frame: the spectrum of the one speaking, or
    // silence when nothing is attached, so the bars still fall to the floor.
    let target = new Array(barCount).fill(0);
    for (const src of sources.values()) {
      src.analyser.getByteFrequencyData(src.bins);
      target = barLevels(src.bins, src.ranges);
    }
    for (let i = 0; i < barCount; i++) {
      const t = target[i] || 0;
      // A bar rises with the sound and falls toward it, so it settles.
      levels[i] = t >= levels[i] ? t : levels[i] - (levels[i] - t) * VIZ_BAR_FALL;
      // The marker jumps to any new height and otherwise falls more slowly.
      // Never below the bar it marks: a fixed fall step that crosses zero
      // while the bar still stands would drop the marker to the floor for one
      // frame and pop it back on the next reading.
      peaks[i] =
        levels[i] >= peaks[i] ? levels[i] : Math.max(levels[i], peaks[i] - VIZ_PEAK_FALL);
    }
    const surface = _surface();
    if (!surface) return;
    const { ctx2d, w, h } = surface;
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
      // A 2px marker at the held peak, above the bar it belongs to.  Its top is
      // kept on the canvas: a full-scale peak would otherwise put the marker's
      // own thickness above the top edge.
      const mark = Math.max(2, peaks[i] * h);
      const top = Math.max(0, h - mark - 2);
      ctx2d.fillRect(i * slot, top, slot - gap, 2);
    }
  }

  function _onVisibility() {
    if (!_visible()) _cancelFrame();
    else _scheduleFrame();
  }

  function show() {
    if (shown) return;
    shown = true;
    // The token values may have changed while the strip was hidden, so resolve
    // them fresh for this appearance.
    colours = null;
    if (!el.parentNode) host.insertBefore(el, before);
    if (typeof document !== "undefined" && document.addEventListener) {
      document.addEventListener("visibilitychange", _onVisibility);
    }
    _watchTheme();
    // One resting frame: the strip shows its muted floor line immediately (and
    // a theme applied while it was hidden is painted, not left stale).  The
    // frame loop itself still waits for a source.
    _draw();
    _scheduleFrame();
  }

  function hide() {
    if (!shown) return;
    shown = false;
    _cancelFrame();
    if (typeof document !== "undefined" && document.removeEventListener) {
      document.removeEventListener("visibilitychange", _onVisibility);
    }
    _unwatchTheme();
    _clearPendingAudio();
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
