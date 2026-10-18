"""Read-aloud runs report when playback has finished (``shared_static/tts_player.js``).

Voice mode is half-duplex: the coordinator closes the mic while a reply plays
and reopens it only once the audio is really over, so every run (a finished
reply's ``play()`` and a live reply's ``stream()``) exposes ``done`` — a Promise
that settles exactly once to ``{reason, heard}``.  "finished" means the reply
has no more text coming and the last queued clip's audio fired ``ended`` (or
there was nothing speakable at all); "cancelled" means a stop, a superseding
run or an error ended it first.  ``heard`` is the clips whose audio actually
played, in order.

The shipped module is evaluated in node (imports stripped, so ``authFetch``,
``Audio``, ``showToast`` ... are the stubs in ``_tts_js_stubs``), which is why
these drive the real player rather than a copy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import demodulize, node_skip, run_node_source
from tests._tts_js_stubs import STUBS, TREE_DOM

pytestmark = node_skip

_ROOT = Path(__file__).resolve().parents[1] / "turnstone" / "shared_static"
_MODULE = demodulize(_ROOT / "tts_player.js")

# ``settled`` observes a pending promise without hanging the test: null means
# "not settled yet", which is what a still-playing clip must report.
_HELPERS = """
const settled = (promise) => Promise.race([promise, tick().then(() => null)]);
"""

_PRELUDE = "\n".join((TREE_DOM, _MODULE, STUBS, _HELPERS))


def _run(body: str) -> Any:
    src = (
        _PRELUDE
        + f"\n(async () => {{\n{body}\n}})().catch((e) => {{ console.error(e); process.exit(1); }});"
    )
    r = run_node_source(src)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------
# Streaming runs
# --------------------------------------------------------------------------


def test_a_streaming_run_finishes_after_end_and_the_last_clip_ends() -> None:
    out = _run(
        """
stubStream = true;
const player = createTtsPlayer({ base: "" });
const run = player.stream();
run.push("First sentence is complete. Second sentence is complete.");
run.end();
await tick();
const before = await settled(run.done);          // text is done; clip one still plays
log.audios[0].fire("ended");
await tick();
const afterFirst = await settled(run.done);      // one clip heard, one still playing
log.audios[0].fire("ended");
const result = await run.done;
console.log(JSON.stringify({ before, afterFirst, result, heard: run.heard,
  fetched: log.fetches.filter((f) => f.method === "POST").map((f) => f.text) }));
"""
    )
    assert out["before"] is None
    assert out["afterFirst"] is None
    assert out["result"] == {
        "reason": "finished",
        "heard": ["First sentence is complete.", "Second sentence is complete."],
    }
    # Every clip was fetched and every clip was heard, in order.
    assert out["fetched"] == out["result"]["heard"]
    assert out["heard"] == out["result"]["heard"]


def test_a_stream_with_nothing_speakable_finishes_with_heard_empty() -> None:
    out = _run(
        """
stubStream = true;
const player = createTtsPlayer({ base: "" });
const empty = player.stream();
empty.end();                                     // no deltas ever arrived
const emptyResult = await empty.done;
const blank = player.stream();
blank.push("   \\n");                            // pushed text shapes to nothing
blank.end();
const blankResult = await blank.done;
console.log(JSON.stringify({ emptyResult, blankResult, emptyHeard: empty.heard,
  blankHeard: blank.heard, audios: log.audios.length }));
"""
    )
    assert out["emptyResult"] == {"reason": "finished", "heard": []}
    assert out["blankResult"] == {"reason": "finished", "heard": []}
    assert out["emptyHeard"] == []
    assert out["blankHeard"] == []
    assert out["audios"] == 0


def test_with_the_setting_off_a_stream_still_finishes_when_its_text_ends() -> None:
    # A run created while audio.tts_stream is off never fetches or plays, so
    # the voice loop must not wait on audio that will never exist.
    out = _run(
        """
stubStream = false;
const player = createTtsPlayer({ base: "" });
const run = player.stream();
run.push("A sentence that must not be spoken. Another one either.");
run.end();
const result = await run.done;
console.log(JSON.stringify({ result, heard: run.heard, audios: log.audios.length,
  posts: log.fetches.filter((f) => f.method === "POST").length }));
"""
    )
    assert out["result"] == {"reason": "finished", "heard": []}
    assert out["heard"] == []
    assert out["audios"] == 0
    assert out["posts"] == 0


def test_cancel_settles_a_streaming_run_as_cancelled() -> None:
    out = _run(
        """
