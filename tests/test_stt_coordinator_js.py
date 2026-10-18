"""Dictation on the coordinator composer (coordinator.js).

The shipped coordinator pieces (the ``createMicController`` call,
``_applyVoiceRoles``, ``setBusy``, ``destroy``) are sliced out of
``createCoordinatorPane`` and run in node against the real ``stt_mic.js`` and
the fakes in ``_stt_js_stubs``, so the tests cover which composers show a mic,
where the audio goes and what the pane tears down.  The recorder's own state
machine is covered by ``test_stt_mic_js``.  Wiring that sits inside functions
whose other dependencies are not worth faking is pinned on the source text.
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
from tests._stt_js_stubs import MIC_DOM, MIC_STUBS
from tests._tts_js_stubs import TREE_DOM

pytestmark = node_skip

_STATIC = Path(__file__).resolve().parents[1] / "turnstone"
_COORD = (_STATIC / "console/static/coordinator/coordinator.js").read_text(encoding="utf-8")
_MODULE_PATH = _STATIC / "shared_static/stt_mic.js"
_MIC_MODULE = (
    demodulize(_MODULE_PATH)
    if _MODULE_PATH.exists()
    else "function createMicController() { throw new Error('missing stt_mic.js'); }"
)


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


# The pane state the sliced functions read.  `composer` is the stand-in from the
# stubs; `wsId` and `busy` are the pane's own closure variables.
_STATE = """
const composer = makeComposer();
composer.setBusy = () => {};
let wsId = "coord-1", busy = false, busySource = null;
let currentReasoningEl = null, currentReasoningBuf = "";
const reasoningActivity = { finish() {} };
const messagesEl = new El("div");
const queue = { onIdleEdge() {} };
function reconcileSendBlock() {}
function _addTtsActions() {}
let voiceRoles = { stt: false, tts: false };
const ttsPlayer = { stop() {} };
let ttsStream = null;
// Hands-free voice mode is covered by test_voice_loop_wiring_js; here it is
// only the pane state the sliced functions touch.
function _voiceRefresh() {}
let voiceLoop = null;
"""

_COORD_FNS = "\n".join(
    _fn(sig)
    for sig in (
        "  function _applyVoiceRoles(roles) {",
        "  function setBusy(b, source) {",
    )
)

# The mic is created before the functions that call it run, exactly as in the
# pane; a missing declaration is a pane with no mic.
_MIC_DECL = _decl(
    r"const mic = createMicController\(\{[\s\S]*?\}\);",
    "const mic = { setAvailable() { throw new Error('missing mic'); }, syncBusy() { throw new Error('missing mic'); }, destroy() { throw new Error('missing mic'); }, button: null };",
)

_PRELUDE = "\n".join((TREE_DOM, MIC_DOM, MIC_STUBS, _MIC_MODULE, _STATE, _MIC_DECL, _COORD_FNS))

# Everything destroy() touches besides the player and the mic is inert here.
# Each timer is declared by name so a new one added to destroy() fails loudly.
_DESTROY_STUBS = """
const closeStreamTransport = () => {}, historyRepair = { clear() {} }, histAttempts = new Map();
let staleRetryTimer = null, cancelTimeoutId = null, forceTimeoutId = null, tasksRefreshTimer = null,
  liveBadgeFlushTimer = null, pruneTimer = null, unmountTranscriptPresentation = null;
const removeVisibilityHandler = () => {}, _childObserver = null, unregisterTranscriptScroller = () => {};
const scrollFollow = { destroy() {} };
let voiceModeOn = false;
const voiceBtn = new El("button"), voiceStatusEl = new El("div");
"""


def _run(body: str, extra: str = "") -> Any:
    src = (
        _PRELUDE
        + extra
        + f"\n(async () => {{\n{body}\n}})().catch((e) => {{ console.error(e); process.exit(1); }});"
    )
    r = run_node_source(src)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


# A recording the way a user makes one: press, wait for the mic, press again.
_DICTATE = """
async function dictate() { mic.button.click(); await tick(); mic.button.click(); await tick(); }
"""


def test_the_mic_is_present_only_when_the_stt_role_resolves() -> None:
    out = _run(
        """
const row = composer.actionsRowEl;
const show = () => [mic.button.style.display, row.classList.contains("has-mic")];
const before = show();
_applyVoiceRoles({ stt: false, tts: true });     // read-aloud alone does not bring a mic
const ttsOnly = show();
_applyVoiceRoles({ stt: true, tts: false });
const sttOnly = show();
_applyVoiceRoles({ stt: false, tts: false });
console.log(JSON.stringify({ before, ttsOnly, sttOnly, off: show(),
  order: row.children.map((c) => c.className.split(" ")[0]) }));
"""
    )
    assert out["before"] == out["ttsOnly"] == out["off"] == ["none", False]
    assert out["sttOnly"] == ["", True]
    assert out["order"] == [
        "composer-mic-btn",
        "composer-send",
    ]  # beside send, as in the interactive pane


def test_recording_posts_to_the_console_coordinator_route() -> None:
    out = _run(
        _DICTATE
        + """
