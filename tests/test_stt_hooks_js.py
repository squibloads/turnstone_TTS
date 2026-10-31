"""The dictation mic's hands-free hooks (``shared_static/stt_mic.js``).

A hands-free pane needs two things from the mic that a click-to-dictate pane
does not: the text one clip added, once its upload has finished, and the
controller's state (idle / recording / transcribing) as it changes.  These pin
both hooks against the shipped controller, including the edges a voice turn
must not act on — a discarded clip, a failed upload, an empty transcript — and
the ordering the page depends on: the transcript is handed over while the
controller still reports ``transcribing``, before it settles back to ``idle``.

The controller without hooks is unchanged; that is the territory of
``test_stt_mic_js``, which this suite runs beside.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import demodulize, node_skip, run_node_source
from tests._stt_js_stubs import MIC_DOM, MIC_STUBS
from tests._tts_js_stubs import TREE_DOM

pytestmark = node_skip

_STATIC = Path(__file__).resolve().parents[1] / "turnstone"
_MODULE = demodulize(_STATIC / "shared_static/stt_mic.js")
_PRELUDE = "\n".join((TREE_DOM, MIC_DOM, MIC_STUBS, _MODULE))


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


def test_on_transcript_gets_only_the_clip_text_once_the_stream_finishes() -> None:
    out = _run(
        """
fake.chunks = ["hello ", "wor", "ld  "];
const said = [];
const composer = makeComposer("typed earlier  ");
const perDelta = [];                    // said.length at each composer insertion
composer.inputEl.addEventListener("input", () => perDelta.push(said.length));
const m = createMicController({
  composer, base: "", wsId: "w",
  onTranscript: (text) => said.push(text),
});
m.setAvailable(true);
m.button.click(); await tick();
const during = said.slice();            // nothing before the clip is uploaded
m.button.click(); await tick();
console.log(JSON.stringify({
  during, said, perDelta, value: composer.inputEl.value,
  fetches: log.fetches.length, inputs: composer.inputEl.inputs,
}));
"""
    )
    assert out["during"] == []
    # One call, the whole clip joined and trimmed — not the typed prefix, and
    # not the trailing whitespace the composer insertion keeps.
    assert out["said"] == ["hello world"]
    # ... and only after the last delta: no insertion saw a transcript yet.
    assert out["perDelta"] == [0, 0, 0]
    assert out["value"] == "typed earlier hello world  "
    assert out["fetches"] == 1
    assert out["inputs"] == 3  # insertion is per delta, exactly as before


def test_no_transcript_hook_for_a_discarded_failed_or_empty_clip() -> None:
    out = _run(
        """
const said = [];
const mk = (extra) => createMicController(Object.assign(
  { composer: makeComposer(), base: "", wsId: "w", onTranscript: (t) => said.push(t) },
  extra || {},
));
// stop(true) abandons the clip
const d = mk(); d.button.click(); await tick(); d.stop(true); await tick();
// the busy edge abandons it the same way
let busy = false;
const b = mk({ isBusy: () => busy }); b.button.click(); await tick();
busy = true; b.syncBusy(); await tick();
// a server refusal leaves no transcript to hand over
fake.stt = "error";
const f = mk(); f.button.click(); await tick(); f.button.click(); await tick();
// so does a stream with no deltas at all ...
fake.stt = "empty";
const e = mk(); e.button.click(); await tick(); e.button.click(); await tick();
// ... and one whose only deltas are whitespace
fake.stt = "stream"; fake.chunks = ["  ", "\\n"];
const wComposer = makeComposer();
const w = mk({ composer: wComposer });
w.button.click(); await tick(); w.button.click(); await tick();
console.log(JSON.stringify({
  said, fetches: log.fetches.length, toasts: log.toasts,
  value: wComposer.inputEl.value,
}));
"""
    )
    assert out["said"] == []
    assert out["fetches"] == 3  # failure, empty and whitespace clips still upload
    assert out["toasts"] == [
        ["STT is not configured", "error"],
        ["No speech detected", "error"],
    ]
    assert out["value"] == "  \n"  # insertion itself is unchanged


def test_on_state_follows_a_kept_clip_from_recording_to_idle() -> None:
    out = _run(
        """
