"""Streaming read-aloud (``shared_static/tts_player.js``).

A reply is read aloud while it is still arriving: the feeder holds the trailing
partial sentence, hands every completed sentence to the player, and the player
synthesizes and plays those clips with the same one-fetch-in-flight,
prefetch-one-ahead discipline as a finished reply.  ``stream_end`` flushes the
remainder, and a cancel drops everything still queued.

The shipped module is evaluated in node (imports stripped, so ``authFetch``,
``Audio``, ``showToast`` ... are the stubs in ``_tts_js_stubs``), which is why
these drive the real feeder and player rather than a copy.  Time-to-first-audio
is a live measurement and is noted in the run-4 report, not asserted here.
"""

from __future__ import annotations

import json
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

_TURNSTONE = Path(__file__).resolve().parents[1] / "turnstone"
_ROOT = _TURNSTONE / "shared_static"
_MODULE = demodulize(_ROOT / "tts_player.js")
_INTERACTIVE = (_ROOT / "interactive.js").read_text(encoding="utf-8")
_COORDINATOR = (_TURNSTONE / "console/static/coordinator/coordinator.js").read_text(
    encoding="utf-8"
)

_PRELUDE = "\n".join((TREE_DOM, _MODULE, STUBS))


def _run(body: str, prelude: str = _PRELUDE) -> Any:
    src = (
        prelude
        + f"\n(async () => {{\n{body}\n}})().catch((e) => {{ console.error(e); process.exit(1); }});"
    )
    r = run_node_source(src)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------
# The feeder: text deltas -> completed clips
# --------------------------------------------------------------------------


def test_only_a_completed_sentence_is_handed_to_the_run() -> None:
    out = _run(
        """
stubStream = true;
const clips = [];
const feeder = createStreamFeeder({ onClip: (clip) => clips.push(clip.text) });
feeder.push("The first sentence is complete. The second is still");
const afterFirst = [...clips];                 // the completed sentence, nothing more
feeder.push(" being written.");
const beforeEnd = [...clips];                  // still no terminal mark: held
feeder.end();
console.log(JSON.stringify({ afterFirst, beforeEnd, afterEnd: clips }));
"""
    )
    assert out["afterFirst"] == ["The first sentence is complete."]
    assert out["beforeEnd"] == ["The first sentence is complete."]
    assert out["afterEnd"] == [
        "The first sentence is complete.",
        "The second is still being written.",
    ]


def test_markdown_markers_are_not_spoken() -> None:
    # A streamed delta is raw model text, not the rendered bubble, so the
    # markers the finished-reply DOM path never sees must be dropped here.
    out = _run(
        r"""
stubStream = true;
const clips = [];
const feeder = createStreamFeeder({ onClip: (clip) => clips.push(clip.text) });
feeder.push("## Summary\n\nThe **build** is `green` and _ready_. ");
feeder.push("See [the log](http://x/y) for details.\n- First item done.\n");
feeder.end();
console.log(JSON.stringify(clips.join(" ")));
"""
    )
    assert "#" not in out
    assert "*" not in out
    assert "`" not in out
    assert "_" not in out
    assert "http" not in out
    assert "build" in out and "green" in out and "the log" in out and "First item" in out


def test_deltas_that_split_a_sentence_mid_word_still_produce_whole_sentences() -> None:
    out = _run(
        """
const clips = [];
const feeder = createStreamFeeder({ onClip: (clip) => clips.push(clip.text) });
feeder.push("The quick brown fo");
feeder.push("x jumps over the lazy dog. Then it");
feeder.push(" rests.");
feeder.end();
console.log(JSON.stringify(clips));
"""
    )
    assert out == [
        "The quick brown fox jumps over the lazy dog.",
        "Then it rests.",
    ]


