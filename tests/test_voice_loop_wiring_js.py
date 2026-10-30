"""Voice mode wiring in the coordinator pane (``coordinator.js``).

The shipped voice-mode functions are sliced out of ``createCoordinatorPane``
and run in node against the real ``voice_loop.js``, a fake microphone
controller that exposes the two hooks the pane passes in, and a controllable
TTS player, so these tests cover the offer gate (roles, setting, persona), the
transcript -> send path, the run's ``done`` -> loop path and teardown.
The state machine itself is covered by ``test_voice_loop_js``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import (
    demodulize,
    extract_braced,
    node_skip,
    run_node_source,
    strip_js_comments,
)
from tests._tts_js_stubs import TREE_DOM

pytestmark = node_skip

_STATIC = Path(__file__).resolve().parents[1] / "turnstone"
_COORD = (_STATIC / "console/static/coordinator/coordinator.js").read_text(encoding="utf-8")
_LOOP = demodulize(_STATIC / "console/static/coordinator/voice_loop.js")


def _fn(sig: str) -> str:
    """The shipped function, or a throwing stub when the source lacks it, so a
    test against code that predates the wiring fails on its assertions."""
    if sig not in _COORD:
        name = sig.split("(")[0].split()[-1]
        return f"function {name}() {{ throw new Error('missing {name}'); }}"
    return extract_braced(_COORD, sig)


def _decl(pattern: str, fallback: str) -> str:
    """One shipped declaration, or a stand-in that makes the missing wiring
    fail the test rather than the harness."""
    m = re.search(pattern, _COORD)
    return m.group(0) if m else fallback


_COORD_FNS = "\n".join(
    _fn(sig)
    for sig in (
        "  function _applyVoiceRoles(roles) {",
        "  function _buildVoiceControls() {",
        "  function _voiceHooksReady() {",
        "  function _voicePlayerSupportsDone() {",
        "  function _voiceNoDone() {",
        "  function _readVoiceModeEnabled() {",
        "  function _readVoicePersona() {",
        "  function _voiceSetStatus(state) {",
        "  function _voiceShow(on) {",
        "  function _voiceCreateLoop() {",
        "  function _sendVoiceText(text) {",
        "  function _voiceTurnOn() {",
        "  function _voiceTurnOff() {",
        "  function _voiceToggle() {",
        "  function _voiceMicToggle() {",
        "  function _voicePaintMute() {",
        "  function _voiceRefresh() {",
        "  function _voiceSetup() {",
        "  function _watchVoiceRun(run, replyEl) {",
        "  function _voiceSpeakAwaiting() {",
        "  function _voicePlayFinished(replyEl) {",
        "  function _refreshRetryButton() {",
        "  function _feedTtsStream(text) {",
        "  function _endTtsStream() {",
        "  function _cancelTtsStream() {",
        "  function setBusy(b, source) {",
        "  function destroy() {",
    )
)

# The pane's own voice-mode state, extracted so a removed declaration fails the
# harness rather than resolving to an accidental global.
_VOICE_STATE = "\n".join(
    (
        _decl(r"let voiceLoop = [^;]*;", "let voiceLoop = undefined;"),
        _decl(r"let voiceModeOn = [^;]*;", "let voiceModeOn = undefined;"),
        _decl(r"let voiceOffer = [^;]*;", "let voiceOffer = undefined;"),
        _decl(r"let voiceSetup = [^;]*;", "let voiceSetup = undefined;"),
        _decl(r"let voiceDoneProbe = [^;]*;", "let voiceDoneProbe = undefined;"),
        _decl(r"let voiceAutoStop = [^;]*;", "let voiceAutoStop = undefined;"),
        _decl(
            r"const voiceControls = _buildVoiceControls\(\);",
            "const voiceControls = { button: new El('button'), status: new El('div') };",
        ),
        _decl(r"const voiceBtn = voiceControls\.button;", "const voiceBtn = voiceControls.button;"),
        _decl(
            r"const voiceStatusEl = voiceControls\.status;",
            "const voiceStatusEl = voiceControls.status;",
        ),
    )
)

# The mic declaration is the shipped one, evaluated against a factory that
# records the options and exposes the two hooks as callable methods.
_MIC_DECL = _decl(
    r"const mic = createMicController\(\{[\s\S]*?\}\);",
    "const mic = { setAvailable() { throw new Error('missing mic'); }, button: null };",
)

_STATE = """
const messagesEl = new El("div");
document.documentElement.appendChild(messagesEl);
let wsId = "coord-1", busy = false, busySource = null;
let currentReasoningEl = null, currentReasoningBuf = "";
const reasoningActivity = { finish() {} };
const queue = { onIdleEdge() {} };
function reconcileSendBlock() {}
function _addTtsActions() {
  // What the real helper does for a settled reply: a speaker on its bubble,
  // but only when there is speakable text (a code-only reply gets none).
  if (!voiceRoles.tts) return;
  for (const el of messagesEl.querySelectorAll(".msg.assistant")) {
    if (el === currentAssistantEl) continue;
    if (el.querySelector(".msg-tts-btn")) continue;
    const b = el.querySelector(".msg-body");
    if (!b || !b.textContent.trim()) continue;
    const btn = new El("button"); btn.className = "msg-tts-btn"; el.appendChild(btn);
  }
}
// The settle edge the SSE router and the history render both land on.
function finishAssistantStream() {
  currentAssistantEl = null;
  currentAssistantBuf = "";
  _refreshRetryButton();
}
let currentAssistantBuf = "";
function _addRetryAction(el) {}
let voiceRoles = { stt: false, tts: false };
let ttsStream = null;
let currentAssistantEl = null;
let voiceAwaitingReply = null;
const ttsLog = { streams: [], plays: [], stops: 0 };
const tick = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r)); };
function makeRun() {
  let resolve = null;
  let reject = null;
  const run = {
    pushed: [], ended: false, cancelled: false, el: null, heard: [],
    push(text) { this.pushed.push(text); },
    end() { this.ended = true; },
    cancel() { this.cancelled = true; },
    done: new Promise((res, rej) => { resolve = res; reject = rej; }),
    resolve: (value) => resolve(value),
    reject: (value) => reject(value),
  };
  return run;
}
"""

# The composer stand-in: the pane reads .value, writes it, and coordSend clears
# it — the same contract the real Composer offers.
_COMPOSER = """
const composer = {
  value: "", el: new El("div"),
  actionsRowEl: new El("div"), sendBtn: new El("button"),
  setBusy() {}, setSendBlocked() {},
};
composer.actionsRowEl.appendChild(composer.sendBtn);
composer.el.appendChild(composer.actionsRowEl);
"""

_FAKES = """
function createMicController(opts) {
  return {
    button: new El("button"), opts,
    started: 0, stopped: 0,
    start() { this.started++; },
    stop(discard) { this.stopped++; this.lastDiscard = discard; },
    setAvailable() {}, syncBusy() {}, destroy() { this.destroyed = true; },
  };
}
let autoStop = true;            // audio.stt_auto_stop on the same probe
let voiceSetting = true;          // audio.voice_mode_enabled on the STT config probe
let personaRows = [];             // GET /v1/api/workstreams rows
let probeDown = false;            // a failed settings probe must default to on
function authFetch(url) {
  if (url === "/v1/api/stt/config") {
    if (probeDown) return Promise.reject(new Error("down"));
    return Promise.resolve({ ok: true, json: async () => ({ auto_stop: autoStop, voice_mode_enabled: voiceSetting }) });
  }
  if (url === "/v1/api/workstreams") {
    return Promise.resolve({ ok: true, json: async () => ({ workstreams: personaRows }) });
  }
  return Promise.resolve({ ok: false, json: async () => ({}) });
}
// The send path the loop calls.  coordSend clears the composer on acceptance
// and flips busy; `sendRefused` reproduces the paths that leave both alone.
const sent = [];
let sendRefused = false;
function coordSend() {
  const text = composer.value.trim();
  if (!text) return false;
  if (sendRefused) return false;
  sent.push(text);
  composer.value = "";
  setBusy(true, "optimistic");
  return false;
}
// The spectrum strip has its own suite (test_voice_viz_wiring_js); here it is a
// no-op apart from the turn handoff, which is recorded for the pending tests.
const vizLog = { pending: [] };
const voiceViz = { show() {}, hide() {}, detach() {}, attachStream() {}, attachAudio() {}, setSpeaker() {}, setPending(on) { vizLog.pending.push(on); }, resumeFromGesture() { return Promise.resolve(); } };
const ttsPlayer = {
  stream() { const r = makeRun(); ttsLog.streams.push(r); return r; },
  play(el, btn) { const r = makeRun(); r.el = el; r.btn = btn; ttsLog.plays.push(r); return r; },
  stop() { ttsLog.stops++; },
};
const attached = { closeStreamTransport: () => {}, historyRepair: { clear() {} }, histAttempts: new Map() };
const closeStreamTransport = attached.closeStreamTransport;
const historyRepair = attached.historyRepair, histAttempts = attached.histAttempts;
let staleRetryTimer = null, cancelTimeoutId = null, forceTimeoutId = null, tasksRefreshTimer = null,
  liveBadgeFlushTimer = null, pruneTimer = null, unmountTranscriptPresentation = null;
