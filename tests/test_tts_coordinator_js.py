"""Read-aloud on the coordinator pane (coordinator.js).

The shipped coordinator functions (``_addTtsActions``, ``_applyVoiceRoles``,
``finishAssistantStream``, ``_refreshRetryButton``, ``destroy``) are sliced out
of ``createCoordinatorPane`` and run in node against a tree DOM, with the real
``tts_player.js`` and the real ``copy_actions.js`` bar helpers, so the tests
cover which replies carry a speaker and what a click requests.  The player's own
playback behaviour is covered by ``test_tts_segments_js`` / ``test_tts_player_js``.
Teardown and rebuild sit inside functions whose other dependencies are not worth
faking (``refetchHistory``, the SSE reconnect path), so those are pinned on the
source text.
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
from tests._tts_js_stubs import STUBS, TREE_DOM

pytestmark = node_skip

_STATIC = Path(__file__).resolve().parents[1] / "turnstone"
_COORD = (_STATIC / "console/static/coordinator/coordinator.js").read_text(encoding="utf-8")
_COPY = (_STATIC / "shared_static/copy_actions.js").read_text(encoding="utf-8")
_PLAYER_MODULE = demodulize(_STATIC / "shared_static/tts_player.js")


def _fn(sig: str) -> str:
    """The shipped function, or a throwing stub when the source lacks it, so a
    test against code that predates the wiring fails on its assertions."""
    if sig not in _COORD:
        name = sig.split("(")[0].split()[-1]
        return f"function {name}() {{ throw new Error('missing {name}'); }}"
    return extract_braced(_COORD, sig)


def _decl(pattern: str, fallback: str) -> str:
    """One shipped declaration (``let voiceRoles = ...;``), or a stand-in that
    makes the missing wiring fail the test rather than the harness."""
    m = re.search(pattern, _COORD)
    return m.group(0) if m else fallback


_COPY_FNS = "\n".join(
    extract_braced(_COPY, sig)
    for sig in (
        "export function findMsgActionsBar(el) {",
        "export function ensureMsgActionsBar(el) {",
        "function _icon(cls) {",
        "export function buildMsgRetryButton(onRetry) {",
    )
).replace("export function", "function")

_COORD_FNS = "\n".join(
    _fn(sig)
    for sig in (
        "  function _addTtsActions() {",
        "  function _applyVoiceRoles(roles) {",
        "  function finishAssistantStream() {",
        "  function _refreshRetryButton() {",
        "  function _addRetryAction(el) {",
    )
)

_STATE = """
function buildMsgCopyButton() { const b = new El('button'); b.className = 'msg-action-btn msg-copy-btn'; return b; }
const messagesEl = new El('div');
let currentAssistantEl = null, currentAssistantBuf = '', currentReasoningEl = null, currentReasoningBuf = '';
const reasoningActivity = { finish() {} };
const _scheduleScroll = () => {};
function _retryLast() {}
// The dictation mic (covered by test_stt_coordinator_js) is inert here.
const mic = { setAvailable() {}, syncBusy() {}, destroy() {} };
"""

_HELPERS = r"""
// What appendMsg does for an assistant bubble: body + the persistent copy button.
function appendReply(text) {
  const el = reply(text);
  ensureMsgActionsBar(el).appendChild(buildMsgCopyButton(el));
  messagesEl.appendChild(el);
  return el;
}
const names = (el) => el.children.filter((c) => c.className.includes("msg-actions"))
  .flatMap((bar) => bar.children.map((b) => b.className.split(" ").find((c) => /^msg-(retry|tts|copy)-btn$/.test(c)).slice(4, -4)));
"""

_PRELUDE = "\n".join(
    (
        TREE_DOM,
        _COPY_FNS,
        _PLAYER_MODULE,
        STUBS,
        _STATE,
        _decl(r"let voiceRoles = [^;]*;", "let voiceRoles = undefined;"),
        _decl(
            r"const ttsPlayer = createTtsPlayer\(\{[^}]*\}\);",
            "const ttsPlayer = { stop() { throw new Error('missing ttsPlayer'); } };",
        ),
        _COORD_FNS,
        _HELPERS,
    )
)


def _run(body: str, extra: str = "") -> Any:
    src = _PRELUDE + extra + f"\n(async () => {{\n{body}\n}})().catch((e) => {{ console.error(e); process.exit(1); }});"
    r = run_node_source(src)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_every_finished_reply_gets_a_speaker_beside_copy_once_tts_is_available() -> None:
    out = _run(
        """
