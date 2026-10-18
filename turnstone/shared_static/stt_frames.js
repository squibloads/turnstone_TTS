// ===========================================================================
//  turnstone frame capture — shared module (shared_static/stt_frames.js)
//
//  Voice mode will decide, per short segment, whether the person speaking is
//  the enrolled user.  Dictation cannot answer that today: stt_mic.js records
//  one compressed webm/opus clip per turn, and a codec stream only means
//  something as a whole.  This module taps the same microphone a different
//  way — an AudioWorklet pulls raw samples, downmixes them to mono and hands
//  the caller fixed 512-sample frames at 16 kHz, so a speaker check can run on
//  any 32 ms slice while the turn is still being spoken.
//
//  The DSP is kept apart from Web Audio: createResampler, floatToInt16 and
//  createFramer are pure and run under node.  The worklet itself only
//  downmixes and buffers, and its source ships as a string turned into a Blob
//  URL, so adding this module needs no extra static route.  The caller owns
//  the MediaStream; stop() releases the graph, never the tracks.
//
//  Not wired into dictation yet: importing this module changes nothing until
//  something calls createFrameCapture().  ES module.
// ===========================================================================

// The frame contract other modules code against: 512 samples at 16 kHz mono,
// 32 ms per frame.
export const SAMPLE_RATE = 16000;
export const FRAME_SIZE = 512;

// The worklet posts in ~21 ms batches at 48 kHz rather than one message per
// 128-sample render quantum: a message per quantum is 375 postMessage calls a
// second for a capture that runs for a whole conversation.  1024 keeps the
// first frame's latency comfortably below a blink while the port stays quiet.
const _WORKLET_CHUNK = 1024;
const _PROCESSOR_NAME = "turnstone-frame-capture";

// ---------------------------------------------------------------------------
//  Pure DSP (node-testable, no Web Audio)
// ---------------------------------------------------------------------------

// Streaming linear resampler.  A one-shot conversion is easy; the state here
// exists because the worklet delivers arbitrary-length chunks and voices must
// not click at every chunk boundary.  `prev` carries the last input sample and
// `pos` the next output position measured against the incoming chunk, so a
// split inside an interpolation interval resumes at exactly the same phase it
// would have had in one piece.
export function createResampler(inRate, outRate) {
  if (!(inRate > 0) || !(outRate > 0)) {
    throw new TypeError("resampler rates must be positive");
  }
  const step = inRate / outRate;
  let prev = 0;
  let pos = 1;
  let started = false;

  return {
    push(chunk) {
      const n = chunk.length;
      if (!n) return new Float32Array(0);
      // One extra leading sample reproduces the previous chunk's tail, which
      // is the left endpoint of an interval that spans the boundary.
      const joined = new Float32Array(n + 1);
      if (!started) {
        // The first output is the chunk's first sample, not an interpolation
        // from silence: duplicating it keeps every chunk on the same formula.
        prev = chunk[0];
        started = true;
      }
      joined[0] = prev;
      joined.set(chunk, 1);

      const out = [];
      while (pos <= n) {
        const i = Math.floor(pos);
        const frac = pos - i;
        const a = joined[i];
        const b = i < n ? joined[i + 1] : a;
        out.push(a + (b - a) * frac);
        pos += step;
      }
      // Shift the phase into the next chunk's coordinates, where index 0 is
      // this chunk's last sample.
      pos -= n;
      prev = chunk[n - 1];
      return Float32Array.from(out);
    },
  };
}

// PCM16 is asymmetric: +1 maps to 0x7fff and -1 to 0x8000.  Clipping here is
// intentional — a hot microphone can overshoot ±1, and wrapping that around
// would turn a loud word into a burst of noise for the speaker check.
export function floatToInt16(f32) {
  const out = new Int16Array(f32.length);
  for (let i = 0; i < f32.length; i++) {
    const sample = f32[i];
    if (!Number.isFinite(sample)) continue;
    const clipped = sample < -1 ? -1 : sample > 1 ? 1 : sample;
    out[i] = Math.round(clipped < 0 ? clipped * 0x8000 : clipped * 0x7fff);
  }
  return out;
}