const removeVisibilityHandler = () => {}, _childObserver = null, unregisterTranscriptScroller = () => {};
const scrollFollow = { destroy() {} };
"""

_PRELUDE = "\n".join(
    (
        TREE_DOM,
        _LOOP,
        _STATE,
        _COMPOSER,
        _FAKES,
        _MIC_DECL,
        _VOICE_STATE,
        _COORD_FNS,
    )
)


def _run(body: str, extra: str = "") -> Any:
    src = (
        _PRELUDE
        + extra
        + f"\n(async () => {{\n{body}\n}})().catch((e) => {{ console.error(e); process.exit(1); }});"
    )
    r = run_node_source(src)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def _show() -> str:
    return 'const shown = () => [voiceBtn.style.display, voiceBtn.attrs["aria-pressed"]];'


def _settle() -> str:
    # The settings + persona probes are two awaits deep; six turns is ample.
    return "await tick(); await tick();"


def test_the_button_appears_only_with_both_roles_and_the_setting_on() -> None:
    out = _run(
        _show()
        + """
voiceSetting = true; personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: false });     // dictation alone does not offer hands-free
"""
        + _settle()
        + """
const sttOnly = shown();
_applyVoiceRoles({ stt: false, tts: true });     // read-aloud alone is not a conversation
"""
        + _settle()
        + """