const states = [];
const events = [];
const m = createMicController({
  composer: makeComposer(), base: "", wsId: "w",
  onState: (state) => { states.push(state); events.push(state); },
  onTranscript: (text) => events.push("transcript:" + text),
});
const before = states.slice();          // construction is not a change
m.start(); await tick();
const recording = states.slice();
m.stop(false); await tick();
console.log(JSON.stringify({ before, recording, states, events }));
"""
    )
    assert out["before"] == []
    assert out["recording"] == ["recording"]
    # The page gets the text before the controller settles: it is still
    # "transcribing" when onTranscript runs.
    assert out["states"] == ["recording", "transcribing", "idle"]
    assert out["events"] == ["recording", "transcribing", "transcript:hello world", "idle"]


def test_on_state_returns_to_idle_when_a_clip_is_discarded() -> None:
    out = _run(
        """
const states = [];
const m = createMicController({
  composer: makeComposer(), base: "", wsId: "w",
  onState: (state) => states.push(state),
});
m.start(); await tick();
m.stop(true); await tick();
console.log(JSON.stringify({ states, recording: m.isRecording() }));
"""
    )
    assert out["states"] == ["recording", "idle"]
    assert out["recording"] is False


def test_on_state_returns_to_idle_after_a_failed_upload() -> None:
    out = _run(
        """
fake.stt = "error";
const states = [];
const m = createMicController({
  composer: makeComposer(), base: "", wsId: "w",
  onState: (state) => states.push(state),
});
m.start(); await tick();
m.stop(false); await tick();
console.log(JSON.stringify({ states, recording: m.isRecording() }));
"""
    )
    assert out["states"] == ["recording", "transcribing", "idle"]
    assert out["recording"] is False


def test_on_state_returns_to_idle_when_a_teardown_aborts_the_upload() -> None:
    out = _run(
        """
fake.stt = "hang";
const states = [];
const m = createMicController({
  composer: makeComposer(), base: "", wsId: "w",
  onState: (state) => states.push(state),
});
m.start(); await tick();
m.stop(false); await tick();
const during = states.slice();
m.destroy(); await tick();
console.log(JSON.stringify({ during, states, recording: m.isRecording() }));
"""
    )
    assert out["during"] == ["recording", "transcribing"]
    assert out["states"] == ["recording", "transcribing", "idle"]
    assert out["recording"] is False


def test_start_records_and_is_a_noop_while_recording_busy_or_denied() -> None:
    out = _run(
        """
let busy = false;
const states = [];
const m = createMicController({
  composer: makeComposer(), base: "", wsId: "w",
  isBusy: () => busy,
  onState: (state) => states.push(state),
});
m.start(); await tick();
const first = [m.isRecording(), log.recorders.length, log.tracks.length];
m.start(); await tick();                // already recording: no second capture
const twice = [log.recorders.length, log.tracks.length];
m.stop(true); await tick();
busy = true;
m.start(); await tick();                // a busy pane cannot start
const whenBusy = [m.isRecording(), log.recorders.length];
busy = false;
fake.gum = "deny";
m.start(); await tick();                // the browser refuses the microphone
const denied = [m.isDenied(), log.toasts.length];
m.start(); await tick();                // ... and a block is not re-prompted
console.log(JSON.stringify({
  first, twice, whenBusy, denied, toasts: log.toasts,
  afterDenied: log.toasts.length, states,
}));
"""
    )
    assert out["first"] == [True, 1, 1]
    assert out["twice"] == [1, 1]
    assert out["whenBusy"] == [False, 1]
    assert out["denied"] == [True, 1]
    assert out["toasts"] == [["Microphone access was denied", "error"]]
    assert out["afterDenied"] == 1
    assert out["states"] == ["recording", "idle"]