def test_abbreviations_and_decimals_are_not_cut_early() -> None:
    # Each delta stops on the ambiguous character: "e.g." and "3." at a delta
    # edge, and the abbreviation again before "Smith".  A mark needs whitespace
    # after it, and a known abbreviation or initial is not a sentence end, so
    # each sentence reaches the shaper in one piece.
    out = _run(
        """
const clips = [];
const feeder = createStreamFeeder({ onClip: (clip) => clips.push(clip.text) });
feeder.push("Use e.g.");
feeder.push(" this rule for values like 3.");
feeder.push("14 exactly. Dr. Smith");
feeder.push(" arrived.");
feeder.end();
console.log(JSON.stringify(clips));
"""
    )
    assert out == [
        "Use e.g. this rule for values like 3.14 exactly.",
        "Dr. Smith arrived.",
    ]


def test_stream_end_flushes_a_reply_that_trails_off() -> None:
    out = _run(
        """
const clips = [];
const feeder = createStreamFeeder({ onClip: (clip) => clips.push(clip.text) });
feeder.push("A reply that trails off without");
feeder.push(" a final mark");
const beforeEnd = [...clips];
feeder.end();
feeder.push(" ignored after the end");
console.log(JSON.stringify({ beforeEnd, afterEnd: clips }));
"""
    )
    assert out["beforeEnd"] == []
    assert out["afterEnd"] == ["A reply that trails off without a final mark."]


def test_a_line_break_completes_a_clip_without_terminal_punctuation() -> None:
    # A line break is a clip boundary even without a terminal mark: waiting
    # for the next paragraph's period would keep the first audio silent.
    out = _run(
        """
const clips = [];
const feeder = createStreamFeeder({ onClip: (clip) => clips.push(clip.text) });
feeder.push("A short status line\\nAll services are up.\\n");
feeder.push("The next paragraph is still");
feeder.end();
console.log(JSON.stringify(clips));
"""
    )
    assert out == [
        "A short status line.",
        "All services are up.",
        "The next paragraph is still.",
    ]


def test_cancel_drops_the_held_text() -> None:
    out = _run(
        """
const clips = [];
const feeder = createStreamFeeder({ onClip: (clip) => clips.push(clip.text) });
feeder.push("A complete sentence is here. A partial sentence is");
feeder.cancel();
feeder.push(" ignored after cancel");
feeder.end();
console.log(JSON.stringify(clips));
"""
    )
    assert out == ["A complete sentence is here."]


# --------------------------------------------------------------------------
# The player run: playback, acknowledgement, cancellation
# --------------------------------------------------------------------------


def _posts_js() -> str:
    return 'log.fetches.filter((f) => f.method === "POST").map((f) => f.text)'


def test_a_streaming_run_speaks_completed_clips_and_exposes_what_was_heard() -> None:
    out = _run(
        f"""
stubStream = true;
const player = createTtsPlayer({{ base: "" }});
const run = player.stream();
run.push("First sentence is complete. Second sentence is complete.");
run.end();                                       // the last period is at the buffer edge
await tick();
const fetched = {_posts_js()};
const beforeEnded = [...run.heard];              // fetched is not heard
log.audios[0].fire("ended");                     // first clip really finishes
await tick();
const afterFirst = [...run.heard];
log.audios[0].fire("ended");                     // the same element plays clip two
await tick();
console.log(JSON.stringify({{ fetched, beforeEnded, afterFirst, afterSecond: [...run.heard],
  audios: log.audios.length }}));
"""
    )
    assert out["fetched"] == [
        "First sentence is complete.",
        "Second sentence is complete.",
    ]
    assert out["beforeEnded"] == []
    assert out["afterFirst"] == ["First sentence is complete."]
    assert out["afterSecond"] == [
        "First sentence is complete.",
        "Second sentence is complete.",
    ]
    assert out["audios"] == 1  # one element for the whole run, as in play()


def test_cancel_drops_every_queued_clip_and_never_acknowledges_one() -> None:
    out = _run(
        f"""
stubStream = true;
const player = createTtsPlayer({{ base: "" }});
const run = player.stream();
run.push("First sentence is here. Second sentence is here. Third sentence is here.");
await tick();
const before = {_posts_js()};                    // first + the one-clip prefetch
run.cancel();
log.audios[0].fire("ended");                     // a late ended must not count
await tick();
run.push("Fourth sentence arrives after the cancel.");
run.end();
await tick();
console.log(JSON.stringify({{ before, after: {_posts_js()}, heard: [...run.heard],
  paused: log.audios[0].paused }}));
"""
    )
    assert len(out["before"]) == 2
    # The third clip was queued, not fetched; the cancel dropped it, and the
    # post-cancel push was ignored.
    assert out["after"] == out["before"]
    assert out["heard"] == []
    assert out["paused"] is True