const a = appendReply("First reply."), b = appendReply("Second reply."), c = appendReply("Third reply.");
_applyVoiceRoles({ stt: false, tts: true });
_applyVoiceRoles({ stt: false, tts: true });   // a second resolve adds no duplicates
const before = [a, b, c].map(names);
finishAssistantStream();                        // settle edge: retry joins the last reply
console.log(JSON.stringify([before, [a, b, c].map(names)]));
"""
    )
    assert out == [
        [["tts", "copy"], ["tts", "copy"], ["tts", "copy"]],
        [["tts", "copy"], ["tts", "copy"], ["retry", "tts", "copy"]],
    ]


def test_no_speaker_anywhere_without_a_tts_role() -> None:
    out = _run(
        """
const a = appendReply("First reply."), b = appendReply("Second reply.");
_applyVoiceRoles({ stt: true, tts: false });
finishAssistantStream();
console.log(JSON.stringify([names(a), names(b)]));
"""
    )
    assert out == [["copy"], ["retry", "copy"]]


def test_a_streaming_reply_gets_its_speaker_only_after_it_settles() -> None:
    out = _run(
        """
const done = appendReply("An earlier reply."), live = appendReply("Half a rep");
currentAssistantEl = live;                      // tokens still arriving
_applyVoiceRoles({ stt: false, tts: true });
const during = [names(done), names(live)];
finishAssistantStream();                        // stream_end: refs cleared, then the refresh
console.log(JSON.stringify([during, names(live), currentAssistantEl]));
"""
    )
    assert out == [[["tts", "copy"], ["copy"]], ["retry", "tts", "copy"], None]


def test_history_replayed_before_the_roles_resolve_is_backfilled() -> None:
    """The /v1/api/models probe resolves after the first history paint, so the
    replayed bubbles exist without a speaker until the roles arrive."""
    out = _run(
        """
const bs = [appendReply("One."), appendReply("Two."), appendReply("Three.")];
_refreshRetryButton();                          // the end-of-replay refresh, roles unknown
const before = bs.map(names);
_applyVoiceRoles({ stt: false, tts: true });
console.log(JSON.stringify([before, bs.map(names)]));
"""
    )
    assert out == [
        [["copy"], ["copy"], ["retry", "copy"]],
        [["tts", "copy"], ["tts", "copy"], ["retry", "tts", "copy"]],
    ]


def test_a_replay_with_roles_already_known_adds_speakers_through_the_refresh() -> None:
    out = _run(
        """
_applyVoiceRoles({ stt: false, tts: true });
const bs = [appendReply("One."), appendReply("Two.")];   // a rebuilt column
_refreshRetryButton();
console.log(JSON.stringify(bs.map(names)));
"""
    )
    assert out == [["tts", "copy"], ["retry", "tts", "copy"]]


def test_clicking_a_speaker_posts_the_reply_to_the_console_tts_endpoint() -> None:
    out = _run(
        """
const a = appendReply("The first reply is long enough to read.");
_applyVoiceRoles({ stt: false, tts: true });
const btn = a.querySelector(".msg-tts-btn");
btn.click(); await tick();
const f = log.fetches.filter((x) => x.url.endsWith("/tts"));
console.log(JSON.stringify({
  urls: f.map((x) => x.url), methods: f.map((x) => x.method), texts: f.map((x) => x.text),
  playing: btn.classList.contains("is-playing"), pressed: btn.attrs["aria-pressed"],
  label: btn.attrs["aria-label"], title: btn.title, toasts: log.toasts,
}));
"""
    )
    assert out["urls"] == ["/v1/api/tts"]  # the console's own, never /node/{id}/...
    assert out["methods"] == ["POST"]
    assert out["texts"] == ["The first reply is long enough to read."]
    assert out["playing"] is True and out["pressed"] == "true"
    assert out["label"] == out["title"] == "Play response aloud"
    assert out["toasts"] == []


def test_pressing_again_stops_and_pressing_another_speaker_switches() -> None:
    out = _run(
        """
