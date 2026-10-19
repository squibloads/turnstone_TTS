"""The voice segmenter (``shared_static/stt_segmenter.js``).

The module is the pure half of the speaker gate: it turns the captured
32 ms frames into speaker-sized segments and conversation turns, and asks an
optional ``verdict`` callback about each segment.  Nothing here touches a
microphone or a page, so the frames are synthesized: a 440 Hz tone inside the
speech spans and deterministic low-level noise outside them.

The two clocks are pinned separately from the energy detector.  A segment
ends ``segmentGapMs`` after the last speech energy; a turn ends
``turnSilenceMs`` after the last *kept* segment's speech.  ``verdict`` is
called when a segment closes (possibly concurrently, so verdicts can resolve
out of order) but its result is applied in segment order — a reject closes
the turn at that point and the following segments start the next one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import demodulize, node_skip, run_node_source

pytestmark = node_skip

_MODULE = demodulize(
    Path(__file__).resolve().parents[1] / "turnstone/shared_static/stt_segmenter.js"
)

# One frame is 512 samples at 16 kHz (32 ms).  The tone's RMS is ~0.13 and the
# noise's ~0.0004, well clear of the detector's defaults, so the tests are
# about the clocks and the framing rather than about tuning the detector.
_HELPERS = r"""
const SR = 16000;
const FRAME_MS = 32;
const FRAME_SAMPLES = 512;

function makeFrame(t0Ms, seq, spans) {
  const pcm = new Int16Array(FRAME_SAMPLES);
  for (let i = 0; i < FRAME_SAMPLES; i++) {
    const tMs = t0Ms + (i * 1000) / SR;
    let inSpeech = false;
    for (let s = 0; s < spans.length; s++) {
      if (tMs >= spans[s][0] && tMs < spans[s][1]) { inSpeech = true; break; }
    }
    if (inSpeech) {
      pcm[i] = Math.round(6000 * Math.sin((2 * Math.PI * 440 * tMs) / 1000));
    } else {
      pcm[i] = ((i * 37 + t0Ms) % 41) - 20;
    }
  }
  return { pcm, seq, t0Ms };
}

function makeFrames(spans, untilMs) {
  const frames = [];
  for (let t0 = 0; t0 < untilMs; t0 += FRAME_MS) {
    frames.push(makeFrame(t0, frames.length, spans));
  }
  return frames;
}

const tick = async () => {
  for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r));
};
"""

_PRELUDE = _MODULE + "\n" + _HELPERS


def _run(body: str) -> Any:
    src = (
        _PRELUDE
        + "\n(async () => {\n"
        + body
        + "\n})().catch((e) => { console.error(e); process.exit(1); });"
    )
    r = run_node_source(src)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# One clock at a time
# ---------------------------------------------------------------------------


def test_one_burst_is_one_segment_and_the_turn_ends_a_second_after_the_speech() -> None:
    out = _run(
        """
const frames = makeFrames([[320, 704]], 2500);
const segments = [];
const turns = [];
const rejected = [];
let now = 0;
let turnAt = -1;
const seg = createSegmenter({
  onSegment: (s) => segments.push(s),
  onTurnEnd: (t) => { turns.push(t); turnAt = now; },
  onRejected: (s) => rejected.push(s),
});
for (const f of frames) { now = f.t0Ms; seg.push(f); }
const s = segments[0];
console.log(JSON.stringify({
  count: segments.length,
  turns: turns.length,
  rejected: rejected.length,
  startMs: s && s.startMs,
  endMs: s && s.endMs,
  frameCount: s && s.frames.length,
  firstIsPreroll: !!(s && s.frames[0] === frames[9].pcm),
  allInt16: !!(s && s.frames.every((p) => p instanceof Int16Array)),
  turnSegments: turns[0] ? turns[0].segments.length : -1,
  sameObject: !!(s && turns[0] && turns[0].segments[0] === s),
  turnAt,
}));
"""
    )
    assert out["count"] == 1
    assert out["turns"] == 1 and out["rejected"] == 0
    assert out["startMs"] == 288  # one 32 ms frame of pre-roll before speech at 320
    assert 704 < out["endMs"] <= 704 + 250
    assert 13 <= out["frameCount"] <= 20
    assert out["firstIsPreroll"] is True
    assert out["allInt16"] is True
    assert out["turnSegments"] == 1 and out["sameObject"] is True
    assert out["turnAt"] == 1728  # last speech frame ends at 704; +1000 ms, frame-rounded


def test_two_bursts_over_the_segment_gap_are_two_segments_in_one_turn() -> None:
    """~300 ms of quiet splits the segment but not the turn."""
    out = _run(
        """