// Re-chunks a resampled stream into fixed frames.  The remainder is kept, not
// padded or dropped: a frame is only handed on when it is genuinely 512
// samples, which is what makes seq and t0Ms exact.
export function createFramer(frameSize) {
  const size = Math.floor(frameSize);
  if (!(size > 0)) throw new TypeError("frameSize must be positive");
  let pending = new Float32Array(0);

  return {
    push(chunk) {
      if (!chunk || !chunk.length) return [];
      const joined = new Float32Array(pending.length + chunk.length);
      joined.set(pending);
      joined.set(chunk, pending.length);
      const frames = [];
      let offset = 0;
      while (offset + size <= joined.length) {
        frames.push(joined.slice(offset, offset + size));
        offset += size;
      }
      pending = joined.slice(offset);
      return frames;
    },
  };
}

// ---------------------------------------------------------------------------
//  The worklet
// ---------------------------------------------------------------------------

// Downmix and buffering live on the audio thread; resampling and framing stay
// on the page so the shipped DSP above is the one under test.  The processor
// writes no output — it is connected to the destination only so the browser
// keeps pulling its input.
const WORKLET_SOURCE = `
class TurnstoneFrameCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this._chunk = new Float32Array(${_WORKLET_CHUNK});
    this._fill = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input.length || !input[0] || !input[0].length) return true;
    const channels = input.length;
    const length = input[0].length;
    for (let i = 0; i < length; i++) {
      let sum = 0;
      for (let c = 0; c < channels; c++) sum += input[c][i];
      this._chunk[this._fill++] = sum / channels;
      if (this._fill === this._chunk.length) this._flush();
    }
    return true;
  }

  _flush() {
    const samples = this._chunk.slice(0, this._fill);
    this.port.postMessage({ type: "samples", samples }, [samples.buffer]);
    this._fill = 0;
  }
}

registerProcessor(${JSON.stringify(_PROCESSOR_NAME)}, TurnstoneFrameCapture);
`;

// ---------------------------------------------------------------------------
//  Capture lifecycle
// ---------------------------------------------------------------------------

