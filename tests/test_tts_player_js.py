"""The ``shared_static/tts_player.js`` module surface.

The shipped module is evaluated in node (imports stripped, so ``authFetch``,
``Audio``, ``showToast`` ... are the stubs in ``_tts_js_stubs``).  The segmented
playback details (prefetch, caps, error paths) are pinned against the
interactive pane's delegates in ``test_tts_segments_js``; this suite covers what
a second consumer relies on: the per-base voice-role memo, the transport base,
the auth-change hook, the button contract and the finished-reply walk.
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

_PRELUDE = "\n".join((TREE_DOM, _COPY_FNS, _MODULE, STUBS))


def _run(body: str) -> Any:
    src = _PRELUDE + f"\n(async () => {{\n{body}\n}})().catch((e) => {{ console.error(e); process.exit(1); }});"
    r = run_node_source(src)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_voice_roles_are_fetched_once_per_base_from_that_bases_models() -> None:
    out = _run(
        """
fetchMode = "tts";
const [a, b] = await Promise.all([getVoiceRoles(""), getVoiceRoles("")]);
const node = await getVoiceRoles("/node/n1");
await getVoiceRoles("/node/n1");
const sameAsEmpty = (await getVoiceRoles(undefined)) === a;   // undefined and "" are one base
console.log(JSON.stringify({ a, b, node, sameAsEmpty, urls: log.fetches.map((f) => f.url) }));
"""
    )
    assert out["a"] == out["b"] == out["node"] == {"stt": True, "tts": True}
    assert out["sameAsEmpty"] is True
    assert out["urls"] == ["/v1/api/models", "/node/n1/v1/api/models"]


def test_a_failed_models_probe_resolves_to_no_roles() -> None:
    out = _run(
        """
fetchMode = "down";
console.log(JSON.stringify(await getVoiceRoles("/node/down")));
"""
    )
    assert out == {"stt": False, "tts": False}


def test_the_transport_base_prefixes_the_tts_request() -> None:
    out = _run(
        """
const reply1 = reply("A reply for the same origin."), reply2 = reply("A reply for a proxied node."),
  reply3 = reply("A reply for a late-bound base.");
const same = createTtsPlayer({ base: "" }), proxied = createTtsPlayer({ base: "/node/n7" });
let late = "/node/first";
const bound = createTtsPlayer({ base: () => late });
late = "/node/second";                              // read per request, not at construction
for (const [p, r] of [[same, reply1], [proxied, reply2], [bound, reply3]]) {
  p.play(r, new El("button")); await tick(); p.stop();
}
console.log(JSON.stringify(log.fetches.map((f) => f.url)));
"""
    )
    assert out == ["/v1/api/tts", "/node/n7/v1/api/tts", "/node/second/v1/api/tts"]


def test_the_player_reports_state_and_toggles() -> None:
    out = _run(
        """
const p = createTtsPlayer({ base: "" });
const btn = new El("button"), el = reply("A reply that plays until stopped.");
const idle = [p.isPlaying(), p.activeButton(), p.audio()];
p.play(el, btn);
const busy = [p.isPlaying(), btn.classList.contains("is-busy")];   // synchronous: before the first clip lands
await tick();
const playing = [p.isPlaying(), p.activeButton() === btn, btn.classList.contains("is-playing"), btn.attrs["aria-pressed"], p.audio() !== null, log.announce];
await p.play(el, btn);                                              // same button: toggles off
const stopped = [p.isPlaying(), p.activeButton(), p.audio(), btn.classList.contains("is-playing"), btn.attrs["aria-pressed"], log.audios[0].paused, log.revoked.length];
console.log(JSON.stringify({ idle, busy, playing, stopped }));
"""
    )
    assert out["idle"] == [False, None, None]
    assert out["busy"] == [True, True]
    assert out["playing"] == [True, True, True, "true", True, ["Playing response."]]
    assert out["stopped"] == [False, None, None, False, "false", True, 1]


def test_a_changed_auth_generation_ends_the_run_before_it_speaks() -> None:
    out = _run(
        """
let gen = 1;
const p = createTtsPlayer({ base: "", authGeneration: () => gen });
const btn = new El("button");
p.play(reply("A reply read under the old login."), btn);
gen = 2;                                            // logout / account switch mid-fetch
await tick();
console.log(JSON.stringify({ playing: btn.classList.contains("is-playing"), active: p.isPlaying(), minted: log.revoked.length, toasts: log.toasts }));
"""
    )
    assert out == {"playing": False, "active": False, "minted": 0, "toasts": []}


def test_the_default_auth_generation_is_the_auth_modules() -> None:
    out = _run(
        """