const ttsOnly = shown();
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
const both = shown();
console.log(JSON.stringify({ sttOnly, ttsOnly, both, micAvailable: mic.opts.onTranscript !== undefined }));
"""
    )
    assert out["sttOnly"][0] == "none" and out["ttsOnly"][0] == "none"
    assert out["both"][0] == ""


def test_the_setting_being_off_hides_the_button_even_with_both_roles() -> None:
    out = _run(
        _show()
        + """
voiceSetting = false;
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
console.log(JSON.stringify({ shown: shown(), state: voiceLoop && voiceLoop.state }));
"""
    )
    assert out["shown"] == ["none", "false"]
    assert out["state"] is None  # the mode never started


def test_a_failed_settings_probe_defaults_to_offering_the_button() -> None:
    """Missing field or a dead probe = the registry default, which is on."""
    out = _run(
        _show()
        + """
probeDown = true;
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
console.log(JSON.stringify({ shown: shown() }));
"""
    )
    assert out["shown"] == ["", "false"]  # offered, but the persona is not voice


def test_a_voice_persona_starts_listening_and_another_persona_does_not() -> None:
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
voiceSetting = true;
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
const voice = { state: voiceLoop.state, started: mic.started, pressed: voiceBtn.attrs["aria-pressed"] };
personaRows = [{ ws_id: "coord-1", persona: "coordinator" }];
voiceSetup = null; voiceLoop = null; voiceDoneProbe = null; ttsStream = null;
voiceModeOn = false;
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
const other = { offered: voiceBtn.style.display, started: mic.started, mode: voiceModeOn };
console.log(JSON.stringify({ voice, other }));
"""
    )
    assert out["voice"] == {"state": "listening", "started": 1, "pressed": "true"}
    assert out["other"] == {"offered": "", "started": 1, "mode": False}