// opts:
//   onFrame    ({ pcm, seq, t0Ms }) => void; pcm is Int16Array(512) at
//              16 kHz mono, seq starts at 0, t0Ms is the frame's start in ms
//              since capture start (sample count, not wall clock).
//   onError    optional (err) => void; a missing AudioWorklet or a failed
//              module load is reported here and start() resolves false.
//
// Returns { start(stream), stop(), running }.  `running` is live.  start()
// resolves true once the graph is pulling; stop() disconnects the graph,
// closes the context and revokes the Blob URL, and never touches the
// caller-owned stream.  Both are safe to call more than once, and start()
// after stop() begins a fresh sequence at t0Ms 0.
export function createFrameCapture(opts) {
  opts = opts || {};
  const onFrame = typeof opts.onFrame === "function" ? opts.onFrame : null;
  const onError = typeof opts.onError === "function" ? opts.onError : null;

  let running = false;
  // A start() still inside addModule() owns no shared slots yet; `starting`
  // refuses a concurrent second start instead of letting two graphs race for
  // the same teardown state.
  let starting = false;
  // Every start/stop pair gets a generation.  An addModule() still in flight
  // when stop() runs must not resurrect the graph or report a torn-down
  // context's failure against the next capture.
  let generation = 0;
  let context = null;
  let source = null;
  let node = null;
  let blobUrl = null;
  let seq = 0;
  let resampler = null;
  let framer = null;

  function report(err) {
    // The error callback is the end of the line: a throwing handler must not
    // become an unhandled rejection inside the port's message dispatch.
    try {
      if (onError) onError(err);
    } catch (e) {
      /* nobody left to tell */
    }
  }

  function releaseParts(ctx, src, workletNode, url) {
    if (workletNode) {
      try {
        workletNode.port.onmessage = null;
      } catch (e) {
        /* the port is already gone */
      }
      try {
        workletNode.disconnect();
      } catch (e) {
        /* already disconnected */
      }
      try {
        workletNode.port.close();
      } catch (e) {
        /* already closed */
      }
    }
    if (src) {
      try {
        src.disconnect();
      } catch (e) {
        /* already disconnected */
      }
    }
    if (ctx) {
      try {
        const closing = ctx.close();
        if (closing && typeof closing.catch === "function") {
          closing.catch(() => {});
        }
      } catch (e) {
        /* already closed */
      }
    }
    if (url) {
      try {
        URL.revokeObjectURL(url);
      } catch (e) {
        /* no Blob URL support left to release */
      }
    }
  }

  function release() {
    const ctx = context;
    const src = source;
    const workletNode = node;
    const url = blobUrl;
    context = source = node = blobUrl = null;
    resampler = null;
    framer = null;
    releaseParts(ctx, src, workletNode, url);
  }

  function handleSamples(data) {
    if (!running || !resampler || !framer) return;
    const samples = data && data.samples;
    if (!samples || !samples.length) return;
    try {
      const at16k = resampler.push(samples);
      const whole = framer.push(at16k);
      for (let i = 0; i < whole.length; i++) {
        // A consumer may stop from inside onFrame; do not keep feeding it
        // frames from the batch that was already in flight.
        if (!running) return;
        const pcm = floatToInt16(whole[i]);
        // Derived from the sample count, never Date.now(): a backgrounded tab
        // or a late worklet message must not shift a frame's timestamp.
        const frame = {
          pcm,
          seq,
          t0Ms: (seq * FRAME_SIZE * 1000) / SAMPLE_RATE,
        };
        seq += 1;
        if (onFrame) onFrame(frame);
      }
    } catch (err) {
      // A throwing consumer or malformed chunk ends the capture instead of
      // leaving a hot graph behind with nobody reading it.
      report(err);
      stop();
    }
  }

  async function start(stream) {
    if (running || starting) return false;
    starting = true;
    const mine = ++generation;
    // Everything is built in locals and committed only once the graph is
    // complete.  A stop() during addModule() therefore tears down the shared
    // slots (which this start has not touched) and this start can release its
    // own context without touching whatever captures exist by the time its
    // await resumes.
    let ctx = null;
    let src = null;
    let workletNode = null;
    let url = null;
    let resample = null;
    let frameChunks = null;
    let committed = false;
    try {
      const Ctx =
        typeof AudioContext !== "undefined"
          ? AudioContext
          : typeof webkitAudioContext !== "undefined"
            ? webkitAudioContext
            : null;
      if (!Ctx) {
        report(new Error("Audio capture is not supported in this browser"));
        return false;
      }
      try {
        ctx = new Ctx();
      } catch (err) {
        report(err);
        return false;
      }
      // There is no degraded mode for a capture module: without the worklet
      // we can neither downmix on the audio thread nor pace frames.  Say so
      // once and leave dictation's recorder path untouched.
      if (
        !ctx.audioWorklet ||
        typeof ctx.audioWorklet.addModule !== "function" ||
        typeof AudioWorkletNode === "undefined"
      ) {
        report(new Error("AudioWorklet is not supported in this browser"));
        return false;
      }
      resample = createResampler(ctx.sampleRate, SAMPLE_RATE);
      frameChunks = createFramer(FRAME_SIZE);
      url = URL.createObjectURL(
        new Blob([WORKLET_SOURCE], { type: "application/javascript" }),
      );
      await ctx.audioWorklet.addModule(url);
      if (mine !== generation) return false;
      src = ctx.createMediaStreamSource(stream);
      workletNode = new AudioWorkletNode(ctx, _PROCESSOR_NAME);
      src.connect(workletNode);
      // A capture node has to be part of the rendering graph or the browser
      // never pulls its input; it emits silence, so this adds no echo.
      workletNode.connect(ctx.destination);
      context = ctx;
      source = src;
      node = workletNode;
      blobUrl = url;
      resampler = resample;
      framer = frameChunks;
      seq = 0;
      workletNode.port.onmessage = (event) => handleSamples(event.data);
      running = true;
      committed = true;
      try {
        await ctx.resume();
      } catch (e) {
        /* a suspended context may be resumed by the page later */
      }
      // A stop() after commit already released the graph; anything later
      // belongs to a newer start and must not be touched here.
      return mine === generation;
    } catch (err) {
      if (mine === generation) report(err);
      return false;
    } finally {
      if (!committed) releaseParts(ctx, src, workletNode, url);
      if (mine === generation) starting = false;
    }
  }

  function stop() {
    generation += 1;
    running = false;
    starting = false;
    release();
  }

  return {
    start,
    stop,
    get running() {
      return running;
    },
  };
}
