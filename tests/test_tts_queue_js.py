"""Queueing voice-mode replies (``shared_static/tts_player.js``).

A voice session can receive a second reply while the first is still being read
aloud.  ``stream({voice: true})`` runs must not cut the first one off: they wait
in arrival order, keep accumulating their own text, and start when the run
ahead of them finishes naturally.  A speaker-button press or ``stop()`` is the
user taking over, so those still supersede everything, queued runs included.
Non-voice streaming (the interactive pane's "read every message") keeps the old
one-run-per-player supersede behaviour.

The shipped module is evaluated in node (imports stripped, so ``authFetch``,
``Audio``, ``showToast`` ... are the stubs in ``_tts_js_stubs``), which is why
these drive the real player rather than a copy.  The coordinator's use of the
player's ``idle()`` hook is covered in ``test_tts_coordinator_js``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import demodulize, node_skip, run_node_source
from tests._tts_js_stubs import STUBS, TREE_DOM

pytestmark = node_skip

_TURNSTONE = Path(__file__).resolve().parents[1] / "turnstone"
_ROOT = _TURNSTONE / "shared_static"
_MODULE = demodulize(_ROOT / "tts_player.js")

# ``settled`` observes a pending promise without hanging the test: null means
# "not settled yet", which a queued run must report.
_HELPERS = """
const settled = (promise) => Promise.race([promise, tick().then(() => null)]);
"""

_PRELUDE = "\n".join((TREE_DOM, _MODULE, STUBS, _HELPERS))


def _run(body: str, prelude: str = _PRELUDE) -> Any:
    src = (
        prelude
        + f"\n(async () => {{\n{body}\n}})().catch((e) => {{ console.error(e); process.exit(1); }});"
    )
    r = run_node_source(src)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def _posts() -> str:
    return 'log.fetches.filter((f) => f.method === "POST").map((f) => f.text)'


# --------------------------------------------------------------------------
# Voice runs queue
# --------------------------------------------------------------------------


def test_a_second_voice_run_waits_for_the_first_to_finish() -> None:
    out = _run(
        f"""
stubStream = true;
const player = createTtsPlayer({{ base: "" }});
const order = [];
const first = player.stream({{ voice: true }});
first.done.then((r) => order.push("first:" + r.reason));
first.push("First reply sentence one. First reply sentence two.");
first.end();
await tick();
// The first reply is playing its first clip when the second arrives.  It must
// wait rather than cut the first off mid-sentence, and it must keep taking its
// own deltas while it waits.
const second = player.stream({{ voice: true }});
second.done.then((r) => order.push("second:" + r.reason));
second.push("Second reply sentence one. Second reply sentence two.");
second.end();
await tick();
const queued = {{
  audios: log.audios.length,
  posts: {_posts()},
  firstHeard: [...first.heard],
  secondHeard: [...second.heard],
  secondSettled: await settled(second.done),
  idle: await settled(player.idle()),
}};
// Clip one of the first reply ends; clip two still has to play before the
// queued reply may start.
log.audios[0].fire("ended");
await tick();
const between = {{
  audios: log.audios.length,
  firstHeard: [...first.heard],
  secondHeard: [...second.heard],
  firstSettled: await settled(first.done),
}};
log.audios[0].fire("ended");
await tick();
const firstResult = await first.done;
const afterFirst = {{
  audios: log.audios.length,
  posts: {_posts()},
  secondHeard: [...second.heard],
  idle: await settled(player.idle()),
}};
log.audios[1].fire("ended");
await tick();
log.audios[1].fire("ended");
await tick();
const secondResult = await second.done;
console.log(JSON.stringify({{ queued, between, firstResult, afterFirst, secondResult,
  order, idleAfter: await settled(player.idle().then(() => "idle")) }}));