def test_one_click_toggles_exactly_once() -> None:
    """A listener bound twice would flip on and straight back off."""
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "coordinator" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
const clicks = voiceBtn.l.click.length;
voiceBtn.click();
const on = [voiceModeOn, voiceLoop.state, mic.started];
voiceBtn.click();
const off = [voiceModeOn, mic.stopped, voiceLoop.state];
console.log(JSON.stringify({ clicks, on, off }));
"""
    )
    assert out["clicks"] == 1
    assert out["on"] == [True, "listening", 1]
    assert out["off"] == [False, 1, "off"]


def test_a_transcript_calls_the_send_path_with_the_composer_text() -> None:
    """stt_mic already wrote the transcript into the composer: the pane sends
    that text and must not append it a second time."""
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
composer.value = "what needs my attention";     // the mic's insertion
mic.opts.onTranscript("what needs my attention");
const afterSend = { sent: sent.slice(), busy, loop: voiceLoop.state, cancelled: mic.lastDiscard };
console.log(JSON.stringify(afterSend));
"""
    )
    assert out["sent"] == ["what needs my attention"]
    assert out["busy"] is True
    assert out["loop"] == "thinking"
    # Leaving listening stops (discards) the mic rather than sending a clip.
    assert out["cancelled"] is True


def test_a_second_insertion_is_not_appended_by_the_pane() -> None:
    """A pane that appended `text` would send "x x" when the mic already
    inserted it; the send path sees the composer text only."""
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
composer.value = "hello there";
mic.opts.onTranscript("hello there");
console.log(JSON.stringify({ sent, composerAfter: composer.value }));
"""
    )
    assert out["sent"] == ["hello there"]
    assert out["composerAfter"] == ""


def test_a_refused_send_does_not_leave_the_loop_waiting() -> None:
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
composer.value = "hello";
sendRefused = true;
mic.opts.onTranscript("hello");
const state = voiceLoop.state;
console.log(JSON.stringify({ state, sent }));
"""
    )
    assert out["sent"] == []
    assert out["state"] == "listening"


def test_busy_edges_reach_the_loop_and_close_the_mic() -> None:
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
const listening = voiceLoop.state;
setBusy(true);                                  // a typed turn starts: mic closes
const during = { state: voiceLoop.state, stopped: mic.stopped };
setBusy(false);                                 // no reply run: straight back to listening
const after = voiceLoop.state;
console.log(JSON.stringify({ listening, during, after, started: mic.started }));
"""
    )
    assert out["listening"] == "listening"
    assert out["during"] == {"state": "thinking", "stopped": 1}
    assert out["after"] == "listening"
    assert out["started"] == 2


def test_the_run_done_returns_the_loop_to_listening_after_the_turn_settles() -> None:
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
composer.value = "hello";
mic.opts.onTranscript("hello");                 // thinking, busy=true
_feedTtsStream("the reply ");
const run = ttsLog.streams[0];
const speaking = voiceLoop.state;
run.resolve({ reason: "finished", heard: ["the reply"] });
await tick();
const whileBusy = voiceLoop.state;
setBusy(false);
console.log(JSON.stringify({ speaking, whileBusy, after: voiceLoop.state }));
"""
    )
    assert out["speaking"] == "speaking"
    assert out["whileBusy"] == "speaking"
    assert out["after"] == "listening"


