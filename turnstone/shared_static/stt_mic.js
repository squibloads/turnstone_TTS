// ===========================================================================
//  turnstone dictation mic — shared module (shared_static/stt_mic.js)
//
//  The speech-to-text button lifted out of the interactive pane so the
//  coordinator composer dictates the same way: getUserMedia + MediaRecorder,
//  the upload to the workstream's /speech-to-text/stream route, and the
//  transcript deltas written into the composer as they arrive.  "Auto-stop"
//  (audio.stt_auto_stop) additionally watches the recording with a browser
//  voice-activity detector and ends it when the speaker goes quiet; the
//  detector itself is vendored, and its absence is a supported state.
//
//  Transport is one knob, as in tts_player.js: `base` is "" for a same-origin
//  pane (the console's own coordinator route) and "/node/{id}" when the console
//  proxies a node-hosted session.  Each pane owns its own controller, so a
//  destroyed pane releases its own microphone.
//
//  Frame capture (audio.stt_frame_capture, default off) is speaker-gate
//  groundwork: while the recorder runs, a second tap on the same stream sends
//  raw 32 ms frames to the server.  It is strictly additive — the clip upload
//  is untouched — and every capture failure is swallowed so a missing
//  AudioWorklet or a refused batch can never cost a dictation.
//
//  ES module.  House style: programmatic DOM, NO innerHTML.
// ===========================================================================

import { authFetch } from "./auth.js";
import { createFrameSession } from "./stt_frame_session.js";
import { showToast } from "./toast.js";
import { makeAnnouncer } from "./utils.js";

// Created on first use, not at import: the module must load before
// document.body exists in a test or a degraded page, and a pane that never
// dictates never needs a region.  Panes that already own a voice announcer pass
// it in so the mic and read-aloud share one region instead of clobbering.
let _defaultAnnounce = null;
function _announceMic(msg) {
  if (!_defaultAnnounce) _defaultAnnounce = makeAnnouncer();
  _defaultAnnounce(msg);
}

const _MIME_CANDIDATES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/ogg;codecs=opus",
  "audio/mp4",
];

// ---------------------------------------------------------------------------
//  Auto-stop: browser voice-activity detection
// ---------------------------------------------------------------------------

// Probabilities at or above `threshold` are speech; below `threshold` minus
// this gap they are silence.  The gap is what keeps a frame that wobbles
// around the threshold during speech from starting the silence clock, and it
// is the same 0.15 the setting's help text names.
const _VAD_FLOOR_GAP = 0.15;

// The settings route always sends all three fields.  These are the last-resort
// fallbacks for a degraded page (an older server, a failed probe) and match
// the registry defaults, so dictation still works without them.
const _STT_CONFIG_DEFAULTS = {
  auto_stop: false,
  silence_ms: 900,
  vad_threshold: 0.5,
  frame_capture: false,
  segment_gap_ms: 250,
  frame_capture_max_seconds: 300,
};