_applyVoiceRoles({ stt: true, tts: false });
await dictate();
const f = log.fetches[0];
console.log(JSON.stringify({ n: log.fetches.length, url: f.url, method: f.method, field: f.body.get("audio").name, toasts: log.toasts }));
"""
    )
    # The console's own route, never the node proxy's /node/{id}/... one.
    assert out["url"] == "/v1/api/workstreams/coord-1/speech-to-text/stream"
    assert out["n"] == 1 and out["method"] == "POST" and out["field"].startswith("speech.")
    assert out["toasts"] == []


def test_transcript_deltas_stream_into_the_coordinator_composer() -> None:
    out = _run(
        _DICTATE
        + """
composer.inputEl.value = "already typed ";
_applyVoiceRoles({ stt: true, tts: false });
await dictate();
console.log(JSON.stringify({ value: composer.inputEl.value, inputs: composer.inputEl.inputs, focused: composer.inputEl.focused }));
"""
    )
    assert out == {"value": "already typed hello world", "inputs": 2, "focused": True}


def test_permission_denial_disables_the_mic_with_a_message() -> None:
    out = _run(
        """
fake.gum = "deny";
_applyVoiceRoles({ stt: true, tts: false });
mic.button.click(); await tick();
setBusy(true); setBusy(false);                   // a turn passing must not re-enable a blocked mic
const msg = "Microphone blocked — enable it in your browser's site settings";
console.log(JSON.stringify({ disabled: mic.button.disabled, title: mic.button.title, label: mic.button.attrs["aria-label"], msg, toasts: log.toasts, fetches: log.fetches.length }));
"""
    )
    assert out["disabled"] is True
    assert out["title"] == out["label"] == out["msg"]
    assert out["toasts"] == [["Microphone access was denied", "error"]]
    assert out["fetches"] == 0


def test_the_mic_follows_the_coordinator_busy_state() -> None:
    out = _run(
        """
_applyVoiceRoles({ stt: true, tts: false });
setBusy(true);
const whileBusy = mic.button.disabled;
mic.button.click(); await tick();                // pressing a disabled-by-busy mic records nothing
const started = log.recorders.length;
setBusy(false);
const idle = mic.button.disabled;
mic.button.click(); await tick();
const recording = mic.button.classList.contains("is-recording");
setBusy(true); await tick();                     // the turn starts mid-recording: the clip is abandoned
console.log(JSON.stringify({ whileBusy, started, idle, recording,
  after: [mic.button.classList.contains("is-recording"), mic.button.disabled],
  tracksStopped: log.tracks.map((t) => t.stopped), fetches: log.fetches.length }));
"""
    )
    assert out["whileBusy"] is True and out["started"] == 0
    assert out["idle"] is False and out["recording"] is True
    assert out["after"] == [False, True]
    assert out["tracksStopped"] == [True] and out["fetches"] == 0


def test_destroy_aborts_the_recording_and_takes_the_mic_out() -> None:
    out = _run(
        """
_applyVoiceRoles({ stt: true, tts: false });
mic.button.click(); await tick();
const during = mic.button.classList.contains("is-recording");
destroy(); await tick();
console.log(JSON.stringify({ during, tracksStopped: log.tracks.map((t) => t.stopped),
  recorderState: log.recorders[0].state, fetches: log.fetches.length,
  row: composer.actionsRowEl.children.map((c) => c.className.split(" ")[0]) }));
""",
        extra=_DESTROY_STUBS + _fn("  function destroy() {"),
    )
    assert out["during"] is True
    assert out["tracksStopped"] == [True] and out["recorderState"] == "inactive"
    assert out["fetches"] == 0  # the clip was dropped, not uploaded
    assert out["row"] == ["composer-send"]


def test_destroy_while_the_permission_prompt_is_open_leaves_no_hot_microphone() -> None:
    out = _run(
        """
fake.gum = "pending";
_applyVoiceRoles({ stt: true, tts: false });
mic.button.click(); await tick();
destroy();
fake.pendingGum(); await tick();
console.log(JSON.stringify({ tracksStopped: log.tracks.map((t) => t.stopped), recorders: log.recorders.length }));
""",
        extra=_DESTROY_STUBS + _fn("  function destroy() {"),
    )
    assert out == {"tracksStopped": [True], "recorders": 0}


def _body(sig: str) -> str:
    assert sig in _COORD, f"{sig!r} not found in coordinator.js"
    return strip_js_comments(extract_braced(_COORD, sig))


def test_the_mic_is_mounted_from_the_shared_module_on_the_console_base() -> None:
    assert 'from "/shared/stt_mic.js"' in _COORD
    decl = _decl(r"const mic = createMicController\(\{[\s\S]*?\}\);", "")
    assert decl, "coordinator.js has no createMicController wiring"
    assert "composer: composer" in decl or "composer," in decl
    assert 'base: ""' in decl and "/node/" not in decl
    assert "isBusy: () => busy" in decl
    # The id is read per upload from the pane's closure, like every other route.
    assert re.search(r"wsId: \(\) => wsId|wsId: function \(\) \{\s*return wsId;", decl)
    assert "mic.setAvailable(" in _body("  function _applyVoiceRoles(roles) {")
    assert "mic.syncBusy()" in _body("  function setBusy(b, source) {")
    assert "mic.destroy()" in _body("  function destroy() {")