def test_a_stream_run_without_done_stands_voice_mode_down() -> None:
    """An older player's run cannot report its end.  Parking the loop in
    `speaking` would leave the mic closed for good, so the mode stands down."""
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
const run = { push() {}, end() {}, cancel() {} };   // no done
_watchVoiceRun(run, null);
console.log(JSON.stringify({
  mode: voiceModeOn, shown: voiceBtn.style.display,
  stopped: mic.stopped, state: voiceLoop && voiceLoop.state,
}));
"""
    )
    assert out["mode"] is False
    assert out["shown"] == "none"
    assert out["stopped"] >= 1  # the mic was closed with the mode
    assert out["state"] == "off"


def test_a_stream_run_with_empty_heard_plays_the_finished_reply() -> None:
    """audio.tts_stream is off, so the stream run drops its text.  Voice mode
    must still speak: the settled reply goes through the speaker path."""
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
const el = new El("div"); el.className = "msg assistant";
const body = new El("div"); body.className = "msg-body"; el.appendChild(body);
const btn = new El("button"); btn.className = "msg-tts-btn"; el.appendChild(btn);
messagesEl.appendChild(el);
currentAssistantEl = el;                        // the bubble the reply is arriving in
composer.value = "hello";
mic.opts.onTranscript("hello");
_feedTtsStream("the reply ");
const streamRun = ttsLog.streams[0];
streamRun.resolve({ reason: "finished", heard: [] });   // the gate was off
await tick();
const beforeSettle = ttsLog.plays.length;      // still streaming: no speaker yet
finishAssistantStream();                       // stream_end -> _refreshRetryButton
const played = ttsLog.plays.length;
const stillSpeaking = voiceLoop.state;
ttsLog.plays[0].resolve({ reason: "finished", heard: ["the reply"] });
await tick();
setBusy(false);
console.log(JSON.stringify({ beforeSettle, played, stillSpeaking, after: voiceLoop.state }));
"""
    )
    assert out["beforeSettle"] == 0
    assert out["played"] == 1
    assert out["stillSpeaking"] == "speaking"
    assert out["after"] == "listening"


def test_a_cancelled_playback_returns_to_listening_not_off() -> None:
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
composer.value = "hello";
mic.opts.onTranscript("hello");
_feedTtsStream("the reply ");
ttsLog.streams[0].resolve({ reason: "cancelled", heard: [] });
await tick();
setBusy(false);
console.log(JSON.stringify({ mode: voiceModeOn, state: voiceLoop.state }));
"""
    )
    assert out["mode"] is True
    assert out["state"] == "listening"


# --- the spectrum strip's turn handoff -------------------------------------


def test_a_sent_spoken_turn_starts_the_pending_handoff() -> None:
    # The strip warms to the AI colour from the moment the heard text is
    # submitted, before any reply audio exists to read.
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
composer.value = "hello";
mic.opts.onTranscript("hello");
console.log(JSON.stringify({
  pending: vizLog.pending.slice(-1), state: voiceLoop.state, busy,
}));
"""
    )
    assert out["pending"] == [True]
    assert out["state"] == "thinking"
    assert out["busy"] is True


def test_the_first_reply_audio_ends_the_pending_handoff() -> None:
    # Reply playback starts the moment its run is created and told to speak;
    # the strip must already be in the accent then, not fade behind the voice.
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
composer.value = "hello";
mic.opts.onTranscript("hello");
const beforeReply = vizLog.pending.slice(-1);
_feedTtsStream("the reply ");
console.log(JSON.stringify({
  beforeReply, afterReply: vizLog.pending.slice(-1), state: voiceLoop.state,
}));
"""
    )
    assert out["beforeReply"] == [True]
    assert out["afterReply"] == [False]
    assert out["state"] == "speaking"


def test_a_turn_that_ends_without_a_reply_clears_the_pending_handoff() -> None:
    # A reply with nothing to say, a server error or a cancel before any audio
    # is the end of the wait; the strip must not stay warmed for a reply that
    # will never play.
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
composer.value = "hello";
mic.opts.onTranscript("hello");
const during = vizLog.pending.slice(-1);
setBusy(false);                            // idle edge, no reply run was made
console.log(JSON.stringify({ during, after: vizLog.pending.slice(-1), state: voiceLoop.state }));
"""
    )
    assert out["during"] == [True]
    assert out["after"] == [False]
    assert out["state"] == "listening"


