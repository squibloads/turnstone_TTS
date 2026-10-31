"""The hands-free voice loop (``console/static/coordinator/voice_loop.js``).

The module is DOM-free by design, so these tests run the shipped source
directly under node (imports stripped) with recording callbacks: every state
transition, every mic start/stop, and the order of ``setStatus`` calls.  The
races that matter are the two completion orders — playback done before the
coordinator goes idle and the reverse — plus the turns that never speak.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import demodulize, node_skip, run_node_source

pytestmark = node_skip

_MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "turnstone/console/static/coordinator/voice_loop.js"
)
_MODULE = demodulize(_MODULE_PATH)

# `calls` is the ordered log every callback appends to, so a test can assert
# both what happened and what happened first.  `loop` is rebuilt per test by
# makeLoop(); the harness forwards each option to a recorder.
_PRELUDE = """
const calls = [];
const loop = createVoiceLoop({
  startListening: () => calls.push("start"),
  stopListening: () => calls.push("stop"),
  send: (text) => calls.push(["send", text]),
  setStatus: (state) => calls.push(["status", state]),
});
const state = () => loop.state;
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


def test_enable_starts_listening_and_disable_stops_everything() -> None:
    out = _run(
        """
loop.enable();
const on = state();
loop.disable();
const off = state();
loop.onBusy(true);            // nothing fires after a disable
loop.onTranscript("late");
console.log(JSON.stringify({ on, off, calls, after: state() }));
"""
    )
    assert out["on"] == "listening" and out["off"] == "off"
    assert out["calls"] == [
        ["status", "listening"],
        "start",
        "stop",
        ["status", "off"],
    ]
    assert out["after"] == "off"


def test_enable_is_idempotent_and_never_starts_a_second_recording() -> None:
    out = _run(
        """
loop.enable();
loop.enable();
loop.disable();
loop.disable();
console.log(JSON.stringify({ calls }));
"""
    )
    assert out["calls"] == [["status", "listening"], "start", "stop", ["status", "off"]]


def test_a_transcript_is_sent_once_and_moves_to_thinking() -> None:
    out = _run(
        """
loop.enable();
const micAllowed = loop.state === "listening";
loop.onTranscript("  what needs my attention?  ");
const sent = state();
loop.onTranscript("a second clip");     // ignored: the turn already started
console.log(JSON.stringify({ micAllowed, sent, calls, after: state() }));
"""
    )
    assert out["micAllowed"] is True
    assert out["sent"] == "thinking"
    assert out["after"] == "thinking"
    assert ["send", "what needs my attention?"] in out["calls"]
    assert ["send", "a second clip"] not in out["calls"]
    assert out["calls"].count(["status", "thinking"]) == 1


def test_an_empty_transcript_reopens_the_mic_instead_of_sending() -> None:
    out = _run(
        """
loop.enable();
loop.onTranscript("   ");
console.log(JSON.stringify({ calls, state: state() }));
"""
    )
    assert out["calls"] == [["status", "listening"], "start", ["status", "listening"], "start"]
    assert out["state"] == "listening"
    assert not [c for c in out["calls"] if isinstance(c, list) and c[0] == "send"]


def test_the_reply_speaking_then_finishing_returns_to_listening() -> None:
    out = _run(
        """
loop.enable();
loop.onTranscript("hello");
loop.onBusy(true);
loop.onReplyStarted();
const speaking = state();
loop.onPlaybackDone({ reason: "finished", heard: ["hello"] });
const whileBusy = state();               // busy is still on: wait for it
loop.onBusy(false);
console.log(JSON.stringify({ speaking, whileBusy, state: state(), calls }));
"""
    )
    assert out["speaking"] == "speaking"
    assert out["whileBusy"] == "speaking"
    assert out["state"] == "listening"
    # The mic was opened exactly twice: the initial enable and the turn end.
    assert out["calls"].count("start") == 2
    assert out["calls"].count("stop") == 1


