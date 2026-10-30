"""The spectrum strip in the coordinator pane (``coordinator.js``).

The strip shows only while a voice session is on, and leaves with it.  The
shipped voice-mode functions are sliced out of ``createCoordinatorPane`` and
run in node against a recording strip, so these tests pin when the strip is
shown and hidden, which hooks it is handed, and that the AudioContext is
resumed from the Voice click and never on its own.

The strip's own drawing and lifecycle are covered by ``test_voice_viz_strip_js``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import (
    extract_braced,
    node_skip,
    run_node_source,
    strip_js_comments,
)
from tests._tts_js_stubs import TREE_DOM

pytestmark = node_skip

_STATIC = Path(__file__).resolve().parents[1] / "turnstone"
_COORD = (_STATIC / "console/static/coordinator/coordinator.js").read_text(encoding="utf-8")
_COORD_CODE = strip_js_comments(_COORD)


def _fn(sig: str) -> str:
    """The shipped function, or a throwing stub when the source lacks it."""
    if sig not in _COORD:
        name = sig.split("(")[0].split()[-1]
        return f"function {name}() {{ throw new Error('missing {name}'); }}"
    return extract_braced(_COORD, sig)


def _body(sig: str) -> str:
    """The shipped function's body with comments removed, for source pins."""
    return strip_js_comments(extract_braced(_COORD, sig))


_COORD_FNS = "\n".join(
    _fn(sig)
    for sig in (
        "  function _voiceTurnOn() {",
        "  function _voiceTurnOff() {",
        "  function _voiceToggle() {",
    )
)

# A recording strip: every call the pane makes is logged, so a test reads what
# the shipped functions asked for.  `ctxResumes` counts resumeFromGesture calls.
_FAKES = """
const vizLog = { show: 0, hide: 0, streamsIn: [], audiosIn: [], resumes: 0, speakers: [], pending: [] };
const voiceViz = {
  show() { vizLog.show++; },
  hide() { vizLog.hide++; },
  attachStream(s, key) { vizLog.streamsIn.push(key); },
  attachAudio(a, key) { vizLog.audiosIn.push(key); },
  setSpeaker(s) { vizLog.speakers.push(s); },
  setPending(on) { vizLog.pending.push(on); },
  resumeFromGesture() { vizLog.resumes++; return Promise.resolve(); },
  stop() { vizLog.hide++; },
};
let voiceModeOn = false, voiceLoop = null, voiceAutoStop = false;
const voiceBtn = new El("button");
const voiceLoopStub = { enable() {}, disable() {}, muted: false, setMuted() {} };
function _voicePaintMute() {}
function _voiceRefreshShown() {}
// The stream run the mode owns; turn-off cancels it.  Stubbed as the pane has it.
let ttsStream = null;
function _cancelTtsStream() { ttsStream = null; }
function _voiceSetStatus() {}
"""

_STATE = """
const composer = { value: "", el: new El("div"), actionsRowEl: new El("div"), sendBtn: new El("button") };
let voiceOffer = true, voiceSetup = null, voiceDoneProbe = true;
let voiceRoles = { stt: true, tts: true };
const mic = { start() {}, stop() {}, button: new El("button") };
function _voiceHooksReady() { return true; }
function _voiceCreateLoop() { voiceLoop = voiceLoopStub; }
"""

_PRELUDE = "\n".join((TREE_DOM, _STATE, _FAKES, _COORD_FNS))


def _run(body: str) -> Any:
    src = (
        _PRELUDE
        + f"\n(async () => {{\n{body}\n}})().catch((e) => {{ console.error(e); process.exit(1); }});"
    )
    r = run_node_source(src)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_the_strip_is_shown_when_voice_mode_turns_on_and_hidden_when_it_turns_off() -> None:
    out = _run(
        """
_voiceTurnOn();
const on = { shown: vizLog.show, mode: voiceModeOn };
_voiceTurnOff();
const off = { hidden: vizLog.hide, mode: voiceModeOn };
console.log(JSON.stringify({ on, off }));
"""
    )
    assert out["on"] == {"shown": 1, "mode": True}
    assert out["off"]["hidden"] >= 1
    assert out["off"]["mode"] is False


def test_turning_voice_mode_off_twice_does_not_hide_the_strip_twice() -> None:
    # A second off is a no-op: the mode is already off, so no extra teardown.
    out = _run(
        """
_voiceTurnOn();
_voiceTurnOff();
const once = vizLog.hide;
_voiceTurnOff();
console.log(JSON.stringify({ once, twice: vizLog.hide }));
"""
    )
    assert out["once"] == out["twice"]


def test_turning_voice_mode_off_clears_a_pending_handoff() -> None:
    # The handoff belongs to a live turn.  With the mode off there is no reply
    # to warm for, and the strip is about to leave.
    out = _run(
        """
_voiceTurnOn();
_voiceTurnOff();
console.log(JSON.stringify({ pending: vizLog.pending }));
"""
    )
    assert out["pending"] == [False]


def test_the_strip_is_never_shown_outside_a_voice_session() -> None:
    # A session that never turned voice mode on must not show the strip, so a
    # text-only coordinator keeps its sidebar exactly as it was.
    out = _run(
        """
voiceOffer = false;
_voiceTurnOn();
console.log(JSON.stringify({ shown: vizLog.show, mode: voiceModeOn }));
"""
    )
    assert out == {"shown": 0, "mode": False}


def test_turning_voice_mode_on_resumes_the_audio_context_from_the_click() -> None:
    # Browsers start an AudioContext suspended and only let a user gesture resume
    # it.  The Voice click is that gesture, so the resume belongs in turn-on.
    out = _run(
        """
_voiceTurnOn();
console.log(JSON.stringify({ resumes: vizLog.resumes }));
"""
    )
    assert out["resumes"] == 1


def test_the_mic_stream_and_the_reply_element_reach_the_strip_with_their_speakers() -> None:
    # The mic is the user's voice and the reply's element is the AI's.  Both
    # reach the strip under those speaker keys, through the seams the pane uses:
    # the mic's onStream option and the player's setAudioListener.
    assert re.search(
        r"onStream:\s*\(s\)\s*=>\s*\{[^}]*voiceViz\.attachStream\(s, \"user\"\)", _COORD_CODE
    )
    assert re.search(
        r"setAudioListener\(\(a\)\s*=>\s*\{[^}]*voiceViz\.attachAudio\(a, \"ai\"\)", _COORD_CODE
    )


def test_the_strip_is_built_once_and_mounted_above_the_children_list() -> None:
    # The strip sits in the right panel, before the CHILDREN section, and is
    # created once per pane: a second pane must not reuse this pane's strip.
    assert "createSpectrumStrip" in _COORD_CODE
    assert re.search(
        r"createSpectrumStrip\(\{\s*host:\s*root\.querySelector\(\"#coord-sidebar\"\),\s*"
        r"before:\s*root\.querySelector\(\"#coord-children-wrap\"\)",
        _COORD_CODE,
    )


def test_destroy_hides_the_strip_so_its_document_listener_is_released() -> None:
    # The strip's visibility listener lives on the document.  A pane that is
    # destroyed while voice mode is on must release it, through the one teardown.
    destroy = _body("  function destroy() {")
    assert "voiceViz" in destroy
    assert re.search(r"voiceViz\.(stop|hide)\(\)", destroy)


def test_voice_viz_module_is_imported_by_the_coordinator() -> None:
    assert 'from "/shared/voice_viz.js"' in _COORD_CODE
