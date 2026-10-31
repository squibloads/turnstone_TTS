"""The voice loop turns its own mic off after a quiet minute (voice_loop.js).

A voice session left open is a live microphone: a conversation with someone
else in the room ("yeah, go ahead and delete that") would be transcribed and
sent as an instruction.  So while the mic is actually open and listening, the
loop counts down ``idleMuteMs``; detected speech restarts the count, and the
time it spends thinking or speaking does not count at all.  When the count
runs out the loop mutes itself through the same switch the user's mic button
uses, so replies and reports still play.

The timer is injected (``setTimer`` / ``clearTimer``), so these tests fire it
by hand instead of waiting a minute.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import demodulize, node_skip, run_node_source

pytestmark = node_skip

_MODULE = demodulize(
    Path(__file__).resolve().parents[1] / "turnstone/console/static/coordinator/voice_loop.js"
)

# `timers` holds every armed timer; `live()` the ones not yet cleared or fired.
_PRELUDE = """
const calls = [];
const timers = [];
// Like the pane's mic: it refuses to start while a turn is running.
let micRefuses = false;
const live = () => timers.filter((t) => !t.cleared && !t.fired);
const fire = () => { const t = live()[0]; t.fired = true; t.fn(); };
const makeLoop = (idleMuteMs) => createVoiceLoop({
  startListening: () => { calls.push("start"); return !micRefuses; },
  stopListening: () => calls.push("stop"),
  send: (text) => calls.push(["send", text]),
  setStatus: (state) => calls.push(["status", state]),
  onIdleMute: () => calls.push("idle-mute"),
  idleMuteMs,
  setTimer: (fn, ms) => { const t = { fn, ms, cleared: false, fired: false }; timers.push(t); return t; },
  clearTimer: (t) => { if (t) t.cleared = true; },
});
"""


def _run(body: str) -> Any:
    src = (
        _MODULE
        + _PRELUDE
        + "\n(async () => {\n"
        + body
        + "\n})().catch((e) => { console.error(e); process.exit(1); });"
    )
    r = run_node_source(src)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_a_quiet_minute_mutes_the_mic_and_keeps_voice_mode() -> None:
    out = _run(
        """
const loop = makeLoop(60000);
loop.enable();
const armed = live().map((t) => t.ms);
fire();
console.log(JSON.stringify({ armed, muted: loop.muted, state: loop.state,
  tail: calls.slice(-3), live: live().length }));
"""
    )
    assert out["armed"] == [60000]
    assert out["muted"] is True
    assert out["state"] == "listening"  # still in voice mode, reporting muted
    assert out["tail"] == ["stop", ["status", "muted"], "idle-mute"]
    assert out["live"] == 0


def test_speech_restarts_the_count() -> None:
    out = _run(
        """
const loop = makeLoop(60000);
loop.enable();
const first = live()[0];
loop.onMicState("transcribing");          // speech detected: the clip is uploading
const duringClip = live().length;
loop.onMicState("idle");                  // discarded clip: back to listening
console.log(JSON.stringify({ firstCleared: first.cleared, duringClip,
  rearmed: live().length, muted: loop.muted }));
"""
    )
    assert out == {"firstCleared": True, "duringClip": 0, "rearmed": 1, "muted": False}


def test_thinking_and_speaking_do_not_count() -> None:
    out = _run(
        """
const loop = makeLoop(60000);
loop.enable();
loop.onTranscript("what is the status");  // -> thinking: mic closed
const thinking = live().length;
loop.onBusy(true);
loop.onReplyStarted();
const speaking = live().length;
loop.onBusy(false);
loop.onPlaybackDone({ reason: "done", heard: [] });  // back to listening
console.log(JSON.stringify({ thinking, speaking, after: live().length,
  state: loop.state, muted: loop.muted }));
"""
    )
    assert out == {
        "thinking": 0,
        "speaking": 0,
        "after": 1,
        "state": "listening",
        "muted": False,
    }


def test_a_mic_waiting_on_a_busy_turn_is_not_open_and_does_not_count() -> None:
    out = _run(
        """
const loop = makeLoop(60000);
micRefuses = true;
loop.onBusy(true);           // a turn is running when voice mode starts
loop.enable();               // listening, but the mic refuses: start deferred
const deferred = live().length;
micRefuses = false;
loop.onBusy(false);          // idle edge opens the mic
console.log(JSON.stringify({ deferred, opened: live().length }));
"""
    )
    assert out == {"deferred": 0, "opened": 1}


def test_muting_or_turning_off_cancels_the_count_and_unmuting_restarts_it() -> None:
    out = _run(
        """
const loop = makeLoop(60000);
loop.enable();
loop.setMuted(true);
const muted = live().length;
loop.setMuted(false);
const unmuted = live().length;
loop.disable();
console.log(JSON.stringify({ muted, unmuted, off: live().length,
  idleMutes: calls.filter((c) => c === "idle-mute").length }));
"""
    )
    assert out == {"muted": 0, "unmuted": 1, "off": 0, "idleMutes": 0}


def test_zero_turns_the_idle_mute_off() -> None:
    out = _run(
        """
const loop = makeLoop(0);
loop.enable();
console.log(JSON.stringify({ armed: timers.length }));
"""
    )
    assert out == {"armed": 0}


def test_a_late_timer_after_the_mic_closed_does_nothing() -> None:
    # A clear can race a timer that was already queued to run.
    out = _run(
        """
const loop = makeLoop(60000);
loop.enable();
const t = live()[0];
loop.onTranscript("hello");   // mic closed, timer cleared...
t.fn();                       // ...but its callback runs anyway
console.log(JSON.stringify({ muted: loop.muted, state: loop.state,
  idleMutes: calls.filter((c) => c === "idle-mute").length }));
"""
    )
    assert out == {"muted": False, "state": "thinking", "idleMutes": 0}