def test_idle_before_done_keeps_the_mic_closed_until_the_audio_ends() -> None:
    out = _run(
        """
loop.enable();
loop.onTranscript("hello");
loop.onBusy(true);
loop.onReplyStarted();
loop.onBusy(false);                     // server turn is over...
const before = state();                 // ...but the reply is still speaking
loop.onPlaybackDone({ reason: "finished", heard: ["hello"] });
const after = state();
console.log(JSON.stringify({ before, after, starts: calls.filter((c) => c === "start").length }));
"""
    )
    assert out == {"before": "speaking", "after": "listening", "starts": 2}


def test_done_before_idle_keeps_the_mic_closed_until_the_turn_settles() -> None:
    out = _run(
        """
loop.enable();
loop.onTranscript("hello");
loop.onBusy(true);
loop.onReplyStarted();
loop.onPlaybackDone({ reason: "finished", heard: ["hello"] });
const before = state();
loop.onBusy(false);
const after = state();
console.log(JSON.stringify({ before, after, starts: calls.filter((c) => c === "start").length }));
"""
    )
    assert out == {"before": "speaking", "after": "listening", "starts": 2}


def test_a_reply_with_nothing_spoken_returns_to_listening() -> None:
    out = _run(
        """
loop.enable();
loop.onTranscript("hello");
loop.onBusy(true);
loop.onReplyStarted();
loop.onPlaybackDone({ reason: "finished", heard: [] });
loop.onBusy(false);
console.log(JSON.stringify({ state: state() }));
"""
    )
    assert out["state"] == "listening"


def test_a_turn_that_never_speaks_returns_to_listening_on_the_idle_edge() -> None:
    """A tool-only reply starts no run; no done will ever arrive."""
    out = _run(
        """
loop.enable();
loop.onTranscript("run the tests");
loop.onBusy(true);
loop.onBusy(false);
console.log(JSON.stringify({ state: state(), starts: calls.filter((c) => c === "start").length }));
"""
    )
    assert out == {"state": "listening", "starts": 2}


def test_a_cancelled_playback_or_an_error_returns_to_listening_not_off() -> None:
    for reason in ("cancelled", "error"):
        out = _run(
            f"""
loop.enable();
loop.onTranscript("hello");
loop.onBusy(true);
loop.onReplyStarted();
loop.onPlaybackDone({{ reason: "{reason}", heard: [] }});
loop.onBusy(false);
console.log(JSON.stringify({{ state: state() }}));
"""
        )
        assert out["state"] == "listening", reason


def test_a_busy_edge_while_transcribing_abandons_the_clip() -> None:
    """Another tab's turn starting mid-upload must not let the transcript
    queue itself behind that turn."""
    out = _run(
        """
loop.enable();
loop.onMicState("transcribing");
loop.onBusy(true);
const during = state();
loop.onTranscript("a stale clip");      // ignored: the turn is no longer ours
console.log(JSON.stringify({ during, state: state(), sends: calls.filter((c) => Array.isArray(c) && c[0] === "send") }));
"""
    )
    assert out == {"during": "thinking", "state": "thinking", "sends": []}


def test_a_transcript_while_speaking_is_ignored_and_never_queued() -> None:
    out = _run(
        """
loop.enable();
loop.onTranscript("first");
loop.onBusy(true);
loop.onReplyStarted();
loop.onTranscript("a late clip");       // the mic should not be open; drop it
loop.onPlaybackDone({ reason: "finished", heard: ["first"] });
loop.onBusy(false);
console.log(JSON.stringify({ state: state(), sends: calls.filter((c) => Array.isArray(c) && c[0] === "send") }));
"""
    )
    assert out["state"] == "listening"
    assert out["sends"] == [["send", "first"]]