def test_a_new_streaming_run_stops_the_previous_one() -> None:
    out = _run(
        f"""
stubStream = true;
const first = createTtsPlayer({{ base: "" }});
const second = createTtsPlayer({{ base: "/node/n2" }});
const a = first.stream();
a.push("A sentence from the first pane. Another one follows.");
await tick();
const playing = {{ paused: log.audios[0].paused, heard: [...a.heard] }};
const b = second.stream();
b.push("A sentence from the second pane. Another one follows.");
await tick();
console.log(JSON.stringify({{ playing, after: {{ paused: log.audios[0].paused,
  first: [...a.heard], second: [...b.heard] }}, posts: {_posts_js()} }}));
"""
    )
    assert out["playing"] == {"paused": False, "heard": []}
    assert out["after"]["paused"] is True  # one voice per page
    assert out["after"]["first"] == []
    assert out["after"]["second"] == []
    assert out["posts"] == [
        "A sentence from the first pane.",
        "A sentence from the second pane.",
    ]


def test_an_explicit_play_supersedes_a_stream_that_has_not_started() -> None:
    # A streaming run waits on the config probe before its first clip, so it is
    # not yet in _activeTtsRuns; pressing the speaker on a finished reply must
    # still win, or the stream's first sentence would silence the chosen reply.
    out = _run(
        f"""
stubStream = true;
const player = createTtsPlayer({{ base: "" }});
const run = player.stream();
run.push("A streamed sentence that must not be spoken. Another one follows.");
const el = reply("The reply the user explicitly chose, long enough to read.");
player.play(el, new El("button"));
await tick();
console.log(JSON.stringify({{ posts: {_posts_js()}, audios: log.audios.length,
  streamHeard: [...run.heard], playing: player.isPlaying() }}));
"""
    )
    assert out["posts"] == ["The reply the user explicitly chose, long enough to read."]
    assert out["audios"] == 1
    assert out["streamHeard"] == []
    assert out["playing"] is True


def test_with_the_setting_off_a_stream_fetches_and_plays_nothing() -> None:
    out = _run(
        f"""
stubStream = false;
const player = createTtsPlayer({{ base: "" }});
const run = player.stream();
run.push("A sentence that must not be spoken. Another one either.");
run.end();
await tick();
console.log(JSON.stringify({{ posts: {_posts_js()}, audios: log.audios.length,
  heard: [...run.heard], fetched: log.fetches.map((f) => f.url) }}));
"""
    )
    assert out["posts"] == []
    assert out["audios"] == 0
    assert out["heard"] == []
    # Streaming was decided from the same one probe the player already uses.
    assert out["fetched"] == ["/v1/api/tts/styles"]


def test_the_setting_rides_the_styles_probe_once_per_base_and_fails_closed() -> None:
    out = _run(
        """
stubStream = true;
const same = [await getTtsStreamEnabled(""), await getTtsStreamEnabled("")];
stubStream = undefined;                          // a server that predates the field
const missing = await getTtsStreamEnabled("/node/missing");
stylesMode = "down";                             // a failed probe
const down = await getTtsStreamEnabled("/node/down");
console.log(JSON.stringify({
  same, missing, down,
  urls: log.fetches.filter((f) => f.url.endsWith("/tts/styles")).map((f) => f.url),
}));
"""
    )
    assert out["same"] == [True, True]
    assert out["missing"] is False
    assert out["down"] is False
    assert out["urls"] == [
        "/v1/api/tts/styles",
        "/node/missing/v1/api/tts/styles",
        "/node/down/v1/api/tts/styles",
    ]