const p = createTtsPlayer({ base: "" });
const btn = new El("button");
p.play(reply("A reply read under the old login."), btn);
authGen = 5;
await tick();
console.log(JSON.stringify({ active: p.isPlaying(), playing: btn.classList.contains("is-playing") }));
"""
    )
    assert out == {"active": False, "playing": False}


def test_a_player_owns_its_own_state() -> None:
    out = _run(
        """
const a = createTtsPlayer({ base: "" }), b = createTtsPlayer({ base: "" });
const ba = new El("button"), bb = new El("button");
a.play(reply("A reply for the first pane."), ba); await tick();
b.play(reply("A reply for the second pane."), bb); await tick();
a.stop();
console.log(JSON.stringify({ a: [a.isPlaying(), ba.classList.contains("is-playing")], b: [b.isPlaying(), bb.classList.contains("is-playing")] }));
"""
    )
    assert out == {"a": [False, False], "b": [True, True]}


def test_the_speaker_button_contract() -> None:
    out = _run(
        """
const clicks = [];
const btn = buildTtsButton((b) => clicks.push(b));
btn.click();
const icon = btn.children[0];
console.log(JSON.stringify({
  cls: btn.className, title: btn.title, label: btn.attrs["aria-label"], pressed: btn.attrs["aria-pressed"],
  icon: icon.className, iconHidden: icon.attrs["aria-hidden"], clicked: clicks.length, passed: clicks[0] === btn,
}));
"""
    )
    assert out == {
        "cls": "msg-action-btn msg-tts-btn",
        "title": "Play response aloud",
        "label": "Play response aloud",
        "pressed": "false",
        "icon": "icon-speaker",
        "iconHidden": "true",
        "clicked": 1,
        "passed": True,
    }


def test_the_finished_walk_skips_the_streaming_bubble_and_the_non_replies() -> None:
    out = _run(
        """
const col = new El("div");
const mk = (cls) => { const e = new El("div"); e.className = cls; col.appendChild(e); return e; };
const first = mk("msg assistant"), user = mk("msg user"), batch = mk("conv-batch"), live = mk("msg assistant");
const played = [];
const walk = () => addTtsActionsToFinished(col, live, (el, btn) => played.push([el, btn]));
const speakers = () => [first, user, batch, live].map((e) => e.querySelector(".msg-tts-btn") !== null);
walk(); walk();                                              // idempotent
const before = speakers();
const bar = first.querySelector(".msg-actions");
const btn = bar.querySelector(".msg-tts-btn");
btn.click();
console.log(JSON.stringify({ before, bars: first.children.length, played: played.length, target: played[0][0] === first && played[0][1] === btn }));
addTtsActionsToFinished(null, null, () => {});               // a missing column is a no-op
"""
    )
    assert out == {"before": [True, False, False, False], "bars": 1, "played": 1, "target": True}


def test_speaker_goes_before_copy_and_the_walk_stops_at_an_already_served_reply() -> None:
    out = _run(
        """
const col = new El("div");
const mk = () => {
  const e = new El("div"); e.className = "msg assistant";
  const copy = new El("button"); copy.className = "msg-action-btn msg-copy-btn";
  ensureMsgActionsBar(e).appendChild(copy); col.appendChild(e); return e;
};
const [a, b, c] = [mk(), mk(), mk()];
const order = (e) => e.querySelector(".msg-actions").children.map((x) => x.className.split(" ")[1]);
addTtsActionsToFinished(col, null, () => {});
const served = order(a);
// A pre-existing speaker on b marks everything at or before it as handled:
a.querySelector(".msg-tts-btn").remove();
const d = mk();
addTtsActionsToFinished(col, null, () => {});
console.log(JSON.stringify({ served, a: order(a), b: order(b), c: order(c), d: order(d) }));
"""
    )
    assert out["served"] == ["msg-tts-btn", "msg-copy-btn"]
    assert out["a"] == ["msg-copy-btn"]  # not revisited: the walk stopped at b
    assert out["b"] == out["c"] == out["d"] == ["msg-tts-btn", "msg-copy-btn"]


def test_pure_extraction_reads_a_reply_as_pausing_clips() -> None:
    out = _run(
        """
const body = new El("div");
const h = new El("h2"); h.appendChild(new Text_("A heading for the reply"));
const p = new El("p"); p.appendChild(new Text_("A paragraph that is long enough."));
body.appendChild(h); body.appendChild(p);
console.log(JSON.stringify({
  segs: extractSpeechSegments(body),
  shaped: shapeSpeechSegments([{ text: "no  punctuation   here but long enough", pauseMs: 500 }]),
}));
"""
    )
    assert out["segs"] == [
        {"text": "A heading for the reply.", "pauseMs": 600},
        {"text": "A paragraph that is long enough.", "pauseMs": 0},
    ]
    assert out["shaped"] == [{"text": "no punctuation here but long enough.", "pauseMs": 0}]
