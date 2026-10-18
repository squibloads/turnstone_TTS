"""The dictation mic module (``shared_static/stt_mic.js``).

The shipped ``createMicController`` is evaluated in node against the fakes in
``_stt_js_stubs`` (a tree DOM plus controllable ``getUserMedia`` /
``MediaRecorder`` / transcription route), so these cover the recorder state
machine, the upload and the transcript streaming for every pane that mounts it.
How a pane wires the module is covered where the pane is
(``test_stt_coordinator_js``; the interactive pane in ``test_interactive_pane_js``).
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
    src = _PRELUDE + f"\n(async () => {{\n{body}\n}})().catch((e) => {{ console.error(e); process.exit(1); }});"
    r = run_node_source(src)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_button_joins_the_action_row_before_send_and_stays_hidden_until_available() -> None:
    out = _run(
        """
const composer = makeComposer();
const m = createMicController({ composer, base: "", wsId: "w" });
const row = composer.actionsRowEl;
const hidden = m.button.style.display;
m.setAvailable(true);
const shown = [m.button.style.display, row.classList.contains("has-mic")];
m.setAvailable(false);
console.log(JSON.stringify({
  order: row.children.map((c) => c.className.split(" ")[0]), hidden, shown,
  off: [m.button.style.display, row.classList.contains("has-mic")],
  label: m.button.attrs["aria-label"], pressed: m.button.attrs["aria-pressed"],
}));
"""
    )
    assert out["order"] == ["composer-mic-btn", "composer-send"]
    assert out["hidden"] == "none"
    assert out["shown"] == ["", True]
    assert out["off"] == ["none", False]
    assert out["label"] == "Record speech to text" and out["pressed"] == "false"


def test_a_composer_without_an_action_row_gets_no_button() -> None:
    out = _run(
        """
const m = createMicController({ composer: { inputEl: null }, base: "", wsId: "w" });
m.setAvailable(true); m.syncBusy(); m.stop(true); m.destroy();
console.log(JSON.stringify({ button: m.button }));
"""
    )
    assert out == {"button": None}


def test_record_then_stop_uploads_and_streams_the_transcript_into_the_composer() -> None:
    out = _run(
        """
const composer = makeComposer("typed earlier  ");
const m = createMicController({ composer, base: "/node/n1", wsId: "ws 1", announce: (s) => log.announce.push(s) });
m.setAvailable(true);
m.button.click(); await tick();
const rec = { on: m.isRecording(), pressed: m.button.attrs["aria-pressed"], label: m.button.attrs["aria-label"],
  icon: m.button.children[0].className, mime: log.recorders[0].requested };
m.button.click(); await tick();
const f = log.fetches[0];
console.log(JSON.stringify({
  rec, url: f.url, method: f.method, file: f.body.get("audio").name,
  value: composer.inputEl.value, inputs: composer.inputEl.inputs, focused: composer.inputEl.focused,
  after: [m.isRecording(), m.button.disabled, m.button.classList.contains("is-busy")],
  tracksStopped: log.tracks.map((t) => t.stopped), announce: log.announce, toasts: log.toasts,
}));
"""
    )
    assert out["rec"] == {
        "on": True,
        "pressed": "true",
        "label": "Stop recording and transcribe",
        "icon": "composer-mic-icon icon-stop",
        "mime": {"mimeType": "audio/webm;codecs=opus"},
    }
    assert out["url"] == "/node/n1/v1/api/workstreams/ws%201/speech-to-text/stream"
    assert out["method"] == "POST" and out["file"] == "speech.webm"
    # Text typed while transcribing is kept, the first delta is joined with a
    # single space, later deltas append, and each one wakes the composer's
    # auto-resize / send-enable listeners.
    assert out["value"] == "typed earlier hello world"
    assert out["inputs"] == 2 and out["focused"] is True
    assert out["after"] == [False, False, False]
    assert out["tracksStopped"] == [True]
    assert out["announce"] == [
        "Recording. Activate the microphone button again to stop.",
        "Transcribing…",
        "Transcript added to message.",
    ]
    assert out["toasts"] == []


def test_the_clip_type_picks_the_upload_extension() -> None:
    out = _run(
        """
