// ===========================================================================
//  turnstone voice segmenter — shared module (shared_static/stt_segmenter.js)
//
//  Cuts the voice-mode frame stream into speech SEGMENTS and conversation
//  TURNS.  This is the pure half of the speaker gate: no microphone, no page,
//  no timer — frames in, callbacks out — so the detector and both clocks
//  can be exercised from synthesized audio.  It is not wired into a page yet.
//
//  Two clocks and a span bound, deliberately:
//
//    - opts.segmentGapMs (250 ms) closes a segment.  A segment is one
//      speaker-sized stretch, short enough that when somebody else starts
//      talking the gate gets a fresh verdict quickly instead of having the
//      new voice buried inside a long accepted segment.
//    - opts.turnSilenceMs (1000 ms) closes a turn.  A turn is what the
//      coordinator receives, so it has to outlast a thinking pause: a speaker
//      who stops to find a word must not submit half a thought.  Using the
//      short gap here would submit every sentence as its own turn; using the
//      long silence for the segment clock would leave a segment open long
//      after the speaker changed.
//    - opts.maxSegmentMs (15000 ms) bounds a segment's span.  Both clocks
//      above are driven by silence, so a steady loud stream (a fan, a stuck
//      mic) would keep one segment open forever: frames accumulate without
//      bound and no verdict or turn decision is ever reached.  The open
//      segment closes at the bound and the next frame opens a new one; the
//      turn keeps running, exactly as if a gap had closed the segment.
//
//  Backend VAD re-segmentation has to be off when the gate is wired.  These
//  boundaries are the units a verdict is attached to; if the speech-to-text
//  service re-cuts the same audio with its own VAD, a segment we accepted can
//  be merged into a neighbour, or a turn split across the backend's segments.
//  The verdict would then describe different audio from the audio that was
//  transcribed.
//
//  A dropped segment (a click, a door, a word shorter than minSegmentMs) is
//  not speech for the turn clock: its audio is thrown away, so counting it as
//  a speech run would hold a finished turn open for another second per click.
//  It does pause the decision while it is open — the turn must not end in the
//  middle of a maybe-word — and when the blip resolves as too short the clock
//  falls back to the last kept segment.
//
//  ES module, no imports.
// ===========================================================================

const _DEFAULTS = {
  segmentGapMs: 250,
  turnSilenceMs: 1000,
  minSegmentMs: 200,
  maxSegmentMs: 15000,
};

// Voice-mode frames are 512 samples of 16 kHz mono (32 ms).  The sample rate
// only turns a frame's sample count into a duration for endMs; both clocks
// run on t0Ms, so a dropped or duplicated frame cannot stretch them.
const _SAMPLE_RATE = 16000;

// Energy detector.  RMS is normalized to Int16 full scale.  The floor is the
// room, not the speaker: it seeds from the first frame (capped, so a stream
// that opens mid-word still hears the word) and then follows quiet frames
// down quickly and up slowly.  Starting speech must clear startRatio; staying
// speech only has to clear holdRatio, and hangoverMs keeps one low frame from
// cutting a word in half.  These become settings with the gate story.
const _MIN_RMS = 0.002;
const _MAX_SEED_RMS = 0.01;
const _START_RATIO = 2.5;
const _HOLD_RATIO = 1.5;
const _HANGOVER_MS = 160;
const _FLOOR_RISE = 0.02;
const _FLOOR_FALL = 0.25;

