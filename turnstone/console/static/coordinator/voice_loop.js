// ===========================================================================
//  turnstone hands-free voice loop — coordinator module
//
//  Owns the turn cycle for Voice mode:
//
//      off ──enable──▶ listening ──transcript──▶ thinking ──speech──▶ speaking
//        ◀──disable──┘     ▲                         │                 │
//                          └──── playback done + coordinator idle ────┘
//
//  DOM-free on purpose.  The coordinator pane supplies the microphone, the
//  send path and the status line, so the cycle is testable without a browser
//  and later reusable from another pane.  Half-duplex falls out of the states:
//  the mic is started only on entering `listening`; thinking, speaking and
//  transcribing all keep it closed, so playback is never captured.
//
//  The caller owns the two facts the loop cannot observe itself:
//    - onBusy(bool)        the coordinator's turn state (setBusy edges)
//    - onReplyStarted()    the reply's speech has begun (the player's run was
//                          created / played), moving thinking -> speaking
//  and forwards the player's run.done result to onPlaybackDone.  A run that
//  ended with `heard: []` is a reply with nothing to say, not a failure.
//
//  onMicState() is the bridge for the mic controller's onState callback: it
//  makes the short `transcribing` phase visible (and recovers `listening`
//  when a clip was discarded or failed).  It is additive to the required
//  surface; a pane that does not call it still runs the cycle, because the
//  transcript itself moves listening -> thinking.
//
//  setMuted(bool) is the user's mic switch inside the mode.  Muted, the loop
//  runs every turn exactly as before (replies and worker reports still play)
//  but never opens the mic: `listening` reports as `muted` instead.  That lets
//  the user talk to someone else while waiting for a job without the session
//  hearing it.  Only `disable()` clears it.
//
//  idleMuteMs (default 0 = never) mutes the loop itself after that long with
//  the mic open and nothing heard, through the same switch, then calls
//  onIdleMute().  A forgotten session is otherwise a live microphone that
//  will hear the next thing said in the room and send it as an instruction.
//  Only time the mic is actually open counts: detected speech restarts it,
//  and thinking, speaking or a mic still waiting on a busy turn do not count.
//  setTimer/clearTimer default to setTimeout/clearTimeout (injected in tests).
// ===========================================================================