fake.supported = ["audio/ogg;codecs=opus"];
const composer = makeComposer();
const m = createMicController({ composer, base: "", wsId: "w" });
m.button.click(); await tick();
const requested = log.recorders[0].requested;
log.recorders[0].mimeType = "audio/ogg;codecs=opus";
m.button.click(); await tick();
console.log(JSON.stringify({ requested, file: log.fetches[0].body.get("audio").name }));
"""
    )
    assert out == {"requested": {"mimeType": "audio/ogg;codecs=opus"}, "file": "speech.ogg"}


def test_permission_denial_disables_the_button_with_guidance() -> None:
    out = _run(
        """
fake.gum = "deny";
const composer = makeComposer();
const m = createMicController({ composer, base: "", wsId: "w" });
m.setAvailable(true);
m.button.click(); await tick();
const msg = "Microphone blocked — enable it in your browser's site settings";
const denied = [m.button.disabled, m.button.title, m.button.attrs["aria-label"], m.isDenied(), m.isRecording()];
m.syncBusy();                                    // an idle edge must not re-enable a blocked mic
console.log(JSON.stringify({ denied, msg, still: m.button.disabled, toasts: log.toasts, fetches: log.fetches.length }));
"""
    )
    assert out["denied"] == [True, out["msg"], out["msg"], True, False]
    assert out["still"] is True
    assert out["toasts"] == [["Microphone access was denied", "error"]]
    assert out["fetches"] == 0


def test_an_unsupported_browser_says_so_and_does_not_record() -> None:
    out = _run(
        """
Object.defineProperty(globalThis, "navigator", { configurable: true, value: {} });
const composer = makeComposer();
const m = createMicController({ composer, base: "", wsId: "w" });
m.button.click(); await tick();
console.log(JSON.stringify({ toasts: log.toasts, on: m.isRecording(), recorders: log.recorders.length }));
"""
    )
    assert out == {
        "toasts": [["Microphone capture is not supported in this browser", "error"]],
        "on": False,
        "recorders": 0,
    }


def test_a_busy_pane_cannot_start_and_going_busy_abandons_the_recording() -> None:
    out = _run(
        """
let busy = true;
const composer = makeComposer();
const m = createMicController({ composer, base: "", wsId: "w", isBusy: () => busy });
m.button.click(); await tick();
const blocked = [m.isRecording(), log.recorders.length];
busy = false; m.syncBusy();
const idle = m.button.disabled;
m.button.click(); await tick();
const recording = m.isRecording();
busy = true; m.syncBusy(); await tick();         // the turn started while recording
console.log(JSON.stringify({ blocked, idle, recording, after: [m.isRecording(), m.button.disabled],
  tracksStopped: log.tracks.map((t) => t.stopped), fetches: log.fetches.length }));
"""
    )
    assert out["blocked"] == [False, 0]
    assert out["idle"] is False and out["recording"] is True
    assert out["after"] == [False, True]
    assert out["tracksStopped"] == [True]
    assert out["fetches"] == 0  # the clip was discarded, not transcribed


def test_stopping_while_the_permission_prompt_is_open_releases_the_late_stream() -> None:
    out = _run(
        """
fake.gum = "pending";
const composer = makeComposer();
const m = createMicController({ composer, base: "", wsId: "w" });
m.button.click(); await tick();
m.stop(true);                                    // reset / pane teardown while the prompt is up
fake.pendingGum(); await tick();                  // the user grants it afterwards
console.log(JSON.stringify({ on: m.isRecording(), recorders: log.recorders.length, tracksStopped: log.tracks.map((t) => t.stopped) }));
"""
    )
    assert out == {"on": False, "recorders": 0, "tracksStopped": [True]}


def test_the_recording_timer_labels_the_button_and_stops_with_the_clip() -> None:
    out = _run(
        """
