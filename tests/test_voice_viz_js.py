"""The live spectrum strip's frequency mapping (``shared_static/voice_viz.js``).

The strip is a real frequency response, not an animation: each bar reads the
FFT bins of an ``AnalyserNode`` whose band falls inside the speech range.  The
bin-to-bar mapping is the part that can be wrong silently (a bar that reads
the wrong band, or a dead region where no bar lands), so it is a pure exported
function and these pin it against the edges the brief names, at both common
sample rates.  The drawing and lifecycle are covered by ``test_voice_viz_strip_js``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import demodulize, node_skip, run_node_source

pytestmark = node_skip

_ROOT = Path(__file__).resolve().parents[1] / "turnstone" / "shared_static"
_VIZ_PATH = _ROOT / "voice_viz.js"
# A missing module fails each test on its own assertion, not the collection.
_MODULE = (
    demodulize(_VIZ_PATH) if _VIZ_PATH.exists() else "throw new Error('missing voice_viz.js');"
)
# Mirrors VIZ_FFT_SIZE in voice_viz.js.  test_the_fft_size_matches_the_module
# pins the two together, so a change to one cannot silently leave the other.
_FFT_SIZE = 8192


def _run(body: str) -> Any:
    src = (
        _MODULE
        + f"\n(async () => {{\n{body}\n}})().catch((e) => {{ console.error(e); process.exit(1); }});"
    )
    r = run_node_source(src)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_the_speech_band_and_bar_count_are_named_constants() -> None:
    out = _run(
        """
console.log(JSON.stringify({ bars: VIZ_BAR_COUNT, lo: VIZ_MIN_HZ, hi: VIZ_MAX_HZ }));
"""
    )
    assert out == {"bars": 32, "lo": 80, "hi": 8000}


def test_the_fft_size_matches_the_module() -> None:
    out = _run("console.log(JSON.stringify({ fft: VIZ_FFT_SIZE }));")
    assert out == {"fft": _FFT_SIZE}


def test_every_bar_maps_to_at_least_one_bin_and_none_is_empty() -> None:
    # "No dead space": a bar with no bin would never move.  Checked at both
    # common rates and at the FFT size the strip uses.
    out = _run(
        """
const res = {};
for (const sr of [44100, 48000]) {
  const edges = barBinRanges(VIZ_BAR_COUNT, sr, VIZ_FFT_SIZE);
  res[sr] = { n: edges.length, empty: edges.filter(([a, b]) => b <= a).length };
}
console.log(JSON.stringify(res));
"""
    )
    assert out["44100"] == {"n": 32, "empty": 0}
    assert out["48000"] == {"n": 32, "empty": 0}


def test_the_first_bar_starts_at_80_hz_and_the_last_ends_at_8_khz() -> None:
    out = _run(
        """
const res = {};
for (const sr of [44100, 48000]) {
  const fft = VIZ_FFT_SIZE, binHz = sr / fft;
  const edges = barBinRanges(VIZ_BAR_COUNT, sr, fft);
  const first = edges[0], last = edges[edges.length - 1];
  res[sr] = {
    // the bin whose centre is nearest 80 Hz is the first one the strip reads
    firstLowHz: first[0] * binHz,
    // the highest bin read by the last bar sits at or below 8 kHz
    lastHighHz: (last[1] - 1) * binHz,
    // and the next bin up would be past 8 kHz, so the band is not truncated
    pastTop: last[1] * binHz,
  };
}
console.log(JSON.stringify(res));
"""
    )
    for sr in ("44100", "48000"):
        row = out[sr]
        # The first bar cannot read below the speech floor by more than one bin.
        bin_hz = int(sr) / _FFT_SIZE
        assert 80 - bin_hz <= row["firstLowHz"] <= 80 + bin_hz, (sr, row)
        assert row["lastHighHz"] <= 8000, (sr, row)
        assert row["pastTop"] >= 8000 - bin_hz, (sr, row)


def test_the_bands_rise_on_a_log_scale_so_each_bar_is_a_similar_interval() -> None:
    # Speech pitch is perceived logarithmically: equal ratios per bar, not equal
    # Hz.  The ratio of successive bar centres must stay close to constant.
    out = _run(
        """
const sr = 48000, fft = VIZ_FFT_SIZE, binHz = sr / fft;
const edges = barBinRanges(VIZ_BAR_COUNT, sr, fft);
const centres = edges.map(([a, b]) => ((a + b) / 2) * binHz);
const ratios = [];
for (let i = 1; i < centres.length; i++) ratios.push(centres[i] / centres[i - 1]);
console.log(JSON.stringify({ min: Math.min(...ratios), max: Math.max(...ratios) }));
"""
    )
    # Ideal ratio is (8000/80)^(1/32) ~= 1.1507; allow for bin quantisation.
    assert 1.10 < out["min"] < 1.20, out
    assert 1.10 < out["max"] < 1.20, out


def test_no_two_neighbouring_bars_share_a_bin_so_every_bar_moves_on_its_own() -> None:
    # The FFT size is chosen for this property: a shared bin would make two bars
    # move as one, which reads as dead space to the eye.
    out = _run(
        """
const res = {};
for (const sr of [44100, 48000]) {
  const edges = barBinRanges(VIZ_BAR_COUNT, sr, VIZ_FFT_SIZE);
  let shared = 0;
  for (let i = 0; i + 1 < edges.length; i++) {
    if (edges[i][1] > edges[i + 1][0]) shared++;
  }
  res[sr] = shared;
}
console.log(JSON.stringify(res));
"""
    )
    assert out == {"44100": 0, "48000": 0}


def test_a_bin_belongs_to_at_most_two_neighbouring_bars() -> None:
    # Adjacent bars may share a bin at the low end (where bins are wider than
    # a band), but no bin is read by bars that are not neighbours.
    out = _run(
        """
const res = {};
for (const sr of [44100, 48000]) {
  const edges = barBinRanges(VIZ_BAR_COUNT, sr, VIZ_FFT_SIZE);
  let worst = 0;
  for (let i = 0; i < edges.length; i++) {
    for (let j = i + 2; j < edges.length; j++) {
      if (edges[i][1] > edges[j][0]) worst = Math.max(worst, j - i);
    }
  }
  res[sr] = worst;
}
console.log(JSON.stringify(res));
"""
    )
    assert out == {"44100": 0, "48000": 0}


def test_the_mapping_reads_the_analyser_it_is_given_and_returns_levels() -> None:
    out = _run(
        """
// A spectrum that is silent except one bin: only the bar that owns it lifts.
const bins = new Uint8Array(VIZ_FFT_SIZE / 2);
const sr = 48000, fft = VIZ_FFT_SIZE;
const edges = barBinRanges(VIZ_BAR_COUNT, sr, fft);
const hot = Math.floor((edges[10][0] + edges[10][1]) / 2);
bins[hot] = 255;
const levels = barLevels(bins, edges);
const lifted = levels.map((v, i) => (v > 0 ? i : -1)).filter((i) => i >= 0);
console.log(JSON.stringify({ lifted, top: levels[10], len: levels.length }));
"""
    )
    assert out["lifted"] == [10]
    assert out["top"] == 1
    assert out["len"] == 32