"""
    )
    # Nothing of the second run is fetched or played while it waits.
    assert out["queued"]["audios"] == 1
    assert out["queued"]["posts"] == [
        "First reply sentence one.",
        "First reply sentence two.",
    ]
    assert out["queued"]["firstHeard"] == []
    assert out["queued"]["secondHeard"] == []
    assert out["queued"]["secondSettled"] is None
    assert out["queued"]["idle"] is None
    # Finishing the first clip completes neither run.
    assert out["between"]["audios"] == 1
    assert out["between"]["firstHeard"] == ["First reply sentence one."]
    assert out["between"]["secondHeard"] == []
    assert out["between"]["firstSettled"] is None
    assert out["firstResult"] == {
        "reason": "finished",
        "heard": ["First reply sentence one.", "First reply sentence two."],
    }
    # The first run's last clip ending starts the queued run, not the mic.
    assert out["afterFirst"]["audios"] == 2
    assert out["afterFirst"]["secondHeard"] == []
    assert out["afterFirst"]["idle"] is None
    assert out["afterFirst"]["posts"] == [
        "First reply sentence one.",
        "First reply sentence two.",
        "Second reply sentence one.",
        "Second reply sentence two.",
    ]
    assert out["secondResult"] == {
        "reason": "finished",
        "heard": ["Second reply sentence one.", "Second reply sentence two."],
    }
    assert out["order"] == ["first:finished", "second:finished"]
    assert out["idleAfter"] == "idle"


def test_the_queue_keeps_three_waiting_runs_and_drops_the_oldest() -> None:
    out = _run(
        f"""
stubStream = true;
const player = createTtsPlayer({{ base: "" }});
const active = player.stream({{ voice: true }});
active.push("Active reply sentence one. Active reply sentence two.");
active.end();
await tick();
const waiting = [];
for (let n = 1; n <= 4; n++) {{
  const run = player.stream({{ voice: true }});
  run.push("Queued reply " + n + " sentence one. Queued reply " + n + " sentence two.");
  run.end();
  waiting.push(run);
}}
await tick();
const dropped = await waiting[0].done;
const stillWaiting = [];
for (let i = 1; i < waiting.length; i++) stillWaiting.push(await settled(waiting[i].done));
// Drain the active run and then each survivor, in order.
for (let i = 0; i <= 3; i++) {{
  log.audios[i].fire("ended");
  await tick();
  log.audios[i].fire("ended");
  await tick();
}}
const results = [];
for (const run of waiting) results.push(await run.done);
console.log(JSON.stringify({{ dropped, stillWaiting, results,
  audios: log.audios.length, posts: {_posts()} }}));
"""
    )
    # The oldest WAITING run is the one dropped when the cap is reached; the
    # run playing at the time is untouched.
    assert out["dropped"] == {"reason": "cancelled", "heard": []}
    assert out["stillWaiting"] == [None, None, None]
    assert out["audios"] == 4  # active + the three survivors
    assert out["results"] == [
        {"reason": "cancelled", "heard": []},
        {
            "reason": "finished",
            "heard": ["Queued reply 2 sentence one.", "Queued reply 2 sentence two."],
        },
        {
            "reason": "finished",
            "heard": ["Queued reply 3 sentence one.", "Queued reply 3 sentence two."],
        },
        {
            "reason": "finished",
            "heard": ["Queued reply 4 sentence one.", "Queued reply 4 sentence two."],
        },
    ]
    assert "Queued reply 1 sentence one." not in out["posts"]
    assert out["posts"][-2:] == [
        "Queued reply 4 sentence one.",
        "Queued reply 4 sentence two.",
    ]


# --------------------------------------------------------------------------
# The user still supersedes
# --------------------------------------------------------------------------


def test_a_speaker_press_stops_the_current_run_and_empties_the_queue() -> None:
    out = _run(
        """
stubStream = true;
const player = createTtsPlayer({ base: "" });
const first = player.stream({ voice: true });
first.push("First voice sentence one. First voice sentence two.");
first.end();
await tick();
const queued = player.stream({ voice: true });
queued.push("Queued voice sentence one. Queued voice sentence two.");
queued.end();
await tick();
const played = player.play(
  reply("The chosen reply is long enough to be read aloud."),
  new El("button"),
);
await tick();
const during = {
  first: await first.done,
  queued: await queued.done,
  playing: player.isPlaying(),
  audios: log.audios.length,
  paused: log.audios[0].paused,
  posts: log.fetches.filter((f) => f.method === "POST").map((f) => f.text),
};
log.audios[log.audios.length - 1].fire("ended");
await tick();
const chosen = await played.done;
console.log(JSON.stringify({ during, chosen, idle: await settled(player.idle().then(() => "idle")) }));
"""
    )
    assert out["during"]["first"] == {"reason": "cancelled", "heard": []}
    assert out["during"]["queued"] == {"reason": "cancelled", "heard": []}
    assert out["during"]["playing"] is True
    assert out["during"]["paused"] is True
    # The queued clip was never synthesized: only the first run's in-flight
    # clip and the chosen reply were posted.
    assert "Queued voice sentence one." not in out["during"]["posts"]
    assert out["during"]["posts"][-1] == ("The chosen reply is long enough to be read aloud.")
    assert out["chosen"] == {
        "reason": "finished",
        "heard": ["The chosen reply is long enough to be read aloud."],
    }


def test_idle_stays_pending_while_a_speaker_press_starts_its_reply() -> None:
    # play() must claim the player before it tears the superseded stream down,
    # or idle() observes a quiet instant and lets the voice loop reopen the mic
    # under the reply the user just chose.
    out = _run(
        """