def test_a_style_and_a_profile_ride_a_streaming_run_like_a_finished_reply() -> None:
    out = _run(
        """
stylesMode = "ok";
stubStream = true;
stubRate = 1.25;
stubProfile = { clip_max_chars: 100 };
localStorage.setItem("turnstone.tts.style", "Social");
const player = createTtsPlayer({ base: "" });
const run = player.stream();
run.push("A sentence long enough to be spoken with a style.");
run.end();
await tick();
log.audios[0].fire("ended");
await tick();
console.log(JSON.stringify({ bodies: log.fetches.filter((f) => f.method === "POST").map((f) => f.body),
  rate: log.audios[0].defaultPlaybackRate, revoked: log.revoked.length }));
"""
    )
    assert out["bodies"] == [
        {"text": "A sentence long enough to be spoken with a style.", "style": "Social"}
    ]
    assert out["rate"] == 1.25
    assert out["revoked"] == 1


# --------------------------------------------------------------------------
# Pane wiring: the SSE hooks feed, flush and cancel the run
# --------------------------------------------------------------------------


def _interactive_method(sig: str) -> str:
    """The shipped method, or a throwing stub when the source lacks it, so a
    test against code that predates the wiring fails on its assertions."""
    if sig not in _INTERACTIVE:
        name = sig.split("(")[0]
        return f"{name}() {{ throw new Error('missing {name}'); }}"
    return extract_braced(_INTERACTIVE, sig)


def _coordinator_fn(sig: str) -> str:
    if sig not in _COORDINATOR:
        name = sig.split("(")[0].split()[-1]
        return f"function {name}() {{ throw new Error('missing {name}'); }}"
    return extract_braced(_COORDINATOR, sig)


_INTERACTIVE_METHODS = ",\n".join(
    _interactive_method(sig)
    for sig in (
        "_streamTTS(text) {",
        "_endTTSStream() {",
        "_cancelTTSStream() {",
        "_stopTTS() {",
    )
)

_COORDINATOR_FNS = "\n".join(
    _coordinator_fn(sig)
    for sig in (
        "  function _feedTtsStream(text) {",
        "  function _endTtsStream() {",
        "  function _cancelTtsStream() {",
    )
)


def test_the_sse_routers_actually_call_the_stream_hooks() -> None:
    # The methods above are only useful if the event router calls them on the
    # live edges: content feeds, stream_end flushes, cancelled drops the queue.
    interactive = strip_js_comments(_INTERACTIVE)
    coordinator = strip_js_comments(_COORDINATOR)
    assert "this._streamTTS(evt.text);" in interactive
    assert "this._endTTSStream();" in interactive
    assert "this._cancelTTSStream();" in interactive
    assert '_feedTtsStream(ev.text || "");' in coordinator
    assert "_endTtsStream();" in coordinator
    assert "_cancelTtsStream();" in coordinator


def test_the_interactive_pane_feeds_flushes_and_cancels_its_stream() -> None:
    prelude = "\n".join(
        (
            TREE_DOM,
            "const calls = [];",
            "const T = {",
            "  _voiceRoles: { stt: false, tts: true },",
            "  _voiceMode: true,",
            "  _ttsStream: null,",
            "  _ttsPlayer: {",
            "    stream() { calls.push('stream'); return {",
            "      push: (text) => calls.push(['push', text]),",
            "      end: () => calls.push('end'),",
            "      cancel: () => calls.push('cancel'),",
            "    }; },",
            "    stop() { calls.push('stop'); },",
            "  },",
            _INTERACTIVE_METHODS,
            "};",
        )
    )
    out = _run(
        """
T._streamTTS("first delta");
T._streamTTS(" second delta");                  // one run per reply, not one per delta
const firstRun = T._ttsStream !== null;
T._endTTSStream();
const afterEnd = T._ttsStream;
T._streamTTS("next segment");                   // a later segment starts a new run
T._cancelTTSStream();
const afterCancel = T._ttsStream;
T._streamTTS("a delta after a cancel starts fresh");
T._stopTTS();
console.log(JSON.stringify({ calls, firstRun, afterEnd, afterCancel, afterStop: T._ttsStream }));
""",
        prelude=prelude,
    )
    assert out == {
        "calls": [
            "stream",
            ["push", "first delta"],
            ["push", " second delta"],
            "end",
            "stream",
            ["push", "next segment"],
            "cancel",
            "stream",
            ["push", "a delta after a cancel starts fresh"],
            "stop",
        ],
        "firstRun": True,
        "afterEnd": None,
        "afterCancel": None,
        "afterStop": None,
    }