def test_a_typed_turn_does_not_start_the_strip_handoff() -> None:
    # Only a heard transcript is the voice loop's turn.  Busy edges from a
    # typed message must not warm the strip.
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
const before = vizLog.pending.length;
setBusy(true);
const started = vizLog.pending.slice(before).includes(true);
setBusy(false);
console.log(JSON.stringify({ started, state: voiceLoop.state }));
"""
    )
    assert out["started"] is False
    assert out["state"] == "listening"


def test_turning_voice_mode_off_clears_the_pending_handoff() -> None:
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
composer.value = "hello";
mic.opts.onTranscript("hello");
const during = vizLog.pending.slice(-1);
voiceBtn.click();                          // voice mode off
console.log(JSON.stringify({ during, after: vizLog.pending.slice(-1), state: voiceLoop.state }));
"""
    )
    assert out["during"] == [True]
    assert out["after"] == [False]
    assert out["state"] == "off"


def test_leaving_the_pane_disables_the_loop_and_closes_the_mic() -> None:
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
const before = { mode: voiceModeOn, state: voiceLoop.state };
destroy();
console.log(JSON.stringify({
  before, mode: voiceModeOn, state: voiceLoop.state, stopped: mic.stopped,
  micDestroyed: !!mic.destroyed, detached: voiceBtn.parentNode === null,
}));
"""
    )
    assert out["before"] == {"mode": True, "state": "listening"}
    assert out["mode"] is False and out["state"] == "off"
    assert out["stopped"] >= 1
    assert out["micDestroyed"] is True and out["detached"] is True


# ---------------------------------------------------------------------------
# Source pins — wiring whose surroundings are not worth faking
# ---------------------------------------------------------------------------


def _body(sig: str) -> str:
    assert sig in _COORD, f"{sig!r} not found in coordinator.js"
    return strip_js_comments(extract_braced(_COORD, sig))


def test_the_mic_receives_both_hooks_and_routes_them_to_the_loop() -> None:
    decl = _decl(r"const mic = createMicController\(\{[\s\S]*?\}\);", "")
    assert "onTranscript:" in decl and "onState:" in decl
    assert "voiceLoop.onTranscript(text)" in strip_js_comments(decl)
    assert "voiceLoop.onMicState(state)" in strip_js_comments(decl)


def test_the_stream_run_is_started_only_in_voice_mode() -> None:
    feed = _body("  function _feedTtsStream(text) {")
    assert "voiceModeOn" in feed
    assert "ttsPlayer.stream({ voice: true })" in feed
    # Every stream run is watched for its done promise and tells the loop the
    # reply's speech has begun.
    assert "_watchVoiceRun(" in feed
    assert "onReplyStarted()" in feed


def test_destroy_disables_voice_mode_before_releasing_the_mic() -> None:
    body = _body("  function destroy() {")
    assert body.index("voiceLoop.disable()") < body.index("mic.destroy()")
    assert "voiceModeOn = false" in body


def test_the_setting_comes_from_the_stt_config_route() -> None:
    body = _body("  function _readVoiceModeEnabled() {")
    assert '"/v1/api/stt/config"' in body
    assert "voice_mode_enabled" in body


def test_the_persona_comes_from_the_workstream_list() -> None:
    body = _body("  function _readVoicePersona() {")
    assert '"/v1/api/workstreams"' in body
    assert 'persona === "voice"' in body


def test_a_turn_running_at_open_keeps_the_mic_shut_until_it_settles() -> None:
    """The pane reports "busy" to the loop, so a session opened mid-turn does
    not call mic.start() until the coordinator goes idle."""
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
busy = true;                                   // another tab's turn is running
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
const opened = { state: voiceLoop.state, started: mic.started, mode: voiceModeOn };
setBusy(false);                                // the idle edge fulfils the entry
const afterIdle = mic.started;
setBusy(false);
console.log(JSON.stringify({ opened, afterIdle, afterRepeat: mic.started }));
"""
    )
    assert out["opened"] == {"state": "listening", "started": 0, "mode": True}
    assert out["afterIdle"] == 1
    assert out["afterRepeat"] == 1