function _finite(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function _defaultSttConfig() {
  return {
    auto_stop: _STT_CONFIG_DEFAULTS.auto_stop,
    silence_ms: _STT_CONFIG_DEFAULTS.silence_ms,
    vad_threshold: _STT_CONFIG_DEFAULTS.vad_threshold,
    frame_capture: _STT_CONFIG_DEFAULTS.frame_capture,
    segment_gap_ms: _STT_CONFIG_DEFAULTS.segment_gap_ms,
    frame_capture_max_seconds: _STT_CONFIG_DEFAULTS.frame_capture_max_seconds,
  };
}

// One frame's decision.  `state` is the previous { speaking, silentSince }
// (null before the first frame); the return is { state, action } with action
// "start" when speech begins, "stop" when the mic should stop, else null.
//
// Speech starts at `threshold` and never before, so a recording that opens in
// silence cannot stop itself.  Silence is audio below threshold - 0.15, and a
// stop needs `silenceMs` of it *unbroken*: one frame at or above the floor
// restarts the clock.  Audio between the two thresholds is therefore neither
// speech nor silence - it cannot start a recording, and it protects a pause
// from being cut short.  The trade is that a room whose noise sits in that
// band never auto-stops; raise the threshold, or click the mic.
export function vadDecision(state, prob, nowMs, cfg) {
  const threshold = _finite(
    cfg && cfg.threshold,
    _STT_CONFIG_DEFAULTS.vad_threshold,
  );
  const silenceMs = Math.max(
    0,
    _finite(cfg && cfg.silenceMs, _STT_CONFIG_DEFAULTS.silence_ms),
  );
  const prev = state || null;
  const speaking = !!(prev && prev.speaking);
  if (prob >= threshold) {
    return {
      state: { speaking: true, silentSince: null },
      action: speaking ? null : "start",
    };
  }
  if (!speaking) {
    return { state: { speaking: false, silentSince: null }, action: null };
  }
  if (!(prob < threshold - _VAD_FLOOR_GAP)) {
    return { state: { speaking: true, silentSince: null }, action: null };
  }
  const since = prev.silentSince == null ? nowMs : prev.silentSince;
  if (nowMs - since >= silenceMs) {
    return { state: { speaking: false, silentSince: null }, action: "stop" };
  }
  return { state: { speaking: true, silentSince: since }, action: null };
}

// Dictation settings ride their own probe, memoized per transport base like the
// read-aloud ones: a console pane must read the settings of the node hosting
// its session, and a page that never dictates never asks.  A failed or
// unusable body resolves to the defaults above, so a degraded server costs
// auto-stop rather than dictation.
const _sttConfigPromises = {};

export function getSttConfig(base) {
  base = base || "";
  if (!_sttConfigPromises[base]) {
    _sttConfigPromises[base] = Promise.resolve()
      .then(() => authFetch(base + "/v1/api/stt/config"))
      .then((r) => (r.ok ? r.json() : null))
      .then((body) => {
        const cfg = _defaultSttConfig();
        if (!body) return cfg;
        cfg.auto_stop = body.auto_stop === true;
        cfg.silence_ms = _finite(body.silence_ms, cfg.silence_ms);
        cfg.vad_threshold = _finite(body.vad_threshold, cfg.vad_threshold);
        cfg.frame_capture = body.frame_capture === true;
        cfg.segment_gap_ms = _finite(body.segment_gap_ms, cfg.segment_gap_ms);
        cfg.frame_capture_max_seconds = _finite(
          body.frame_capture_max_seconds,
          cfg.frame_capture_max_seconds,
        );
        return cfg;
      })
      .catch(() => _defaultSttConfig());
  }
  return _sttConfigPromises[base];
}

// The detector is vendored, never fetched from a CDN: a voice session may run
// on an isolated deployment, and the model has to come from the app itself.
// The directory is optional at build time - when it is absent (or the browser
// cannot load the module) the import rejects and the mic keeps today's
// click-to-stop behaviour.  This indirection is the seam a vendored build (or
// a test) plugs into, so the rest of the file never names a VAD build; a
// failed load is remembered rather than retried on every recording.  The
// module may export either `loadVad()` or `watch` directly; see
// `docs/voice.md#auto-stop` and THIRD-PARTY-NOTICES for what ships in it.
const _VAD_MODULE = "./vad-0.0.31/vad_loader.js";
let _vadPromise = null;

export function loadVad() {
  if (!_vadPromise) {
    _vadPromise = Promise.resolve()
      .then(() => import(_VAD_MODULE))
      .then((m) => (m && typeof m.loadVad === "function" ? m.loadVad() : m))
      .then((vad) => (vad && typeof vad.watch === "function" ? vad : null))
      .catch(() => null);
  }
  return _vadPromise;
}

// opts:
//   composer   the pane's Composer; the button joins composer.actionsRowEl
//              (before the send button) and transcripts land in composer.inputEl.
//   base       transport prefix, string or () => string (read per request).
//   wsId       workstream id, string or () => string (read per upload).
//   announce   live-region announcer; defaults to a module-owned region.
//   isBusy     () => boolean; a busy pane cannot send, so it cannot dictate.
//   sendBtn    the button to insert before; defaults to composer.sendBtn.
//   onTranscript  optional (text) => void; called once per kept clip after its
//              transcript stream completes, with the text that clip added
//              (trimmed) — never for a discarded, failed or empty clip.
//   onToggle   optional () => boolean; asked first on every button press.  A
//              true return claims the press (hands-free voice mode uses the
//              button as its mic on/off switch); anything else records as usual.
//   onState    optional (state) => void; "recording" | "transcribing" |
//              "idle" on every change; a repeated state is not re-announced
//              and construction is not a change (the controller starts idle).
//   onStream   optional (stream | null) => void; the live microphone stream
//              once capture has started, and null when it is released.  Only
//              the page's analyser reads it, never the destination, so the
//              mic is never echoed back.  A denied or aborted capture never
//              hands one over.
//   loadVad    optional detector loader (tests / a pane with its own source);
//              resolves to { watch(stream, onProbability) -> { stop() } } or
//              null.  Defaults to the vendored build.
//   sttConfig  optional dictation settings, object or () => object; defaults
//              to the per-base GET /v1/api/stt/config probe.
//   frameSession  optional (opts) => { start(stream), stop() } factory for the
//              frame-capture session; defaults to stt_frame_session.js.  Tests
//              and panes with their own transport replace it here.
//   onSegment  optional (segment) => void; a closed short-gap segment.  Only
//              runs when frame capture is on; the speaker gate will consume it.
//
// Returns { button, setAvailable, syncBusy, toggle, start, stop, isRecording,
// isDenied, destroy }.  `button` is null when the composer has no action row.
// The button stays hidden until setAvailable(true), so it never appears when
// no STT role is configured.
export function createMicController(opts) {
  const composer = opts.composer;
  const announce = opts.announce || _announceMic;
  const isBusy = opts.isBusy || (() => false);
  const baseOf = () =>
    typeof opts.base === "function" ? opts.base() : opts.base || "";
  const wsIdOf = () =>
    typeof opts.wsId === "function" ? opts.wsId() : opts.wsId;
  // Two seams a test (or a pane with its own settings source) can replace; the
  // shipped defaults are the per-base settings probe and the vendored detector.
  const vadLoader = typeof opts.loadVad === "function" ? opts.loadVad : loadVad;
  const configOf = () => {
    const supplied =
      typeof opts.sttConfig === "function" ? opts.sttConfig() : opts.sttConfig;
    return supplied ? Promise.resolve(supplied) : getSttConfig(baseOf());
  };

  let btn = null;
  let icon = null;
  let denied = false;
  let recorder = null;
  let stream = null;
  let recording = false;
  let discard = false;
  let aborted = false;
  let timer = null;
  let startMs = 0;
  let uploadAbort = null;
  let destroyed = false;
  // Hands-free state: "idle" | "recording" | "transcribing".  Only changes are
  // reported, so a page renders the initial "idle" itself.
  let state = "idle";
  // Auto-stop state: the token invalidates a detector that is still loading
  // when the recording ends, `handle` is the live detector (released with the
  // stream), and `state` is the last frame's decision state.
  let vadToken = null;
  let vadHandle = null;
  let vadState = null;
  // Frame capture state, on the same token pattern as auto-stop: a session
  // still starting when the recording ends must be stopped, not adopted.
  let frameToken = null;
  let frameHandle = null;

  function setState(next) {
    if (next === state) return;
    state = next;
    if (typeof opts.onState === "function") opts.onState(next);
  }

  function syncButton() {
    if (!btn) return;
    const rec = !!recording;
    btn.classList.toggle("is-recording", rec);
    btn.setAttribute("aria-pressed", rec ? "true" : "false");
    if (icon) {
      icon.classList.toggle("icon-stop", rec);
      icon.classList.toggle("icon-mic", !rec);
    }
    const label = rec
      ? "Stop recording and transcribe"
      : "Record speech to text";
    btn.title = label;
    btn.setAttribute("aria-label", label);
  }

  function setDenied() {
    denied = true;
    showToast("Microphone access was denied", "error");
    syncButton();
    if (btn) {
      btn.disabled = true;
      const msg =
        "Microphone blocked — enable it in your browser's site settings";
      btn.title = msg;
      btn.setAttribute("aria-label", msg);
    }
  }

  function stopTimer() {
    if (timer) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  function tickTimer() {
    if (!recording || !btn) return;
    const secs = Math.max(0, Math.floor((Date.now() - startMs) / 1000));
    const mmss =
      Math.floor(secs / 60) + ":" + String(secs % 60).padStart(2, "0");
    btn.title = "Recording " + mmss + " — activate to stop";
  }

  function startTimer() {
    startMs = Date.now();
    stopTimer();
    timer = window.setInterval(tickTimer, 500);
  }

  function teardownStream() {
    // Releasing the microphone releases the detector and the frame tap with
    // it: a handle left running would keep reading a stream it no longer owns
    // (and could still call stop() on a finished recording).
    stopFrameCapture();
    stopVad();
    if (stream) {
      stream.getTracks().forEach((t) => t.stop());
      stream = null;
      // Only a real release is announced, so a repeated teardown is silent.
      if (typeof opts.onStream === "function") opts.onStream(null);
    }
    recorder = null;
  }

  function stopFrameCapture() {
    frameToken = null;
    if (frameHandle) {
      const handle = frameHandle;
      frameHandle = null;
      try {
        const flushing = handle.stop();
        // The sender's last batch is a promise; a failed one was already
        // reported by the session, so there is nothing to await here.
        if (flushing && typeof flushing.catch === "function") {
          flushing.catch(() => {});
        }
      } catch (e) {
        /* the capture was already torn down */
      }
    }
  }

  function stopVad() {
    vadToken = null;
    vadState = null;
    if (vadHandle) {
      const handle = vadHandle;
      vadHandle = null;
      try {
        handle.stop();
      } catch (e) {
        /* the detector was already torn down */
      }
    }
  }

  // Auto-stop is opt-in and best-effort: the setting must be on, the detector
  // must load, and this must still be the live recording.  Every failure path
  // leaves the mic exactly as it is today (click again to stop).  The settings
  // probe runs first so a deployment with the setting off never loads the
  // detector's model at all.
  function armAutoStop(s) {
    const token = {};
    vadToken = token;
    Promise.resolve(configOf())
      .then((cfg) => {
        // The recording can end while the settings are in flight; do not pay
        // for loading the detector's model when nobody is listening any more.
        if (!cfg || !cfg.auto_stop || vadToken !== token) return null;
        return Promise.resolve(vadLoader()).then((vad) => {
          if (!vad || vadToken !== token || stream !== s || destroyed) {
            return null;
          }
          vadState = null;
          return vad.watch(s, (prob) => {
            if (vadToken !== token) return;
            const step = vadDecision(vadState, prob, Date.now(), {
              threshold: cfg.vad_threshold,
              silenceMs: cfg.silence_ms,
            });
            vadState = step.state;
            if (step.action === "stop" && recording) stop();
          });
        });
      })
      .then((handle) => {
        if (!handle) return;
        if (vadToken !== token) {
          // The recording ended while the model was still loading.
          try {
            handle.stop();
          } catch (e) {
            /* nothing left to release */
          }
          return;
        }
        vadHandle = handle;
      })
      .catch(() => {});
  }

  // Frame capture is speaker-gate groundwork and is off unless the deployment
  // turns it on.  Like auto-stop it is best-effort: the settings probe runs
  // first (so an off deployment never imports the capture modules), and any
  // failure is logged and abandoned without touching the recorder or the clip
  // upload.
  function armFrameCapture(s) {
    const token = {};
    frameToken = token;
    Promise.resolve(configOf())
      .then((cfg) => {
        if (
          !cfg ||
          !cfg.frame_capture ||
          frameToken !== token ||
          stream !== s ||
          destroyed
        ) {
          return null;
        }
        const wsId = wsIdOf();
        if (!wsId) return null;
        const factory =
          typeof opts.frameSession === "function"
            ? opts.frameSession
            : createFrameSession;
        let handle = null;
        try {
          handle = factory({
            base: baseOf(),
            wsId: wsId,
            segmentGapMs: cfg.segment_gap_ms,
            maxSeconds: cfg.frame_capture_max_seconds,
            onSegment:
              typeof opts.onSegment === "function" ? opts.onSegment : null,
            onError: (err) => {
              if (typeof console !== "undefined" && typeof console.warn === "function") {
                console.warn(
                  "Voice frame capture:",
                  err && err.message ? err.message : err,
                );
              }
            },
          });
        } catch (err) {
          return null;
        }
        if (!handle || typeof handle.start !== "function") return null;
        // Adopt the handle before start() resolves: a teardown during the
        // worklet module load must be able to stop the graph the session is
        // already building.  createFrameCapture().stop() is safe mid-start.
        frameHandle = handle;
        return Promise.resolve()
          .then(() => handle.start(s))
          .then(
            () => {
              // The recording can end while the worklet module is still
              // loading; do not keep a graph nobody is listening to.  If
              // teardown already stopped this handle it cleared frameHandle,
              // and a second stop would only repeat the release.
              if (frameToken !== token || destroyed) {
                if (frameHandle === handle) {
                  frameHandle = null;
                  try {
                    handle.stop();
                  } catch (e) {
                    /* already released */
                  }
                }
              }
              return null;
            },
            () => {
              // A failed start already released its own graph; forget it so a
              // later teardown does not stop an unrelated session.
              if (frameHandle === handle) frameHandle = null;
              return null;
            },
          );
      })
      .catch(() => {});
  }

  // Stop recording.  `discardClip` abandons the audio (reset, busy, teardown);
  // false hands it to the upload.  Also aborts a getUserMedia still in flight.
  function stop(discardClip) {
    discard = !!discardClip;
    aborted = true;
    if (recorder && recorder.state !== "inactive") {
      try {
        recorder.stop();
      } catch (e) {
        /* already stopped */
      }
      return;
    }
    teardownStream();
    stopTimer();
    if (recording) {
      recording = false;
      syncButton();
    }
  }

  function upload(blob) {
    const wsId = wsIdOf();
    if (!wsId || destroyed) {
      setState("idle");
      return;
    }
    setState("transcribing");
    const ext =
      blob.type.indexOf("ogg") !== -1
        ? "ogg"
        : blob.type.indexOf("mp4") !== -1
          ? "mp4"
          : "webm";
    const fd = new FormData();
    fd.append("audio", blob, "speech." + ext);
    if (btn) {
      btn.disabled = true;
      btn.classList.add("is-busy");
    }
    announce("Transcribing…");
    const resetMic = () => {
      if (btn && !denied) {
        btn.disabled = !!isBusy();
        btn.classList.remove("is-busy");
      }
    };
    const mine = new AbortController();
    uploadAbort = mine;
    authFetch(
      baseOf() +
        "/v1/api/workstreams/" +
        encodeURIComponent(wsId) +
        "/speech-to-text/stream",
      { method: "POST", body: fd, signal: mine.signal },
    )
      .then(async (r) => {
        if (!r.ok) {
          let msg = "Transcription failed";
          try {
            const body = await r.json();
            if (body && body.error) msg = body.error;
          } catch (_e) {
            /* non-JSON error body */
          }
          showToast(msg, "error");
          return;
        }
        // Stream transcript deltas into the composer as they arrive (first word
        // in ~0.3s) instead of waiting for the whole transcript.
        const reader = r.body.getReader();
        const decoder = new TextDecoder();
        let started = false;
        let got = false;
        // The text this clip contributed, kept apart from the composer value
        // so a hands-free pane is not handed the user's typed prefix.
        let added = "";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          const chunk = decoder.decode(value, { stream: true });
          if (chunk) added += chunk;
          const input = composer.inputEl;
          if (!chunk || !input) continue;
          got = true;
          if (!started) {
            // Read the composer's value now (not before the await) so text the
            // user typed while transcribing isn't clobbered.
            const cur = input.value || "";
            input.value = cur ? cur.replace(/\s*$/, "") + " " + chunk : chunk;
            started = true;
          } else {
            input.value += chunk;
          }
          // Drive the composer's auto-resize + send-enable listeners.
          input.dispatchEvent(new Event("input", { bubbles: true }));
        }
        if (got) {
          if (composer.inputEl) composer.inputEl.focus();
          announce("Transcript added to message.");
        } else {
          showToast("No speech detected", "error");
        }
        // A hands-free pane sends the turn on this; whitespace-only streams
        // have no text to hand over and are not a clip's transcript.
        const text = added.trim();
        if (text && typeof opts.onTranscript === "function") {
          opts.onTranscript(text);
        }
      })
      .catch((err) => {
        // A destroyed pane aborted this itself; nobody is left to tell.
        if (destroyed) return;
        showToast(
          "Transcription failed: " + (err && err.message ? err.message : err),
          "error",
        );
      })
      .finally(() => {
        if (uploadAbort === mine) uploadAbort = null;
        resetMic();
        // A recording can begin while an upload is still draining; its state
        // wins over the older upload settling back to idle.
        setState(recording ? "recording" : "idle");
      });
  }

  function start() {
    if (recording || destroyed || denied || isBusy()) return;
    if (
      !navigator.mediaDevices ||
      !navigator.mediaDevices.getUserMedia ||
      typeof MediaRecorder === "undefined"
    ) {
      showToast("Microphone capture is not supported in this browser", "error");
      return;
    }
    // Synchronous abort latch: if teardown (reset / pane switch) runs while the
    // permission prompt is open, the stream the promise later hands us must be
    // stopped instead of going hot after teardown.
    aborted = false;
    navigator.mediaDevices
      .getUserMedia({ audio: true })
      .then((s) => {
        if (aborted) {
          s.getTracks().forEach((t) => t.stop());
          return;
        }
        stream = s;
        // The live stream is handed out once it is adopted, after the abort
        // latch, so a listener never sees a stream the controller discards.
        if (typeof opts.onStream === "function") opts.onStream(s);
        let mimeType = "";
        for (let i = 0; i < _MIME_CANDIDATES.length; i++) {
          if (
            MediaRecorder.isTypeSupported &&
            MediaRecorder.isTypeSupported(_MIME_CANDIDATES[i])
          ) {
            mimeType = _MIME_CANDIDATES[i];
            break;
          }
        }
        const rec = mimeType
          ? new MediaRecorder(s, { mimeType })
          : new MediaRecorder(s);
        const chunks = [];
        rec.addEventListener("dataavailable", (e) => {
          if (e.data && e.data.size) chunks.push(e.data);
        });
        rec.addEventListener("stop", () => {
          teardownStream();
          recording = false;
          stopTimer();
          syncButton();
          const drop = discard;
          discard = false;
          if (drop) {
            setState("idle");
            return;
          }
          const blob = new Blob(chunks, {
            type: rec.mimeType || mimeType || "audio/webm",
          });
          if (blob.size) upload(blob);
          else setState("idle");
        });
        recorder = rec;
        recording = true;
        discard = false;
        startTimer();
        syncButton();
        setState("recording");
        announce("Recording. Activate the microphone button again to stop.");
        rec.start();
        armAutoStop(s);
        armFrameCapture(s);
      })
      .catch(() => {
        // Denied / hardware unavailable: leave a persistent disabled state with
        // guidance — a hot button reads as dead once the browser blocks re-prompts.
        teardownStream();
        recording = false;
        stopTimer();
        setDenied();
      });
  }

  function toggle() {
    if (typeof opts.onToggle === "function" && opts.onToggle() === true) return;
    if (recording) stop(false);
    else start();
  }

  if (composer && composer.actionsRowEl) {
    btn = document.createElement("button");
    btn.type = "button";
    btn.className = "composer-mic-btn";
    btn.style.display = "none"; // revealed once the STT role is confirmed
    btn.title = "Record speech to text";
    btn.setAttribute("aria-label", "Record speech to text");
    btn.setAttribute("aria-pressed", "false");
    icon = document.createElement("span");
    icon.className = "composer-mic-icon icon-mic";
    icon.setAttribute("aria-hidden", "true");
    btn.appendChild(icon);
    btn.addEventListener("click", toggle);
    composer.actionsRowEl.insertBefore(
      btn,
      opts.sendBtn || composer.sendBtn || null,
    );
  }

  return {
    button: btn,
    toggle,
    start,
    stop,
    isRecording: () => recording,
    isDenied: () => denied,
    // Show or hide the button as the STT role resolves.  The action row also
    // right-aligns the mic next to send (instead of stranded by the model
    // chip) only when STT is actually available; .has-mic drives the CSS.
    setAvailable(on) {
      if (btn) btn.style.display = on ? "" : "none";
      if (composer && composer.actionsRowEl)
        composer.actionsRowEl.classList.toggle("has-mic", !!on);
    },
    // The mic produces composer input the user can only send when idle — keep
    // it in lockstep with the send button.  Call on every busy edge.
    syncBusy() {
      if (!btn || denied) return;
      if (isBusy()) {
        stop(true); // abandon any in-flight recording
        btn.disabled = true;
      } else if (!btn.classList.contains("is-busy")) {
        btn.disabled = false;
      }
    },
    // Release the microphone and any transcription still streaming, and take
    // the button out of the composer.
    destroy() {
      destroyed = true;
      stop(true);
      if (uploadAbort) uploadAbort.abort();
      if (btn && btn.parentNode) btn.parentNode.removeChild(btn);
    },
  };
}