def test_the_coordinator_pane_feeds_flushes_and_cancels_its_stream() -> None:
    prelude = "\n".join(
        (
            TREE_DOM,
            "const calls = [];",
            "let voiceRoles = { stt: false, tts: true };",
            "let ttsStream = null;",
            # Hands-free voice mode owns the streaming run (see
            # test_voice_loop_wiring_js): with the mode on, a delta starts one.
            "let voiceModeOn = true;",
            "let voiceLoop = { onReplyStarted() { calls.push('reply'); } };",
            "const currentAssistantEl = null;",
            "function _watchVoiceRun(run) { calls.push(['watch', !!run.done]); }",
            "const ttsPlayer = {",
            "  stream() { calls.push('stream'); return {",
            "    push: (text) => calls.push(['push', text]),",
            "    end: () => calls.push('end'),",
            "    cancel: () => calls.push('cancel'),",
            "  }; },",
            "};",
            _COORDINATOR_FNS,
        )
    )
    out = _run(
        """
_feedTtsStream("first delta");
_feedTtsStream(" second delta");
const firstRun = ttsStream !== null;
_endTtsStream();
const afterEnd = ttsStream;
_feedTtsStream("next segment");
_cancelTtsStream();
const afterCancel = ttsStream;
console.log(JSON.stringify({ calls, firstRun, afterEnd, afterCancel }));
""",
        prelude=prelude,
    )
    assert out == {
        "calls": [
            "stream",
            ["watch", False],
            "reply",
            ["push", "first delta"],
            ["push", " second delta"],
            "end",
            "stream",
            ["watch", False],
            "reply",
            ["push", "next segment"],
            "cancel",
        ],
        "firstRun": True,
        "afterEnd": None,
        "afterCancel": None,
    }


def test_voice_mode_off_never_starts_a_streaming_run() -> None:
    """A streaming run auto-plays.  With Voice mode off the pane must stay
    silent even when audio.tts_stream is on (the player resolves the setting
    per run, so the pane is the only place the two can be kept apart)."""
    prelude = "\n".join(
        (
            TREE_DOM,
            "const calls = [];",
            "let voiceRoles = { stt: true, tts: true };",
            "let ttsStream = null;",
            "let voiceModeOn = false;          // audio.tts_stream may be on; the pane is not listening",
            "const currentAssistantEl = null;",
            "const voiceLoop = { onReplyStarted() { calls.push('reply'); } };",
            "function _watchVoiceRun() { calls.push('watch'); }",
            "const ttsPlayer = { stream() { calls.push('stream'); return { push() {}, end() {}, cancel() {} }; } };",
            _COORDINATOR_FNS,
        )
    )
    out = _run(
        """
_feedTtsStream("a content delta");
const after = ttsStream;
_endTtsStream();
console.log(JSON.stringify({ calls, after }));
""",
        prelude=prelude,
    )
    assert out == {"calls": [], "after": None}


def test_an_interactive_pane_never_auto_plays_a_streaming_reply() -> None:
    """Worker panes have no voice session, so content deltas must not start a
    streaming run even with audio.tts_stream on: a streaming run auto-plays,
    and it was reading out every worker's reply."""
    prelude = "\n".join(
        (
            TREE_DOM,
            "const calls = [];",
            "const T = {",
            "  _voiceRoles: { stt: true, tts: true },",
            "  _ttsStream: null,",
            "  _ttsPlayer: {",
            "    stream() { calls.push('stream'); return { push() {}, end() {}, cancel() {} }; },",
            "    stop() {},",
            "  },",
            _INTERACTIVE_METHODS,
            "};",
        )
    )
    out = _run(
        """
T._streamTTS("a worker reply delta");
T._endTTSStream();
console.log(JSON.stringify({ calls, after: T._ttsStream }));
""",
        prelude=prelude,
    )
    assert out == {"calls": [], "after": None}