// The status strings live in the pane: this module only names the states.
export function createVoiceLoop(opts) {
  opts = opts || {};
  const startListening =
    typeof opts.startListening === "function" ? opts.startListening : () => {};
  const stopListening =
    typeof opts.stopListening === "function" ? opts.stopListening : () => {};
  const send = typeof opts.send === "function" ? opts.send : () => {};
  const setStatus = typeof opts.setStatus === "function" ? opts.setStatus : () => {};
  const onIdleMute = typeof opts.onIdleMute === "function" ? opts.onIdleMute : () => {};
  const idleMuteMs = Number(opts.idleMuteMs) > 0 ? Number(opts.idleMuteMs) : 0;
  const setTimer =
    typeof opts.setTimer === "function" ? opts.setTimer : (fn, ms) => setTimeout(fn, ms);
  const clearTimer =
    typeof opts.clearTimer === "function" ? opts.clearTimer : (t) => clearTimeout(t);

  let state = "off";
  // The coordinator's own turn state, mirrored from setBusy.  Completion of a
  // spoken turn is the AND of this and the player's run.done, in either order.
  let busy = false;
  // Whether this turn ever produced a speech run.  A turn that goes idle with
  // no run had nothing to say (empty reply, tool-only turn), so it must not
  // wait forever for a done that will never arrive.
  let replyRun = false;
  // The player's completion result for this turn, if it has arrived.
  let doneResult = null;
  // Set when a listening entry could not open the mic — the coordinator was
  // still mid-turn (a session reopened while another tab's turn ran).  The
  // idle edge completes that entry rather than making a second transition.
  let micPending = false;
  // The user's mic switch; see the header.  Independent of `state`.
  let muted = false;
  // The quiet-minute countdown, armed only while the mic is open.  `idleSeq`
  // lets a callback that was already queued when its timer was cleared see
  // that it is stale and do nothing.
  let idleTimer = null;
  let idleSeq = 0;

  function clearIdle() {
    idleSeq++;
    if (idleTimer !== null) clearTimer(idleTimer);
    idleTimer = null;
  }

  function armIdle() {
    clearIdle();
    if (!idleMuteMs) return;
    const seq = idleSeq;
    idleTimer = setTimer(() => {
      if (seq !== idleSeq) return;
      idleTimer = null;
      if (state !== "listening" || muted || micPending) return;
      setMuted(true);
      onIdleMute();
    }, idleMuteMs);
  }

  function resetTurn() {
    replyRun = false;
    doneResult = null;
  }

  // Every state change goes through here so the pane's status line can never
  // drift from the machine.  Entering listening deliberately bypasses the
  // same-state guard: the mic stopped for transcription (or an empty
  // transcript) is a fresh listening entry even though the state label did
  // not change, and it is the only place startListening may fire.  Callers
  // guard `off` themselves — enable() is the one entry that starts from it.
  //
  // startListening returns false when it could not open the mic; that refusal
  // is remembered, not retried in a loop, so a busy coordinator costs one
  // delayed start instead of a microphone fighting the turn.
  function enterListening() {
    state = "listening";
    resetTurn();
    if (muted) {
      micPending = false;
      setStatus("muted");
      return;
    }
    setStatus("listening");
    micPending = startListening() === false;
    if (micPending) clearIdle();
    else armIdle();
  }

  function enter(next) {
    if (state === "off") return;
    state = next;
    setStatus(next);
  }

  // Leaving the mic-open window (`listening` or `transcribing`) closes the
  // mic.  The mic stops its own recording to upload a clip, so this is usually
  // a no-op; it is the guarantee that a clip is never left recording into a
  // turn (half-duplex), and that a clip is discarded rather than sent.
  function leaveListening() {
    if (state !== "listening" && state !== "transcribing") return;
    clearIdle();
    micPending = false;
    stopListening();
  }

  // A turn is over when playback reported done AND the coordinator is idle,
  // whichever lands last.  A turn that never started a reply run is over on
  // the idle edge alone: there is no audio to wait for.
  function maybeFinish() {
    if (state !== "thinking" && state !== "speaking") return;
    if (busy) return;
    if (doneResult) {
      enterListening();
      return;
    }
    if (!replyRun) enterListening();
  }

  function enable() {
    if (state !== "off") return;
    enterListening();
  }

  function disable() {
    if (state === "off") return;
    // Discard anything in flight: disabling the mode must never send a clip
    // the user had already walked away from.  After this the state is off, so
    // every later callback is ignored.
    clearIdle();
    stopListening();
    state = "off";
    busy = false;
    micPending = false;
    muted = false;
    resetTurn();
    setStatus("off");
  }

  // One final transcript per clip.  Only the listening/transcribing window
  // accepts it; text that arrives while a turn runs is ignored, never queued
  // (the mic should not have been open, and replaying it later would send a
  // stale turn).
  function onTranscript(text) {
    if (state !== "listening" && state !== "transcribing") return;
    // A clip that was still uploading when the user muted: they have turned
    // to someone else, so whatever it caught is not for this session.
    if (muted) return;
    const trimmed = String(text == null ? "" : text).trim();
    if (!trimmed) {
      // Nothing was heard: reopen the mic instead of sending an empty turn.
      enterListening();
      return;
    }
    leaveListening();
    state = "thinking";
    resetTurn();
    setStatus("thinking");
    try {
      send(trimmed);
    } catch (_e) {
      // A throwing send (a degraded pane, a blocked composer) must not leave
      // the loop waiting for a reply that was never requested.
      enterListening();
    }
  }

  function onBusy(value) {
    if (state === "off") return;
    const next = !!value;
    if (next) {
      // A turn can start without this loop's transcript — a typed message, a
      // retry, another tab.  Close the mic before that turn can speak: this
      // is the half-duplex guarantee, and it also stops the recording rather
      // than letting it run under the reply.  A clip still uploading is
      // abandoned the same way: its transcript must not arrive later and
      // queue a stale turn behind this one.
      if (state === "listening" || state === "transcribing") {
        leaveListening();
        resetTurn();
        enter("thinking");
      }
      busy = true;
      return;
    }
    busy = false;
    // A listening entry made while the coordinator was busy could not open the
    // mic (the mic controller refuses then).  This is the edge that fulfils
    // it: clear the flag first so one entry costs one start, never a retry
    // loop, and remember a second refusal for the next idle edge.
    if (micPending && state === "listening") {
      micPending = false;
      if (startListening() === false) micPending = true;
      else armIdle();
      return;
    }
    maybeFinish();
  }

  function onReplyStarted() {
    // Only a turn this loop is waiting on can start speaking; a run from an
    // earlier turn (or a manual speaker press while idle) is not ours.
    if (state !== "thinking" || doneResult) return;
    replyRun = true;
    enter("speaking");
  }

  function onPlaybackDone(result) {
    if (state !== "thinking" && state !== "speaking") return;
    const r = result || { reason: "finished", heard: [] };
    doneResult = r;
    // `cancelled` covers cancel(), a superseding run, an error and an
    // interrupted stop.  It still has to respect the busy edge: a turn that is
    // cancelled server-side settles a moment later, and reopening the mic
    // while it is still marked busy would only make the mic controller refuse
    // the start.
    maybeFinish();
  }

  // Mid-turn the switch only records the choice: the mic is already shut, and
  // the turn's own end (enterListening) honours it.  In the listening window
  // it acts at once — muting discards a clip in flight, unmuting opens the mic
  // (or defers to the idle edge, like any listening entry).
  function setMuted(on) {
    if (state === "off") return;
    const next = !!on;
    if (next === muted) return;
    muted = next;
    if (state !== "listening" && state !== "transcribing") return;
    if (muted) {
      clearIdle();
      stopListening();
      micPending = false;
      state = "listening";
      setStatus("muted");
      return;
    }
    if (busy) {
      // A turn is about to own the mic window; the busy edge moves to
      // thinking and the idle edge reopens the mic.
      state = "listening";
      setStatus("listening");
      micPending = true;
      return;
    }
    enterListening();
  }

  function onMicState(next) {
    if (state === "off") return;
    if (muted) return;
    if (next === "transcribing") {
      if (state === "listening") {
        clearIdle();
        enter("transcribing");
      }
      return;
    }
    if (next === "idle" && state === "transcribing") {
      // The clip was discarded or its upload failed: onTranscript is never
      // called for those, so this is the only edge that can bring the mic
      // back.  (The success path calls onTranscript first, which moves the
      // loop to thinking before the mic reports idle.)
      enterListening();
    }
  }

  return {
    enable,
    disable,
    onTranscript,
    onBusy,
    onReplyStarted,
    onPlaybackDone,
    // Additive bridge for the mic's onState; see the header.
    onMicState,
    setMuted,
    get state() {
      return state;
    },
    get muted() {
      return muted;
    },
  };
}
