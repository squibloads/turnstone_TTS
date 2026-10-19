"""The read-aloud player reports each run's audio element (``tts_player.js``).

The spectrum strip needs the element that is speaking, so it can read the
reply's output.  A run makes one ``Audio`` (a finished reply's ``playRun``, or a
streaming run's ``start``).  The player reports that element through
``onAudio(el)`` when the run makes it, and reports ``null`` when the run ends
and the element is released.  A report is the only thing added: playback is
untouched.

The shipped module runs in node against the shared player stubs, so the hook
is exercised through the real player's run lifecycle.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import demodulize, extract_braced, node_skip, run_node_source
from tests._tts_js_stubs import STUBS, TREE_DOM

pytestmark = node_skip

_ROOT = Path(__file__).resolve().parents[1] / "turnstone" / "shared_static"
_MODULE = demodulize(_ROOT / "tts_player.js")
_COPY = (_ROOT / "copy_actions.js").read_text(encoding="utf-8")
_COPY_FNS = "\n".join(
    extract_braced(_COPY, sig)
    for sig in (
        "export function findMsgActionsBar(el) {",
        "export function ensureMsgActionsBar(el) {",
    )
).replace("export function", "function")
_PANE = (_ROOT / "pane.js").read_text(encoding="utf-8")
_POPUP = extract_braced(_PANE, "export function openPopupMenu(anchor, items, opts) {").replace(
    "export function", "function"
)

_PRELUDE = "\n".join((TREE_DOM, _COPY_FNS, _POPUP, _MODULE, STUBS))


def _run(body: str) -> Any:
    src = (
        _PRELUDE
        + f"\n(async () => {{\n{body}\n}})().catch((e) => {{ console.error(e); process.exit(1); }});"
    )
    r = run_node_source(src)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_a_finished_reply_reports_its_element_while_it_plays_and_releases_it_after() -> None:
    out = _run(
        """
const seen = [];
const p = createTtsPlayer({ base: "", onAudio: (a) => seen.push(a === null ? "null" : "element") });
const btn = new El("button");
p.play(reply("A reply that plays until stopped."), btn);
await tick();
const playing = seen.slice();
p.stop();
console.log(JSON.stringify({ playing, after: seen.slice() }));
"""
    )
    assert out["playing"] == ["element"]
    assert out["after"] == ["element", "null"]


def test_a_streaming_run_reports_its_element_and_releases_it() -> None:
    out = _run(
        """
stubStream = true;
const seen = [];
const p = createTtsPlayer({ base: "", onAudio: (a) => seen.push(a === null ? "null" : "element") });
const run = p.stream({ voice: true });
run.push("A sentence that is complete. ");
await tick();
const streaming = seen.slice();
run.cancel();
await tick();
console.log(JSON.stringify({ streaming, after: seen.slice() }));
"""
    )
    assert out["streaming"] == ["element"]
    assert out["after"] == ["element", "null"]


def test_a_run_stopped_before_its_first_clip_still_releases_the_element() -> None:
    # The element is reported as soon as the run makes it, which is before any
    # clip loads.  A stop in that window must still release it, or the listener
    # keeps a reference to an element the run has already abandoned.
    out = _run(
        """
const seen = [];
const p = createTtsPlayer({ base: "", onAudio: (a) => seen.push(a === null ? "null" : "element") });
const btn = new El("button");
p.play(reply("A reply released before its first clip loads."), btn);
p.stop();                                // before the clip is fetched or loaded
await tick();
console.log(JSON.stringify({ seen }));
"""
    )
    assert out["seen"] == ["element", "null"]


def test_the_listener_can_be_attached_after_construction_and_is_then_reported_to() -> None:
    # The coordinator builds its player with a fixed constructor call, so the
    # strip's listener is attached afterwards.  It must receive reports from the
    # next run on, exactly as one passed to the constructor would.
    out = _run(
        """
const seen = [];
const p = createTtsPlayer({ base: "" });
p.setAudioListener((a) => seen.push(a === null ? "null" : "element"));
const btn = new El("button");
p.play(reply("Attached after construction."), btn);
await tick();
p.stop();
console.log(JSON.stringify({ seen }));
"""
    )
    assert out["seen"] == ["element", "null"]


def test_a_player_without_the_hook_plays_exactly_as_before() -> None:
    # The hook is optional: a player built without onAudio must still play.
    out = _run(
        """
const p = createTtsPlayer({ base: "" });
const btn = new El("button");
p.play(reply("A reply that plays with no listener."), btn);
await tick();
const playing = [p.isPlaying(), log.audios.length];
p.stop();
console.log(JSON.stringify({ playing }));
"""
    )
    assert out["playing"] == [True, 1]


def test_the_hook_reports_the_same_element_the_player_plays_on() -> None:
    # The reported element must be the one that plays, or the strip would read
    # a silent element while the reply sounds from another.
    out = _run(
        """
let reported = null;
const p = createTtsPlayer({ base: "", onAudio: (a) => { if (a) reported = a; } });
const btn = new El("button");
p.play(reply("The element reported is the one that plays."), btn);
await tick();
const same = reported !== null && reported === log.audios[0];
p.stop();
console.log(JSON.stringify({ same }));
"""
    )
    assert out["same"] is True