def test_an_empty_reply_ends_in_listening_instead_of_looping() -> None:
    """A reply with no speakable text (empty, or only a code block) produces a
    stream run with no audio.  The fallback is spent once, then the turn ends:
    it must not bounce between the stream path and the settle edge."""
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
const el = new El("div"); el.className = "msg assistant";
const body = new El("div"); body.className = "msg-body"; el.appendChild(body);
const pre = new El("pre"); body.appendChild(pre);          // code only: nothing to say
messagesEl.appendChild(el);
currentAssistantEl = el;
composer.value = "show me the diff";
mic.opts.onTranscript("show me the diff");
_feedTtsStream("```diff\\n-a\\n+b\\n```");
ttsLog.streams[0].resolve({ reason: "finished", heard: [] });
await tick();
finishAssistantStream();                                   // settle: the fallback runs, finds nothing
await tick();
finishAssistantStream();                                   // a later render must not re-arm it
await tick();
setBusy(false);
await tick();
console.log(JSON.stringify({
  plays: ttsLog.plays.length, state: voiceLoop.state,
  starts: mic.started, mode: voiceModeOn,
}));
"""
    )
    assert out["plays"] == 0
    assert out["state"] == "listening"
    assert out["starts"] == 2  # enable + the turn end
    assert out["mode"] is True


def test_rendering_history_with_voice_mode_on_does_not_speak_old_replies() -> None:
    """_refreshRetryButton runs on every history paint; with the mode on it
    must only speak the reply a voice turn is actually waiting on."""
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
// A replayed transcript: three finished replies, each with a speaker.
for (const text of ["one", "two", "three"]) {
  const el = new El("div"); el.className = "msg assistant";
  const body = new El("div"); body.className = "msg-body"; el.appendChild(body);
  const btn = new El("button"); btn.className = "msg-tts-btn"; el.appendChild(btn);
  messagesEl.appendChild(el);
}
_refreshRetryButton();                     // a history render / settle edge
_refreshRetryButton();
await tick();
console.log(JSON.stringify({ plays: ttsLog.plays.length, state: voiceLoop.state }));
"""
    )
    assert out == {"plays": 0, "state": "listening"}


def test_a_pending_reply_reference_is_not_replayed_by_a_history_render() -> None:
    """Once the fallback has spoken (or found nothing), the reference is gone:
    re-rendering history afterwards must not speak it again."""
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
const el = new El("div"); el.className = "msg assistant";
const body = new El("div"); body.className = "msg-body";
body.appendChild(new Text_("the reply")); el.appendChild(body);
messagesEl.appendChild(el);
currentAssistantEl = el;                        // the bubble the reply streams into
composer.value = "hello";
mic.opts.onTranscript("hello");
_feedTtsStream("the reply ");
ttsLog.streams[0].resolve({ reason: "finished", heard: [] });
await tick();
finishAssistantStream();
await tick();
const first = ttsLog.plays.length;
ttsLog.plays[0].resolve({ reason: "finished", heard: ["the reply"] });
await tick();
setBusy(false);
_refreshRetryButton();                     // a later history render
await tick();
console.log(JSON.stringify({ first, afterRender: ttsLog.plays.length, awaiting: voiceAwaitingReply }));
"""
    )
    assert out["first"] == 1
    assert out["afterRender"] == 1
    assert out["awaiting"] is None


# --- mic mute (the mic button inside voice mode) -----------------------------


def _voice_on() -> str:
    return """
