// ===========================================================================
//  turnstone dictation mic — shared module (shared_static/stt_mic.js)
//
//  The speech-to-text button lifted out of the interactive pane so the
//  coordinator composer dictates the same way: getUserMedia + MediaRecorder,
//  the upload to the workstream's /speech-to-text/stream route, and the
//  transcript deltas written into the composer as they arrive.
//
//  Transport is one knob, as in tts_player.js: `base` is "" for a same-origin
//  pane (the console's own coordinator route) and "/node/{id}" when the console
//  proxies a node-hosted session.  Each pane owns its own controller, so a
//  destroyed pane releases its own microphone.
//
//  ES module.  House style: programmatic DOM, NO innerHTML.
// ===========================================================================

import { authFetch } from "./auth.js";
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

// opts:
//   composer   the pane's Composer; the button joins composer.actionsRowEl
//              (before the send button) and transcripts land in composer.inputEl.
//   base       transport prefix, string or () => string (read per request).
//   wsId       workstream id, string or () => string (read per upload).
//   announce   live-region announcer; defaults to a module-owned region.
//   isBusy     () => boolean; a busy pane cannot send, so it cannot dictate.
//   sendBtn    the button to insert before; defaults to composer.sendBtn.
//
// Returns { button, setAvailable, syncBusy, toggle, stop, isRecording,
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
    if (stream) {
      stream.getTracks().forEach((t) => t.stop());
      stream = null;
    }
    recorder = null;
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
    if (!wsId || destroyed) return;
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
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          const chunk = decoder.decode(value, { stream: true });
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
      });
  }

  function start() {
    if (recording || destroyed || isBusy()) return;
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
          if (drop) return;
          const blob = new Blob(chunks, {
            type: rec.mimeType || mimeType || "audio/webm",
          });
          if (blob.size) upload(blob);
        });
        recorder = rec;
        recording = true;
        discard = false;
        startTimer();
        syncButton();
        announce("Recording. Activate the microphone button again to stop.");
        rec.start();
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