function _finite(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function _frameMs(pcm) {
  return (pcm.length / _SAMPLE_RATE) * 1000;
}

function _rms(pcm) {
  if (pcm.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < pcm.length; i++) {
    const sample = pcm[i] / 32768;
    sum += sample * sample;
  }
  return Math.sqrt(sum / pcm.length);
}

function _normalizeVerdict(value) {
  // A verifier that failed or answered something we do not understand has
  // made no claim about the speaker.  Keep the audio (abstain) rather than
  // inventing an accept or silently dropping the user's speech.
  if (value === "accept" || value === "reject") return value;
  return "abstain";
}

/**
 * Build a frame-stream segmenter.
 *
 * Frames are `{ pcm: Int16Array, seq, t0Ms }`, pushed in t0Ms order (`seq` is
 * not consulted; t0Ms is the clock).  The segmenter must be fed every
 * captured frame: a missing run is simply a longer non-speech gap and can
 * close a segment or a turn.
 *
 * A segment is `{ startMs, endMs, frames }`.  `frames` are the Int16Array
 * frames (not copies), including one pre-roll frame and at most one frame of
 * trailing padding; `startMs`/`endMs` are the bounds of that padded span.
 * `endMs` is exclusive.
 *
 * `verdict(segment)` is called as each segment closes, before the segment is
 * handed to onSegment; it may return "accept", "reject", "abstain", or a
 * Promise of one.  Verdicts may settle out of order — results are applied in
 * segment order.  A reject reports the segment through onRejected, ends the
 * turn with the segments collected before it, and the next segment starts a
 * new turn.  A turn with no kept segments is not reported.  A thrown or
 * unknown verdict is treated as "abstain".  Without a verdict every segment
 * is accepted.
 *
 * flush() closes the open segment and ends the turn.  With an asynchronous
 * verdict the callbacks arrive when that verdict settles, not during flush.
 *
 * @param {object} [opts]
 * @param {number} [opts.segmentGapMs=250] non-speech run that closes a segment
 * @param {number} [opts.turnSilenceMs=1000] non-speech run that closes a turn
 * @param {number} [opts.minSegmentMs=200] shortest active speech run kept
 * @param {number} [opts.maxSegmentMs=15000] longest span an open segment may
 *        reach before it is closed
 * @param {(segment: object) => (string | Promise<string>)} [opts.verdict]
 * @param {(segment: object) => void} [opts.onSegment] accepted / abstained
 * @param {(turn: { segments: object[] }) => void} [opts.onTurnEnd] non-empty
 *        turn
 * @param {(segment: object) => void} [opts.onRejected] rejected by verdict
 * @returns {{ push: (frame: object) => void, flush: () => void }}
 */
export function createSegmenter(opts = {}) {
  opts = opts || {};
  const segmentGapMs = Math.max(
    0,
    _finite(opts.segmentGapMs, _DEFAULTS.segmentGapMs),
  );
  // A turn cannot end before the segment inside it has had time to close;
  // clamping keeps a bad pair of settings from dropping the open segment.
  const turnSilenceMs = Math.max(
    segmentGapMs,
    _finite(opts.turnSilenceMs, _DEFAULTS.turnSilenceMs),
  );
  const minSegmentMs = Math.max(
    0,
    _finite(opts.minSegmentMs, _DEFAULTS.minSegmentMs),
  );
  const maxSegmentMs = Math.max(
    0,
    _finite(opts.maxSegmentMs, _DEFAULTS.maxSegmentMs),
  );
  const verdict = typeof opts.verdict === "function" ? opts.verdict : null;
  const onSegment =
    typeof opts.onSegment === "function" ? opts.onSegment : null;
  const onTurnEnd =
    typeof opts.onTurnEnd === "function" ? opts.onTurnEnd : null;
  const onRejected =
    typeof opts.onRejected === "function" ? opts.onRejected : null;

  // --- detector state ----------------------------------------------------
  let seeded = false;
  let noiseFloor = _MIN_RMS;
  let speaking = false;
  let lastActiveMs = -Infinity;

  // --- stream / framing state -------------------------------------------
  let lastT0Ms = null;
  let lastFrame = null;
  // Whether the immediately preceding frame was speech in the detector's
  // state.  Only a non-speech frame can be pre-roll for a new segment.
  let lastFrameSpeech = false;

  // The open segment, or null.  `entries` keep the frame and its speech flag
  // together so the trailing padding can be trimmed without re-running the
  // detector.
  let segment = null;

  // End of the last KEPT segment's active speech.  Null when no turn is
  // open.  Dropped segments never move it.
  let turnSpeechEndMs = null;

  // --- emission queue ----------------------------------------------------
  // Segment verdicts can settle in any order, but callbacks must not.  Each
  // closed segment becomes a job whose verdict runs immediately; the drain
  // applies jobs from the head and stops at the first one that has not
  // settled.  Turn-end jobs sit behind the segments and therefore wait for
  // them, which is what lets a reject remove a segment before the turn is
  // handed over.
  let emitTurn = null;
  const queue = [];
  let draining = false;

  function _apply(job) {
    if (job.kind === "turn") {
      _finishTurn();
      return;
    }
    if (job.verdict === "reject") {
      if (onRejected) onRejected(job.segment);
      _finishTurn();
      return;
    }
    if (!emitTurn) emitTurn = { segments: [] };
    emitTurn.segments.push(job.segment);
    if (onSegment) onSegment(job.segment);
  }

  function _drain() {
    if (draining) return;
    draining = true;
    try {
      while (queue.length && queue[0].settled) _apply(queue.shift());
    } finally {
      draining = false;
    }
  }

  function _finishTurn() {
    if (!emitTurn) return;
    const segments = emitTurn.segments;
    emitTurn = null;
    if (segments.length && onTurnEnd) onTurnEnd({ segments });
  }

  function _enqueueSegment(out) {
    const job = {
      kind: "segment",
      segment: out,
      settled: !verdict,
      verdict: "accept",
    };
    // Queue first: a verdict that re-enters push() must not get its new
    // segment ahead of the one being judged.
    queue.push(job);
    if (verdict) {
      try {
        const value = verdict(out);
        if (value && typeof value.then === "function") {
          Promise.resolve(value).then(
            (resolved) => {
              job.verdict = _normalizeVerdict(resolved);
              job.settled = true;
              _drain();
            },
            () => {
              job.verdict = "abstain";
              job.settled = true;
              _drain();
            },
          );
        } else {
          job.verdict = _normalizeVerdict(value);
          job.settled = true;
        }
      } catch {
        job.verdict = "abstain";
        job.settled = true;
      }
    }
    _drain();
  }

  function _enqueueTurnEnd() {
    queue.push({ kind: "turn", settled: true });
    _drain();
  }

  function _closeSegment() {
    if (!segment) return;
    const closed = segment;
    segment = null;

    // Active speech is the evidence for minSegmentMs; hangover frames are
    // detector state, not speech, and must not turn a click into a segment.
    const speechMs = closed.lastActiveEndMs - closed.firstActiveMs;
    if (speechMs < minSegmentMs) return;

    // Keep one frame after the last speech-state frame: enough padding for a
    // verifier, not the whole gap the clock just measured.
    let end = closed.entries.length;
    while (end > 0 && !closed.entries[end - 1].speech) end--;
    end = Math.min(closed.entries.length, end + 1);
    const entries = closed.entries.slice(0, end);
    const last = entries[entries.length - 1].frame;
    const out = {
      startMs: entries[0].frame.t0Ms,
      endMs: last.t0Ms + _frameMs(last.pcm),
      frames: entries.map((entry) => entry.frame.pcm),
    };

    turnSpeechEndMs = closed.lastActiveEndMs;
    _enqueueSegment(out);
  }

  function _closeTurn() {
    // A flush can land while the last segment is still open; resolve it
    // first so it is either kept (and included) or dropped (and excluded).
    _closeSegment();
    if (turnSpeechEndMs == null) return;
    turnSpeechEndMs = null;
    _enqueueTurnEnd();
  }

  function push(frame) {
    if (!frame || !frame.pcm || typeof frame.pcm.length !== "number") {
      throw new TypeError("stt_segmenter: frame.pcm must be an Int16Array");
    }
    const t0 = Number(frame.t0Ms);
    if (!Number.isFinite(t0)) {
      throw new TypeError("stt_segmenter: frame.t0Ms must be a number");
    }
    if (lastT0Ms !== null && t0 < lastT0Ms) {
      throw new RangeError("stt_segmenter: frame.t0Ms went backwards");
    }
    lastT0Ms = t0;

    const pcm = frame.pcm;
    const duration = _frameMs(pcm);
    const rms = _rms(pcm);

    if (!seeded) {
      noiseFloor = Math.min(_MAX_SEED_RMS, Math.max(_MIN_RMS, rms));
      seeded = true;
    }
    const floor = noiseFloor;
    const active = rms >= floor * _HOLD_RATIO;
    if (speaking) {
      if (active) lastActiveMs = t0;
      else if (t0 - lastActiveMs >= _HANGOVER_MS) speaking = false;
    } else if (rms >= floor * _START_RATIO) {
      speaking = true;
      lastActiveMs = t0;
    }
    const speech = speaking;

    // Follow the room only outside speech, so the speaker's own level never
    // lifts the floor under them.
    if (!speech) {
      const rate = rms < noiseFloor ? _FLOOR_FALL : _FLOOR_RISE;
      noiseFloor = Math.max(
        _MIN_RMS,
        noiseFloor + (rms - noiseFloor) * rate,
      );
    }

    // The gap is measured from the last frame that carried speech energy, not
    // from the detector's release: a 300 ms pause is 300 ms even while
    // hangover is still calling the first frames speech.
    if (segment && t0 - segment.lastActiveEndMs >= segmentGapMs) {
      _closeSegment();
    }

    // Neither clock above can fire while the speaker never pauses.  Cut the
    // open segment once the next frame would take it past its maximum span;
    // that frame then opens the next segment, the same way a gap would have.
    // _closeSegment refreshed turnSpeechEndMs, so the turn itself keeps going.
    if (segment && t0 + duration - segment.entries[0].frame.t0Ms > maxSegmentMs) {
      _closeSegment();
    }

    // While a segment is open the turn decision waits: this frame may be the
    // start of a real continuation, and a sub-minimum blip has to resolve
    // before the clock can fall back to the last kept segment.
    if (
      !segment &&
      turnSpeechEndMs != null &&
      t0 - turnSpeechEndMs >= turnSilenceMs
    ) {
      _closeTurn();
    }

    if (!segment && speech && active) {
      const entries = [];
      if (lastFrame && !lastFrameSpeech) {
        entries.push({ frame: lastFrame, speech: false });
      }
      entries.push({ frame, speech: true });
      segment = {
        entries,
        firstActiveMs: t0,
        lastActiveEndMs: t0 + duration,
      };
    } else if (segment) {
      segment.entries.push({ frame, speech });
      if (active) segment.lastActiveEndMs = t0 + duration;
    }

    lastFrame = frame;
    lastFrameSpeech = speech;
  }

  function flush() {
    _closeSegment();
    _closeTurn();

    // End the utterance, not the estimate: the room's floor is still valid
    // for the next capture.  The timestamp baseline is cleared so a capture
    // that restarts its clock can reuse this segmenter.
    lastFrame = null;
    lastFrameSpeech = false;
    speaking = false;
    lastActiveMs = -Infinity;
    lastT0Ms = null;
  }

  return { push, flush };
}