const frames = makeFrames([[320, 704], [1024, 1408]], 3000);
const segments = [];
const turns = [];
const turnTimes = [];
let now = 0;
const seg = createSegmenter({
  onSegment: (s) => segments.push(s),
  onTurnEnd: (t) => { turns.push(t); turnTimes.push(now); },
});
for (const f of frames) { now = f.t0Ms; seg.push(f); }
console.log(JSON.stringify({
  count: segments.length,
  turns: turns.length,
  turnSegments: turns[0] ? turns[0].segments.length : -1,
  firstEnd: segments[0] && segments[0].endMs,
  secondStart: segments[1] && segments[1].startMs,
  secondEnd: segments[1] && segments[1].endMs,
  turnTimes,
}));
"""
    )
    assert out["count"] == 2
    assert out["turns"] == 1 and out["turnSegments"] == 2
    assert out["firstEnd"] < 1024  # closed inside the pause, before the next burst
    assert out["secondStart"] == 992  # pre-roll frame before speech at 1024
    assert 1408 < out["secondEnd"] <= 1408 + 250
    assert out["turnTimes"] == [2432]


def test_two_bursts_over_a_second_apart_are_two_turns() -> None:
    out = _run(
        """
const frames = makeFrames([[320, 704], [1920, 2304]], 4000);
const segments = [];
const turns = [];
const turnTimes = [];
let now = 0;
const seg = createSegmenter({
  onSegment: (s) => segments.push(s),
  onTurnEnd: (t) => { turns.push(t); turnTimes.push(now); },
});
for (const f of frames) { now = f.t0Ms; seg.push(f); }
console.log(JSON.stringify({
  count: segments.length,
  turns: turns.length,
  sizes: turns.map((t) => t.segments.length),
  turnTimes,
}));
"""
    )
    assert out["count"] == 2
    assert out["turns"] == 2 and out["sizes"] == [1, 1]
    assert out["turnTimes"] == [1728, 3328]


# ---------------------------------------------------------------------------
# Too-short segments
# ---------------------------------------------------------------------------


def test_a_hundred_millisecond_click_is_dropped() -> None:
    out = _run(
        """
const frames = makeFrames([[320, 420]], 2500);
const segments = [];
const turns = [];
const rejected = [];
const seg = createSegmenter({
  onSegment: (s) => segments.push(s),
  onTurnEnd: (t) => turns.push(t),
  onRejected: (s) => rejected.push(s),
});
for (const f of frames) seg.push(f);
console.log(JSON.stringify({ segments: segments.length, turns: turns.length, rejected: rejected.length }));
"""
    )
    assert out == {"segments": 0, "turns": 0, "rejected": 0}


def test_a_segment_at_exactly_the_minimum_is_kept() -> None:
    """The 100 ms burst's active run is four frames (128 ms)."""
    out = _run(
        """
const frames = makeFrames([[320, 420]], 2500);
const segments = [];
const turns = [];
const seg = createSegmenter({
  minSegmentMs: 128,
  onSegment: (s) => segments.push(s),
  onTurnEnd: (t) => turns.push(t),
});
for (const f of frames) seg.push(f);
console.log(JSON.stringify({
  segments: segments.length,
  turns: turns.length,
  startMs: segments[0] && segments[0].startMs,
}));
"""
    )
    assert out["segments"] == 1 and out["turns"] == 1
    assert out["startMs"] == 288