personaRows = [{ ws_id: "coord-1", persona: "voice" }];
_applyVoiceRoles({ stt: true, tts: true });
""" + _settle()


def test_the_mic_button_mutes_and_unmutes_without_leaving_voice_mode() -> None:
    out = _run(
        _voice_on()
        + """
const claimed = mic.opts.onToggle();
const muted = { on: voiceModeOn, muted: voiceLoop.muted, stopped: mic.stopped,
  discard: mic.lastDiscard, status: voiceStatusEl.attrs["data-state"],
  label: voiceStatusEl.textContent, cls: mic.button.classList.contains("is-muted"),
  pressed: mic.button.attrs["aria-pressed"] };
const started = mic.started;
mic.opts.onToggle();
console.log(JSON.stringify({ claimed, muted, reopened: mic.started - started,
  after: [voiceLoop.muted, voiceStatusEl.attrs["data-state"], mic.button.classList.contains("is-muted")] }));
"""
    )
    assert out["claimed"] is True
    assert out["muted"]["on"] is True and out["muted"]["muted"] is True
    assert out["muted"]["stopped"] == 1 and out["muted"]["discard"] is True
    assert out["muted"]["status"] == "muted" and "Mic off" in out["muted"]["label"]
    assert out["muted"]["cls"] is True and out["muted"]["pressed"] == "true"
    assert out["reopened"] == 1
    assert out["after"] == [False, "listening", False]


def test_a_muted_session_still_speaks_a_reply_and_keeps_the_mic_shut() -> None:
    out = _run(
        _voice_on()
        + """
mic.opts.onToggle();                    // mute
const before = mic.started;
setBusy(true);                          // a worker report arrives as a turn
_feedTtsStream("Job finished.");
const r = ttsLog.streams[0];
_endTtsStream();
r.resolve({ reason: "finished", heard: ["Job finished."] });
await tick();
setBusy(false);
await tick();
console.log(JSON.stringify({ streamed: ttsLog.streams.length, pushed: r.pushed.length,
  reopened: mic.started - before, state: voiceLoop.state, status: voiceStatusEl.attrs["data-state"] }));
"""
    )
    assert out["streamed"] == 1 and out["pushed"] >= 1
    assert out["reopened"] == 0
    assert out["state"] == "listening" and out["status"] == "muted"


def test_without_auto_stop_the_mic_button_keeps_ending_the_turn() -> None:
    """With auto-stop off the mic press is the only way to finish a spoken
    turn, so voice mode must not take it over."""
    out = _run(
        "autoStop = false;\n"
        + _voice_on()
        + """
console.log(JSON.stringify({ claimed: mic.opts.onToggle(), muted: voiceLoop.muted }));
"""
    )
    assert out["claimed"] is False
    assert out["muted"] is False


def test_outside_voice_mode_the_mic_button_dictates_as_before() -> None:
    out = _run(
        """
personaRows = [{ ws_id: "coord-1", persona: "coordinator" }];
_applyVoiceRoles({ stt: true, tts: true });
"""
        + _settle()
        + """
console.log(JSON.stringify({ claimed: mic.opts.onToggle() }));
"""
    )
    assert out["claimed"] is False


def test_the_mute_switch_stays_usable_while_a_turn_runs() -> None:
    out = _run(
        _voice_on()
        + """
mic.button.disabled = false;
mic.syncBusy = function () { this.button.disabled = busy; };
setBusy(true);
const during = mic.button.disabled;
console.log(JSON.stringify({ during }));
"""
    )
    assert out["during"] is False


def test_turning_voice_mode_off_clears_the_mute_paint() -> None:
    out = _run(
        _voice_on()
        + """
mic.opts.onToggle();
voiceBtn.click();                       // voice mode off
console.log(JSON.stringify({ cls: mic.button.classList.contains("is-muted"),
  pressed: mic.button.attrs["aria-pressed"], on: voiceModeOn }));
"""
    )
    assert out["on"] is False
    assert out["cls"] is False and out["pressed"] == "false"
