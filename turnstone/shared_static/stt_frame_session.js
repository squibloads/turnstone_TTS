// ===========================================================================
//  turnstone voice frame session — shared module (stt_frame_session.js)
//
//  The seam between dictation and the speaker-gate groundwork.  One session
//  owns one microphone capture: the AudioWorklet frames from stt_frames.js are
//  fed to stt_segmenter.js (short gaps cut segments) and to a chunked upload
//  that buffers them on the server.  Dictation itself is untouched — the
//  compressed clip still goes to the transcriber exactly as before; these
//  frames exist so a later story can attach a speaker verdict to each segment.
//
//  The segmenter is the shipped one, on the same frame clock.  The vendored
//  Silero detector stays with auto-stop: it starts after an asynchronous model
//  load, so its probabilities cannot be aligned sample-for-sample with a
//  worklet that is already running, and stacking a second microphone graph
//  would double the work for no better boundary.
//
//  Transport is chunked POST, one request per ~second, rather than a
//  WebSocket: it rides the same authFetch + `/node/{id}` proxy path as the clip
//  upload, survives an auth refresh between batches, and gives the sender an
//  ordinary response.  A duplex socket would buy nothing at one message per
//  second and would need a second auth/proxy story.
//
//  Every failure is best-effort.  A browser without AudioWorklet, a refused
//  batch, or a thrown segment callback is reported once and ends the session;
//  the recorder keeps running and the user never loses a dictation.  The
//  module is inert until createFrameSession().start() is called.
//
//  ES module.
// ===========================================================================

import { authFetch } from "./auth.js";

// Voice frames are 512 samples at 16 kHz (32 ms).  A one-second batch keeps
// one request per second at roughly 32 KB while the sequence stays coarse
// enough that a retry can be identified and dropped.
const _FRAME_MS = 32;
const _BATCH_FRAMES = 31;
const _DEFAULT_MAX_SECONDS = 300;