def test_a_dropped_click_does_not_restart_the_turn_clock() -> None:
    """A blip pauses the decision while it is open, then falls back.

    The click starts 896 ms after the real segment, so the turn's 1000 ms of
    quiet has not elapsed.  It must not restart the clock at the click's end
    either: once the click resolves as too short, the turn falls back to the
    last kept segment and ends immediately.  The cost of waiting for the
    maybe-word is one segment gap, not another full second.
    """
    out = _run(
        """
const frames = makeFrames([[320, 704], [1600, 1700]], 3000);
const segments = [];
const turns = [];
const turnTimes = [];
let now = 0;
const seg = createSegmenter({
  onSegment: (s) => segments.push(s),
  onTurnEnd: (t) => { turns.push(t); turnTimes.push(now); },
});
for (const f of frames) { now = f.t0Ms; seg.push(f); }
console.log(JSON.stringify({
  segments: segments.length,
  turns: turns.length,
  turnTimes,
  turnSegments: turns[0] ? turns[0].segments.length : -1,
}));
"""
    )
    assert out["segments"] == 1 and out["turns"] == 1
    assert out["turnSegments"] == 1
    # The click opens at 1600 and resolves at 1984; the turn then ends at once.
    # A reset would have pushed this past 2700.
    assert out["turnTimes"] == [1984]


# ---------------------------------------------------------------------------
# The verdict hook
# ---------------------------------------------------------------------------


def test_a_rejected_first_segment_reports_only_the_rejection() -> None:
    """There is no turn to end until a segment is kept."""
    out = _run(
        """
const frames = makeFrames([[320, 704]], 2500);
const segments = [];
const turns = [];
const rejected = [];
const seg = createSegmenter({
  verdict: () => "reject",
  onSegment: (s) => segments.push(s),
  onTurnEnd: (t) => turns.push(t),
  onRejected: (s) => rejected.push(s),
});
for (const f of frames) seg.push(f);
console.log(JSON.stringify({
  segments: segments.length,
  turns: turns.length,
  rejected: rejected.length,
}));
"""
    )
    assert out == {"segments": 0, "turns": 0, "rejected": 1}


def test_a_reject_ends_the_turn_after_the_segments_before_it() -> None:
    out = _run(
        """
const frames = makeFrames([[320, 704], [1024, 1408]], 3000);
const segments = [];
const turns = [];
const rejected = [];
const turnTimes = [];
const events = [];
let calls = 0;
let now = 0;
const seg = createSegmenter({
  verdict: () => (++calls === 1 ? "accept" : "reject"),
  onSegment: (s) => { segments.push(s); events.push("segment"); },
  onTurnEnd: (t) => { turns.push(t); turnTimes.push(now); events.push("turn"); },
  onRejected: (s) => { rejected.push(s); events.push("rejected"); },
});
for (const f of frames) { now = f.t0Ms; seg.push(f); }
console.log(JSON.stringify({
  segments: segments.length,
  turns: turns.length,
  rejected: rejected.length,
  rejectedStart: rejected[0] && rejected[0].startMs,
  turnSegments: turns[0] ? turns[0].segments.length : -1,
  turnAt: turnTimes[0],
  turnHasFirst: !!(turns[0] && turns[0].segments[0] === segments[0]),
  events,
}));
"""
    )
    assert out["segments"] == 1
    assert out["turns"] == 1 and out["turnSegments"] == 1
    assert out["rejected"] == 1 and out["rejectedStart"] == 992
    assert out["turnAt"] == 1664  # the second segment closes
    assert out["turnHasFirst"] is True
    assert out["events"] == ["segment", "rejected", "turn"]