def test_disable_mid_speak_stops_the_mic_and_ignores_the_late_done() -> None:
    out = _run(
        """
loop.enable();
loop.onTranscript("hello");
loop.onBusy(true);
loop.onReplyStarted();
loop.disable();
loop.onPlaybackDone({ reason: "finished", heard: ["hello"] });
loop.onBusy(false);
console.log(JSON.stringify({ state: state(), calls }));
"""
    )
    assert out["state"] == "off"
    # disable stops the mic; the late done/busy callbacks add nothing.
    assert out["calls"] == [
        ["status", "listening"],
        "start",
        "stop",  # the clip stops before the turn moves on
        ["status", "thinking"],
        ["send", "hello"],
        ["status", "speaking"],
        "stop",  # disable releases anything still live
        ["status", "off"],
    ]


def test_a_typed_turn_while_listening_closes_the_mic_and_reopens_after() -> None:
    """A turn this loop did not start must still keep the mic shut while it
    runs — that is the half-duplex guarantee — and give it back after."""
    out = _run(
        """
loop.enable();
loop.onBusy(true);                      // send button pressed, not the mic
const during = state();
loop.onBusy(false);
console.log(JSON.stringify({ during, state: state(), starts: calls.filter((c) => c === "start").length }));
"""
    )
    assert out == {"during": "thinking", "state": "listening", "starts": 2}


def test_the_mic_transcribing_state_is_reported_and_recovers_on_a_discard() -> None:
    out = _run(
        """
loop.enable();
loop.onMicState("transcribing");
const transcribing = state();
loop.onMicState("idle");                 // discarded / failed clip: no transcript
console.log(JSON.stringify({ transcribing, state: state(), calls }));
"""
    )
    assert out["transcribing"] == "transcribing"
    assert out["state"] == "listening"
    assert ["status", "transcribing"] in out["calls"]


def test_a_successful_transcript_wins_over_the_trailing_mic_idle() -> None:
    out = _run(
        """
loop.enable();
loop.onMicState("recording");
loop.onMicState("transcribing");
loop.onTranscript("hello");
loop.onMicState("idle");                 // the mic always reports idle after upload
console.log(JSON.stringify({ state: state() }));
"""
    )
    assert out["state"] == "thinking"


def test_every_transition_tells_the_status_line() -> None:
    out = _run(
        """
loop.enable();
loop.onTranscript("hello");
loop.onBusy(true);
loop.onReplyStarted();
loop.onPlaybackDone({ reason: "finished", heard: ["hello"] });
loop.onBusy(false);
loop.disable();
console.log(JSON.stringify(calls.filter((c) => Array.isArray(c) && c[0] === "status").map((c) => c[1])));
"""
    )
    assert out == [
        "listening",
        "thinking",
        "speaking",
        "listening",
        "off",
    ]


def test_a_listening_entry_while_busy_defers_the_mic_to_the_idle_edge() -> None:
    """A session reopened mid-turn lands in listening with a closed mic; the
    idle edge opens it, exactly once."""
    out = _run(
        """
let refuse = true;                      // the coordinator is mid-turn
const deferred = createVoiceLoop({
  startListening: () => (refuse ? false : calls.push("start")),
  stopListening: () => calls.push("stop"),
  send: (text) => calls.push(["send", text]),
  setStatus: (state) => calls.push(["status", state]),
});
deferred.enable();                      // listens, but the mic refuses
const opened = calls.filter((c) => c === "start").length;
deferred.onBusy(true);
refuse = false;
deferred.onBusy(false);                 // the idle edge fulfils the entry
const afterIdle = calls.filter((c) => c === "start").length;
deferred.onBusy(false);                 // no busy edge, no second start
const afterRepeat = calls.filter((c) => c === "start").length;
console.log(JSON.stringify({ opened, afterIdle, afterRepeat, state: deferred.state }));
"""
    )
    assert out == {"opened": 0, "afterIdle": 1, "afterRepeat": 1, "state": "listening"}