function _frameFinite(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

// PCM16 is little-endian on the wire regardless of the host's typed-array
// representation: the receiver decodes with an explicit `<h`, so a big-endian
// engine cannot turn every sample into noise.
function _frameBytes(frames) {
  let samples = 0;
  for (let i = 0; i < frames.length; i++) samples += frames[i].pcm.length;
  const body = new Uint8Array(samples * 2);
  const view = new DataView(body.buffer);
  let offset = 0;
  for (let i = 0; i < frames.length; i++) {
    const pcm = frames[i].pcm;
    for (let k = 0; k < pcm.length; k++) {
      view.setInt16(offset, pcm[k], true);
      offset += 2;
    }
  }
  return body;
}

// A capture id has to be unique on the server and survive a reload well enough
// that two panes never share a buffer.  randomUUID is not available on every
// non-secure origin, so the fallback stays inside the receiver's id grammar.
function _captureId() {
  try {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
  } catch (e) {
    /* fall through to the timestamp form */
  }
  return "cap-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
}

function _noopError() {}

// opts:
//   base       transport prefix ("" for the page's own origin, "/node/{id}"
//              for a proxied interactive pane)
//   wsId       workstream the capture belongs to
//   captureId  optional; generated per session when omitted
//   batchFrames  optional; defaults to one second
//   fetchImpl  optional fetch for tests / a pane with its own transport
//   onError    optional (err) => void; called once when a batch is refused
//
// Returns { push(frame), flush(), close(), failed }.  push() buffers a
// { pcm, seq, t0Ms } frame and posts when a batch is full; close() flushes the
// remainder and refuses later pushes.  One failed batch ends the sender: a hot
// microphone must not keep posting into a receiver that already said no.
export function createFrameSender(opts) {
  opts = opts || {};
  const base = opts.base || "";
  const wsId = opts.wsId || "";
  const captureId = String(opts.captureId || _captureId());
  const fetchImpl =
    typeof opts.fetchImpl === "function" ? opts.fetchImpl : authFetch;
  const batchFrames = Math.max(
    1,
    Math.floor(_frameFinite(opts.batchFrames, _BATCH_FRAMES)),
  );
  const onError = typeof opts.onError === "function" ? opts.onError : _noopError;
  // Identity rides the query string rather than custom headers: the console's
  // `/node/{id}` proxy forwards a request's query unchanged but only rebuilds
  // a small header set, so a node-hosted pane would otherwise lose the capture
  // id on the way through.  The body stays raw PCM.
  const path =
    base +
    "/v1/api/workstreams/" +
    encodeURIComponent(wsId) +
    "/speech-to-text/frames";

  let pending = [];
  let chain = Promise.resolve();
  let failed = false;
  let closed = false;

  function report(err) {
    try {
      onError(err);
    } catch (e) {
      /* nobody left to tell */
    }
  }

  function flush() {
    if (failed || closed || !pending.length) return chain;
    const frames = pending;
    pending = [];
    const body = _frameBytes(frames);
    const url =
      path +
      "?capture_id=" +
      encodeURIComponent(captureId) +
      "&seq=" +
      encodeURIComponent(String(frames[0].seq)) +
      "&t0_ms=" +
      encodeURIComponent(String(frames[0].t0Ms));
    chain = chain
      .then(() =>
        fetchImpl(url, {
          method: "POST",
          headers: { "Content-Type": "application/octet-stream" },
          body,
        }),
      )
      .then(async (response) => {
        if (!response || response.ok) return;
        let message = "Frame upload failed";
        try {
          const parsed = await response.json();
          if (parsed && parsed.error) message = String(parsed.error);
        } catch (e) {
          /* a non-JSON refusal keeps the generic message */
        }
        throw new Error(message);
      })
      .catch((err) => {
        if (failed) return;
        failed = true;
        report(err);
      });
    return chain;
  }

  function push(frame) {
    if (failed || closed || !frame || !frame.pcm) return;
    pending.push(frame);
    if (pending.length >= batchFrames) flush();
  }

  function close() {
    if (!closed) {
      const flushing = flush();
      closed = true;
      // Flush must run before the closed latch: it is the last batch.
      return flushing;
    }
    return chain;
  }

  return {
    push,
    flush,
    close,
    get failed() {
      return failed;
    },
  };
}

// Default module loader.  Dynamic so a deployment with frame capture off never
// evaluates the capture or segmenter module, and so a harness can substitute
// the two factories without a browser.
function _loadFrameModules() {
  return Promise.all([import("./stt_frames.js"), import("./stt_segmenter.js")]).then(
    (modules) => ({
      createFrameCapture: modules[0].createFrameCapture,
      createSegmenter: modules[1].createSegmenter,
    }),
  );
}

// opts:
//   base, wsId, captureId  transport identity (see createFrameSender)
//   segmentGapMs           short non-speech run that closes a segment
//   maxSeconds             hard capture duration; the server enforces the same
//   onSegment              optional (segment) => void, called as each closes
//   onError                optional (err) => void; never affects dictation
//   loadModules            optional () => Promise<{createFrameCapture,
//                          createSegmenter}>; tests inject the shipped ones
//   fetchImpl              optional fetch for the upload
//
// Returns { start(stream), stop(), running, captureId }.  start() resolves
// true once the worklet graph is pulling; stop() releases the graph and
// flushes the open segment + last batch.  Once stopped the session is done;
// the mic controller creates a fresh one for the next recording.
export function createFrameSession(opts) {
  opts = opts || {};
  const segmentGapMs = _frameFinite(opts.segmentGapMs, 250);
  const maxSeconds = Math.max(
    0,
    _frameFinite(opts.maxSeconds, _DEFAULT_MAX_SECONDS),
  );
  const maxFrames = Math.max(1, Math.floor((maxSeconds * 1000) / _FRAME_MS));
  const loadModules =
    typeof opts.loadModules === "function" ? opts.loadModules : _loadFrameModules;
  const onSegment =
    typeof opts.onSegment === "function" ? opts.onSegment : null;
  const onError = typeof opts.onError === "function" ? opts.onError : _noopError;
  const captureId = String(opts.captureId || _captureId());

  let capture = null;
  let segmenter = null;
  let sender = null;
  let starting = false;
  let stopped = false;
  let running = false;
  let frames = 0;

  function report(err) {
    try {
      onError(err);
    } catch (e) {
      /* nobody left to tell */
    }
  }

  function stop() {
    stopped = true;
    starting = false;
    running = false;
    if (capture) {
      const live = capture;
      capture = null;
      try {
        live.stop();
      } catch (e) {
        /* the graph was already released */
      }
    }
    if (segmenter) {
      const open = segmenter;
      segmenter = null;
      try {
        open.flush();
      } catch (e) {
        report(e);
      }
    }
    if (sender) {
      const upload = sender;
      sender = null;
      return upload.close();
    }
    return Promise.resolve();
  }

  function emitSegment(segment) {
    // The gate is the next story; until then a segment is only useful in the
    // console/debug log and to an in-process hook.
    try {
      if (typeof console !== "undefined" && typeof console.debug === "function") {
        console.debug(
          "voice segment " +
            segment.startMs +
            "-" +
            segment.endMs +
            "ms (" +
            segment.frames.length +
            " frames)",
        );
      }
    } catch (e) {
      /* a console shim must not end the capture */
    }
    if (!onSegment) return;
    try {
      onSegment(segment);
    } catch (err) {
      report(err);
    }
  }

  function handleFrame(frame) {
    // Deliberately not gated on `running`: stt_frames commits its graph and
    // starts posting before start() resolves, and dropping that first batch
    // would make the server see a sequence gap.
    if (stopped) return;
    frames += 1;
    try {
      segmenter.push(frame);
      sender.push(frame);
    } catch (err) {
      report(err);
      stop();
      return;
    }
    if (frames >= maxFrames) {
      // The receiver enforces the same cap; stopping here keeps the browser
      // from collecting audio the server would refuse.
      stop();
    }
  }

  async function start(stream) {
    if (starting || running || stopped) return false;
    starting = true;
    try {
      const modules = await loadModules();
      if (stopped) return false;
      const makeCapture =
        typeof opts.createFrameCapture === "function"
          ? opts.createFrameCapture
          : modules && modules.createFrameCapture;
      const makeSegmenter =
        typeof opts.createSegmenter === "function"
          ? opts.createSegmenter
          : modules && modules.createSegmenter;
      if (typeof makeCapture !== "function" || typeof makeSegmenter !== "function") {
        throw new Error("Frame capture is not available in this browser");
      }
      sender = createFrameSender({
        base: opts.base,
        wsId: opts.wsId,
        captureId,
        fetchImpl: opts.fetchImpl,
        onError: (err) => {
          // A refused batch ends the session; the recorder is untouched.
          report(err);
          stop();
        },
      });
      segmenter = makeSegmenter({ segmentGapMs, onSegment: emitSegment });
      capture = makeCapture({ onFrame: handleFrame, onError: report });
      const ok = await capture.start(stream);
      if (!ok || stopped) {
        if (capture) {
          const failedCapture = capture;
          capture = null;
          try {
            failedCapture.stop();
          } catch (e) {
            /* already torn down */
          }
        }
        if (sender) {
          const unsent = sender;
          sender = null;
          unsent.close();
        }
        segmenter = null;
        return false;
      }
      running = true;
      return true;
    } catch (err) {
      if (!stopped) report(err);
      stop();
      return false;
    } finally {
      starting = false;
    }
  }

  return {
    start,
    stop,
    get running() {
      return running;
    },
    captureId,
  };
}