def test_an_abstain_verdict_keeps_the_segment_in_the_turn() -> None:
    out = _run(
        """
const frames = makeFrames([[320, 704], [1024, 1408]], 3000);
const segments = [];
const turns = [];
const rejected = [];
const seg = createSegmenter({
  verdict: () => "abstain",
  onSegment: (s) => segments.push(s),
  onTurnEnd: (t) => turns.push(t),
  onRejected: (s) => rejected.push(s),
});
for (const f of frames) seg.push(f);
console.log(JSON.stringify({
  segments: segments.length,
  rejected: rejected.length,
  turns: turns.length,
  turnSegments: turns[0] ? turns[0].segments.length : -1,
}));
"""
    )
    assert out == {"segments": 2, "rejected": 0, "turns": 1, "turnSegments": 2}


def test_async_verdicts_that_resolve_out_of_order_still_emit_in_order() -> None:
    out = _run(
        """
const frames = makeFrames([[320, 704], [1024, 1408]], 3000);
const segments = [];
const turns = [];
const pending = [];
const seg = createSegmenter({
  verdict: () => new Promise((resolve) => pending.push(resolve)),
  onSegment: (s) => segments.push(s),
  onTurnEnd: (t) => turns.push(t),
});
for (const f of frames) seg.push(f);
const afterPush = { pending: pending.length, segments: segments.length, turns: turns.length };
pending[1]("accept");
await tick();
const afterLaterFirst = { segments: segments.length, turns: turns.length };
pending[0]("accept");
await tick();
console.log(JSON.stringify({
  afterPush,
  afterLaterFirst,
  count: segments.length,
  order: segments.map((s) => s.startMs),
  turns: turns.length,
  turnOrder: turns[0] ? turns[0].segments.map((s) => s.startMs) : [],
}));
"""
    )
    assert out["afterPush"] == {"pending": 2, "segments": 0, "turns": 0}
    # The second verdict lands first, but the first segment still owns the
    # head of the queue: nothing may be emitted past it.
    assert out["afterLaterFirst"] == {"segments": 0, "turns": 0}
    assert out["count"] == 2
    assert out["order"] == [288, 992]
    assert out["turns"] == 1
    assert out["turnOrder"] == [288, 992]


def test_a_failed_or_unknown_verdict_keeps_the_segment_as_abstain() -> None:
    out = _run(
        """
const frames = makeFrames([[320, 704], [1024, 1408]], 3000);
const segments = [];
const turns = [];
const rejected = [];
let calls = 0;
const seg = createSegmenter({
  verdict: () => {
    calls++;
    if (calls === 1) throw new Error("verifier down");
    return "maybe";
  },
  onSegment: (s) => segments.push(s),
  onTurnEnd: (t) => turns.push(t),
  onRejected: (s) => rejected.push(s),
});
for (const f of frames) seg.push(f);
console.log(JSON.stringify({
  segments: segments.length,
  rejected: rejected.length,
  turns: turns.length,
  turnSegments: turns[0] ? turns[0].segments.length : -1,
}));
"""
    )
    assert out == {"segments": 2, "rejected": 0, "turns": 1, "turnSegments": 2}


# ---------------------------------------------------------------------------
# Flushing
# ---------------------------------------------------------------------------