const composer = makeComposer();
const m = createMicController({ composer, base: "", wsId: "w" });
m.button.click(); await tick();
const realNow = Date.now; const t0 = realNow();
Date.now = () => t0 + 65000;
log.timers[0][0]();                               // one tick of the 500ms interval
const label = m.button.title;
Date.now = realNow;
m.button.click(); await tick();
console.log(JSON.stringify({ label, interval: log.timers[0][1], cleared: log.cleared.length > 0 }));
"""
    )
    assert out["label"] == "Recording 1:05 — activate to stop"
    assert out["interval"] == 500 and out["cleared"] is True


def test_server_refusal_and_silence_are_reported_not_inserted() -> None:
    out = _run(
        """
const composer = makeComposer("keep");
const m = createMicController({ composer, base: "", wsId: "w" });
fake.stt = "error";
m.button.click(); await tick(); m.button.click(); await tick();
fake.stt = "empty";
m.button.click(); await tick(); m.button.click(); await tick();
console.log(JSON.stringify({ toasts: log.toasts, value: composer.inputEl.value, enabled: !m.button.disabled }));
"""
    )
    assert out["toasts"] == [["STT is not configured", "error"], ["No speech detected", "error"]]
    assert out["value"] == "keep" and out["enabled"] is True


def test_no_workstream_means_no_upload() -> None:
    out = _run(
        """
const composer = makeComposer();
const m = createMicController({ composer, base: "", wsId: () => "" });
m.button.click(); await tick(); m.button.click(); await tick();
console.log(JSON.stringify({ fetches: log.fetches.length, tracksStopped: log.tracks.map((t) => t.stopped) }));
"""
    )
    assert out == {"fetches": 0, "tracksStopped": [True]}


def test_destroy_releases_the_microphone_aborts_a_transcription_and_removes_the_button() -> None:
    out = _run(
        """
const composer = makeComposer();
const m = createMicController({ composer, base: "", wsId: "w" });
m.setAvailable(true);
m.button.click(); await tick();
m.destroy(); await tick();
const recording = { on: m.isRecording(), tracksStopped: log.tracks.map((t) => t.stopped), fetches: log.fetches.length };

fake.stt = "hang";
const m2 = createMicController({ composer, base: "", wsId: "w" });
m2.button.click(); await tick(); m2.button.click(); await tick();
const inflight = log.fetches[0].signal.aborted;
m2.destroy(); await tick();
console.log(JSON.stringify({ recording, inflight, aborted: log.fetches[0].signal.aborted,
  row: composer.actionsRowEl.children.map((c) => c.className.split(" ")[0]), toasts: log.toasts }));
"""
    )
    assert out["recording"] == {"on": False, "tracksStopped": [True], "fetches": 0}
    assert out["inflight"] is False and out["aborted"] is True
    assert out["row"] == ["composer-send"]
    assert out["toasts"] == []  # a destroyed pane's own abort is not an error to show


def test_an_on_toggle_hook_that_claims_the_click_skips_recording() -> None:
    """Voice mode reuses the button as a mic on/off switch: a hook returning
    true owns the click, anything else leaves dictation exactly as it was."""
    out = _run(
        """
let claim = true, seen = 0;
const composer = makeComposer();
const m = createMicController({ composer, base: "", wsId: "w",
  onToggle: () => { seen++; return claim; } });
m.button.click(); await tick();
const claimed = [m.isRecording(), log.recorders.length];
claim = false;
m.button.click(); await tick();
console.log(JSON.stringify({ seen, claimed, recording: m.isRecording() }));
"""
    )
    assert out["seen"] == 2
    assert out["claimed"] == [False, 0]
    assert out["recording"] is True