const a = appendReply("The first reply is long enough to read.");
const b = appendReply("The second reply is long enough to read.");
_applyVoiceRoles({ stt: false, tts: true });
const ba = a.querySelector(".msg-tts-btn"), bb = b.querySelector(".msg-tts-btn");
const st = () => [ba, bb].map((x) => [x.classList.contains("is-playing"), x.attrs["aria-pressed"]]);
ba.click(); await tick();
const first = st();
ba.click(); await tick();                       // toggle: stop
const stopped = st();
ba.click(); await tick();
bb.click(); await tick();                       // another reply: stop A, play B
const switched = st();
console.log(JSON.stringify({ first, stopped, switched, paused: log.audios.map((x) => x.paused) }));
"""
    )
    assert out["first"] == [[True, "true"], [False, "false"]]
    assert out["stopped"] == [[False, "false"], [False, "false"]]
    assert out["switched"] == [[False, "false"], [True, "true"]]
    # One Audio element per run: the third (reply A again) was paused by the switch, the fourth plays.
    assert out["paused"][-2:] == [True, False]


# Everything destroy() touches besides the player is inert here.  Each timer is
# declared by name so a new one added to destroy() fails this harness loudly.
_DESTROY_STUBS = """
const closeStreamTransport = () => {}, historyRepair = { clear() {} }, histAttempts = new Map();
let staleRetryTimer = null, cancelTimeoutId = null, forceTimeoutId = null, tasksRefreshTimer = null,
  liveBadgeFlushTimer = null, pruneTimer = null, unmountTranscriptPresentation = null;
const removeVisibilityHandler = () => {}, _childObserver = null, unregisterTranscriptScroller = () => {};
const scrollFollow = { destroy() {} };
"""


def test_destroy_stops_playback() -> None:
    out = _run(
        """
const a = appendReply("The first reply is long enough to read.");
_applyVoiceRoles({ stt: false, tts: true });
const btn = a.querySelector(".msg-tts-btn");
btn.click(); await tick();
const during = [btn.classList.contains("is-playing"), ttsPlayer.isPlaying()];
destroy();
console.log(JSON.stringify({ during, after: [btn.classList.contains("is-playing"), btn.attrs["aria-pressed"], ttsPlayer.isPlaying()], paused: log.audios[0].paused }));
""",
        extra=_DESTROY_STUBS + _fn("  function destroy() {"),
    )
    assert out == {"during": [True, True], "after": [False, "false", False], "paused": True}


def _body(sig: str) -> str:
    assert sig in _COORD, f"{sig!r} not found in coordinator.js"
    return strip_js_comments(extract_braced(_COORD, sig))


def test_a_history_rebuild_stops_playback_before_it_detaches_the_speaker() -> None:
    body = _body("  async function refetchHistory(seedCursor = false) {")
    assert "ttsPlayer.stop()" in body
    stop = body.index("ttsPlayer.stop()")
    # After the render-time supersession gate (a skipped render keeps the column,
    # and so keeps playing) and immediately before the wipe.
    assert body.index("if (currentAssistantEl || currentReasoningEl) return;") < stop
    assert stop < body.index("messagesEl.replaceChildren()")


def test_an_sse_reconnect_does_not_stop_playback() -> None:
    for sig in (
        "  function closeStreamTransport() {",
        "  function connectSSE() {",
        "  function suspendStream() {",
        "  function onLogin() {",
    ):
        assert "ttsPlayer" not in _body(sig), sig


def test_voice_roles_come_from_the_console_and_the_player_uses_its_base() -> None:
    assert 'from "/shared/tts_player.js"' in _COORD
    assert re.search(r'const ttsPlayer = createTtsPlayer\(\{ base: "" \}\);', _COORD)
    assert 'getVoiceRoles("").then(_applyVoiceRoles)' in _body("  async function init() {")
    assert "/node/" not in _body("  function _addTtsActions() {")