def test_flush_mid_speech_emits_the_open_segment_and_ends_the_turn() -> None:
    out = _run(
        """
const frames = makeFrames([[320, 704]], 704);
const segments = [];
const turns = [];
let now = 0;
let turnAt = -1;
const seg = createSegmenter({
  onSegment: (s) => segments.push(s),
  onTurnEnd: (t) => { turns.push(t); turnAt = now; },
});
for (const f of frames) { now = f.t0Ms; seg.push(f); }
const before = { segments: segments.length, turns: turns.length };
now = 704;
seg.flush();
const after = {
  segments: segments.length,
  turns: turns.length,
  startMs: segments[0] && segments[0].startMs,
  endMs: segments[0] && segments[0].endMs,
  frameCount: segments[0] && segments[0].frames.length,
  turnAt,
};
seg.flush();
const twice = { segments: segments.length, turns: turns.length };
console.log(JSON.stringify({ before, after, twice }));
"""
    )
    assert out["before"] == {"segments": 0, "turns": 0}
    assert out["after"]["segments"] == 1 and out["after"]["turns"] == 1
    assert out["after"]["startMs"] == 288  # pre-roll
    assert out["after"]["endMs"] == 704  # the last frame's end; no trailing silence
    assert out["after"]["frameCount"] == 13  # pre-roll + twelve speech frames
    assert out["after"]["turnAt"] == 704
    assert out["twice"] == {"segments": 1, "turns": 1}


def test_a_steady_loud_stream_is_cut_into_segments_of_bounded_length() -> None:
    """A fan or a stuck mic never produces the silence either clock needs.

    Without a span bound the open segment would accumulate frames forever and
    never reach a verdict or a turn decision.  ``maxSegmentMs`` closes it at
    the frame where the next one would exceed the bound; that frame starts the
    next segment and the turn keeps going, exactly as if a gap had closed it.
    """
    out = _run(
        """
const frames = makeFrames([[0, 61000]], 63000);   // 61 s loud, then 2 s quiet
const segments = [];
const turns = [];
const seg = createSegmenter({
  maxSegmentMs: 15000,
  onSegment: (s) => segments.push(s),
  onTurnEnd: (t) => turns.push(t),
});
for (const f of frames) seg.push(f);
const lengths = segments.map((s) => s.endMs - s.startMs);
console.log(JSON.stringify({
  count: segments.length,
  lengths,
  maxLength: Math.max(...lengths),
  firstStart: segments[0] && segments[0].startMs,
  contiguous: segments.every((s, i) => i === 0 || s.startMs === segments[i - 1].endMs),
  framesBounded: segments.every((s) => s.frames.length * 32 <= 15000),
  turns: turns.length,
  turnSegments: turns[0] ? turns[0].segments.length : -1,
}));
"""
    )
    assert out["count"] == 5  # four full 15 s spans plus the clipped tail
    assert out["maxLength"] <= 15000
    assert out["framesBounded"] is True
    assert out["firstStart"] == 0
    assert out["contiguous"] is True
    # The cut is a segment boundary, not a turn boundary: one turn still
    # collects every segment and ends only on the silence after the stream.
    assert out["turns"] == 1
    assert out["turnSegments"] == 5


def test_the_segment_span_bound_defaults_to_fifteen_seconds() -> None:
    out = _run(
        """
const frames = makeFrames([[0, 20000]], 21000);
const segments = [];
const seg = createSegmenter({ onSegment: (s) => segments.push(s) });
for (const f of frames) seg.push(f);
const lengths = segments.map((s) => s.endMs - s.startMs);
console.log(JSON.stringify({ count: segments.length, maxLength: Math.max(...lengths) }));
"""
    )
    assert out["count"] == 2  # 14976 ms + the remainder
    assert out["maxLength"] <= 15000


def test_frames_must_arrive_in_clock_order_and_flush_restarts_the_baseline() -> None:
    out = _run(
        """
const frames = makeFrames([[320, 704]], 704);
const seg = createSegmenter({});
for (const f of frames) seg.push(f);
let backwards = "";
try { seg.push(frames[3]); } catch (e) { backwards = e.name + ": " + e.message; }
seg.flush();
let restarted = "ok";
try { seg.push(makeFrame(0, 0, [])); } catch (e) { restarted = e.name; }
console.log(JSON.stringify({ backwards, restarted }));
"""
    )
    assert out["backwards"].startswith("RangeError:")
    assert out["restarted"] == "ok"