stubStream = true;
const player = createTtsPlayer({ base: "" });
const run = player.stream();
run.push("First sentence is here. Second sentence is here.");
await tick();
const before = await settled(run.done);
run.cancel();
const result = await run.done;
log.audios[0].fire("ended");                     // a late ended must not rewrite the reason
await tick();
console.log(JSON.stringify({ before, result, after: await run.done, heard: run.heard }));
"""
    )
    assert out["before"] is None
    assert out["result"] == {"reason": "cancelled", "heard": []}
    assert out["after"] == out["result"]
    assert out["heard"] == []


def test_a_superseding_run_settles_the_previous_stream_as_cancelled() -> None:
    out = _run(
        """
stubStream = true;
const one = createTtsPlayer({ base: "" });
const two = createTtsPlayer({ base: "/node/n2" });
const a = one.stream();
a.push("A sentence from the first run. Another one follows.");
await tick();
const b = two.stream();                          // another pane takes the one voice slot
b.push("A sentence from the second run. Another one follows.");
await tick();
const aResult = await a.done;
const c = one.stream();                          // a new run on the same player supersedes too
c.push("A sentence from the third run. Another one follows.");
await tick();
const bResult = await b.done;
console.log(JSON.stringify({ aResult, bResult, aHeard: [...a.heard], cHeard: [...c.heard] }));
"""
    )
    assert out["aResult"] == {"reason": "cancelled", "heard": []}
    assert out["bResult"] == {"reason": "cancelled", "heard": []}
    assert out["aHeard"] == []
    assert out["cHeard"] == []


def test_a_stream_cancelled_before_its_config_probe_settles_as_cancelled() -> None:
    # The run has not reached a first clip, so it is not in _activeTtsRuns yet;
    # cancel() must still settle it rather than leave the voice loop waiting.
    out = _run(
        """
stubStream = true;
const player = createTtsPlayer({ base: "" });
const run = player.stream();
run.push("A sentence queued before the probe resolves.");
run.cancel();
const result = await run.done;
await tick();
console.log(JSON.stringify({ result, audios: log.audios.length,
  posts: log.fetches.filter((f) => f.method === "POST").length }));
"""
    )
    assert out["result"] == {"reason": "cancelled", "heard": []}
    assert out["audios"] == 0
    assert out["posts"] == 0


def test_an_empty_stream_that_ends_after_the_probe_still_finishes() -> None:
    # The other ordering of the nothing-speakable case: the probe has already
    # resolved, so the feeder exists and end() flushes it.
    out = _run(
        """
stubStream = true;
const player = createTtsPlayer({ base: "" });
const run = player.stream();
await tick();                                    // the config probe has landed
run.push("   ");
run.end();
const result = await run.done;
console.log(JSON.stringify({ result, audios: log.audios.length }));
"""
    )
    assert out["result"] == {"reason": "finished", "heard": []}
    assert out["audios"] == 0


def test_an_explicit_play_settles_a_pending_stream_as_cancelled() -> None:
    out = _run(
        """
stubStream = true;
const player = createTtsPlayer({ base: "" });
const run = player.stream();
run.push("A streamed sentence the user talked over. Another one follows.");
await tick();
player.play(reply("The reply the user explicitly chose."), new El("button"));
const result = await run.done;
console.log(JSON.stringify({ result, heard: run.heard }));
"""
    )
    assert out["result"] == {"reason": "cancelled", "heard": []}
    assert out["heard"] == []


def test_a_player_stop_settles_a_streaming_run_as_cancelled() -> None:
    # The pane calls player.stop() on reset and on disconnect, not run.cancel().
    out = _run(
        """
stubStream = true;
const player = createTtsPlayer({ base: "" });
const run = player.stream();
run.push("A sentence that is stopped by the pane. Another one follows.");
await tick();
player.stop();
const result = await run.done;
console.log(JSON.stringify({ result, paused: log.audios[0].paused, heard: run.heard }));
"""
    )
    assert out["result"] == {"reason": "cancelled", "heard": []}
    assert out["paused"] is True
    assert out["heard"] == []


def test_a_failed_clip_fetch_settles_a_streaming_run_as_cancelled() -> None:
    out = _run(
        """
stubStream = true;
ttsMode = "down";                                // the styles probe still answers
const player = createTtsPlayer({ base: "" });
const run = player.stream();
run.push("A sentence that never gets synthesized.");
run.end();                                      // flush the sentence at the buffer edge
await tick();
const result = await run.done;
console.log(JSON.stringify({ result, toasts: log.toasts }));
"""
    )
    assert out["result"] == {"reason": "cancelled", "heard": []}
    assert out["toasts"] == [["down", "error"]]


def test_a_playback_error_settles_a_streaming_run_as_cancelled() -> None:
    out = _run(
        """
stubStream = true;
const player = createTtsPlayer({ base: "" });
const run = player.stream();
run.push("A sentence whose audio cannot be decoded.");
run.end();                                      // flush the sentence at the buffer edge
await tick();
log.audios[0].fire("error");
const result = await run.done;
console.log(JSON.stringify({ result, toasts: log.toasts }));
"""
    )
    assert out["result"] == {"reason": "cancelled", "heard": []}
    assert out["toasts"] == [["Audio playback failed", "error"]]


def test_done_settles_once_and_keeps_the_first_value() -> None:
    out = _run(
        """
