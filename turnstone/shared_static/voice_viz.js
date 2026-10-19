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