stubStream = true;
const player = createTtsPlayer({ base: "" });
const voice = player.stream({ voice: true });
voice.push("A sentence that the press stops. Another one follows.");
await tick();
let idle = false;
const idleDone = player.idle().then(() => { idle = true; });
const played = player.play(
  reply("The chosen reply is long enough to be read aloud."),
  new El("button"),
);
await tick();
const during = idle;
log.audios[log.audios.length - 1].fire("ended");
await tick();
await idleDone;
console.log(JSON.stringify({ during, after: idle, cancelled: await voice.done,
  chosen: await played.done }));
"""
    )
    assert out["during"] is False
    assert out["after"] is True
    assert out["cancelled"] == {"reason": "cancelled", "heard": []}
    assert out["chosen"] == {
        "reason": "finished",
        "heard": ["The chosen reply is long enough to be read aloud."],
    }


def test_stop_empties_the_queue_as_well_as_the_playing_run() -> None:
    out = _run(
        """
stubStream = true;
const player = createTtsPlayer({ base: "" });
const first = player.stream({ voice: true });
first.push("First voice sentence one. First voice sentence two.");
first.end();
await tick();
const queued = player.stream({ voice: true });
queued.push("Queued voice sentence one. Queued voice sentence two.");
queued.end();
await tick();
player.stop();
const result = {
  first: await first.done,
  queued: await queued.done,
  paused: log.audios[0].paused,
  idle: await settled(player.idle().then(() => "idle")),
};
console.log(JSON.stringify(result));
"""
    )
    assert out["first"] == {"reason": "cancelled", "heard": []}
    assert out["queued"] == {"reason": "cancelled", "heard": []}
    assert out["paused"] is True
    assert out["idle"] == "idle"


def test_a_voice_run_with_no_audio_does_not_block_the_runs_behind_it() -> None:
    # audio.tts_stream is off on this deployment, so every voice stream settles
    # "finished" without speaking (the pane falls back to the finished-reply
    # path).  A silent run still holds the voice slot, and must release it
    # rather than leave the next reply queued forever.
    out = _run(
        """
stubStream = false;
const player = createTtsPlayer({ base: "" });
const first = player.stream({ voice: true });
first.push("First reply is not streamed.");
first.end();
await tick();
const second = player.stream({ voice: true });
second.push("Second reply is not streamed.");
second.end();
const results = [await first.done, await second.done];
console.log(JSON.stringify({ results, audios: log.audios.length,
  posts: log.fetches.filter((f) => f.method === "POST").length,
  idle: await settled(player.idle().then(() => "idle")) }));
"""
    )
    assert out["results"] == [
        {"reason": "finished", "heard": []},
        {"reason": "finished", "heard": []},
    ]
    assert out["audios"] == 0
    assert out["posts"] == 0
    assert out["idle"] == "idle"


def test_a_non_voice_stream_still_supersedes_voice_runs() -> None:
    # The interactive pane's "read every message" stream did not become a
    # queue: it still owns the one voice slot immediately.
    out = _run(
        """
stubStream = true;
const player = createTtsPlayer({ base: "" });
const voice = player.stream({ voice: true });
voice.push("Voice sentence one is here. Voice sentence two is here.");
voice.end();
await tick();
const queued = player.stream({ voice: true });
queued.push("Queued sentence one is here. Queued sentence two is here.");
queued.end();
await tick();
const plain = player.stream();
plain.push("Plain sentence one is here. Plain sentence two is here.");
plain.end();
await tick();
console.log(JSON.stringify({ voice: await voice.done, queued: await queued.done,
  plainHeard: [...plain.heard], audios: log.audios.length, paused: log.audios[0].paused,
  posts: log.fetches.filter((f) => f.method === "POST").map((f) => f.text) }));
"""
    )
    assert out["voice"] == {"reason": "cancelled", "heard": []}
    assert out["queued"] == {"reason": "cancelled", "heard": []}
    assert out["audios"] == 2
    assert out["paused"] is True
    assert out["posts"][-2:] == [
        "Plain sentence one is here.",
        "Plain sentence two is here.",
    ]
    assert all("Queued sentence" not in post for post in out["posts"])