stubStream = true;
const player = createTtsPlayer({ base: "" });
const run = player.stream();
run.push("A single sentence is enough here.");
run.end();
await tick();
log.audios[0].fire("ended");
const first = await run.done;
run.cancel();                                    // a late cancel must not overwrite it
player.stop();                                   // nor a second stop
await tick();
const second = await run.done;
console.log(JSON.stringify({ first, second, same: first === second }));
"""
    )
    assert out["first"] == {"reason": "finished", "heard": ["A single sentence is enough here."]}
    assert out["second"] == out["first"]
    assert out["same"] is True


# --------------------------------------------------------------------------
# Finished-reply runs
# --------------------------------------------------------------------------


def _two_paragraph_reply() -> str:
    return """
const el = reply("First paragraph is read aloud.");
const body = el.querySelector(".msg-body");
const second = new El("p");
second.appendChild(new Text_("Second paragraph is read aloud."));
body.appendChild(second);
"""


def test_a_finished_reply_run_reports_finished_after_its_last_clip() -> None:
    out = _run(
        _two_paragraph_reply()
        + """
stubProfile = { pause_paragraph_ms: 0 };
const player = createTtsPlayer({ base: "" });
const btn = new El("button");
const run = player.play(el, btn);
await tick();
const playing = player.isPlaying();
log.audios[0].fire("ended");
await tick();
log.audios[0].fire("ended");
const result = await run.done;
console.log(JSON.stringify({ playing, result, heard: run.heard, audios: log.audios.length,
  fetched: log.fetches.filter((f) => f.method === "POST").map((f) => f.text),
  active: player.activeButton() }));
"""
    )
    assert out["playing"] is True
    assert out["result"] == {
        "reason": "finished",
        "heard": [
            "First paragraph is read aloud.",
            "Second paragraph is read aloud.",
        ],
    }
    assert out["heard"] == out["result"]["heard"]
    assert out["fetched"] == out["result"]["heard"]
    assert out["audios"] == 1
    assert out["active"] is None


def test_a_finished_reply_with_nothing_speakable_finishes_empty() -> None:
    out = _run(
        """
const player = createTtsPlayer({ base: "" });
const run = player.play(new El("div"), new El("button"));
const result = await run.done;
console.log(JSON.stringify({ result, heard: run.heard, playing: player.isPlaying(),
  audios: log.audios.length, posts: log.fetches.filter((f) => f.method === "POST").length }));
"""
    )
    assert out["result"] == {"reason": "finished", "heard": []}
    assert out["heard"] == []
    assert out["playing"] is False
    # The element is still made (it is the one the click's gesture unlocks),
    # but nothing is fetched and nothing is heard.
    assert out["audios"] == 1
    assert out["posts"] == 0


def test_stopping_a_finished_reply_run_settles_it_as_cancelled() -> None:
    out = _run(
        """
const player = createTtsPlayer({ base: "" });
const run = player.play(reply("A reply that is stopped midway."), new El("button"));
await tick();
const before = await settled(run.done);
player.stop();
const result = await run.done;
console.log(JSON.stringify({ before, result, heard: run.heard, playing: player.isPlaying() }));
"""
    )
    assert out["before"] is None
    assert out["result"] == {"reason": "cancelled", "heard": []}
    assert out["heard"] == []
    assert out["playing"] is False


def test_a_second_play_supersedes_the_first_as_cancelled() -> None:
    out = _run(
        """
const player = createTtsPlayer({ base: "" });
const first = player.play(reply("The first reply is being read."), new El("button"));
await tick();
const second = player.play(reply("The second reply is being read."), new El("button"));
await tick();
const firstResult = await first.done;
const secondBefore = await settled(second.done);
log.audios[log.audios.length - 1].fire("ended");   // the second run's own element
const secondResult = await second.done;
console.log(JSON.stringify({ firstResult, secondBefore, secondResult }));
"""
    )
    assert out["firstResult"] == {
        "reason": "cancelled",
        "heard": [],
    }
    assert out["secondBefore"] is None
    assert out["secondResult"] == {
        "reason": "finished",
        "heard": ["The second reply is being read."],
    }


def test_a_failed_clip_fetch_settles_a_finished_reply_run_as_cancelled() -> None:
    out = _run(
        """
fetchMode = "down";
const player = createTtsPlayer({ base: "" });
const run = player.play(reply("A reply whose clips cannot be synthesized."), new El("button"));
const result = await run.done;
console.log(JSON.stringify({ result, toasts: log.toasts }));
"""
    )
    assert out["result"] == {"reason": "cancelled", "heard": []}
    assert out["toasts"] == [["down", "error"]]
