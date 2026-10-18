"""The ``shared_static/tts_player.js`` module surface.

The shipped module is evaluated in node (imports stripped, so ``authFetch``,
``Audio``, ``showToast`` ... are the stubs in ``_tts_js_stubs``).  The segmented
playback details (prefetch, caps, error paths) are pinned against the
interactive pane's delegates in ``test_tts_segments_js``; this suite covers what
a second consumer relies on: the per-base voice-role memo, the transport base,
the auth-change hook, the button contract, the finished-reply walk and the
speaking-style picker (including the request body it produces).
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

# The picker rides the shared popup-menu chrome and keyboard handling from
# pane.js.  Sliced rather than imported so the harness keeps one shared
# context and the test drives the shipped helper, not a copy.
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
console.log(JSON.stringify(log.fetches.filter((f) => f.method === "POST").map((f) => f.url)));
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


def test_playing_in_one_pane_stops_every_other_pane() -> None:
    out = _run(
        """
const a = createTtsPlayer({ base: "" }), b = createTtsPlayer({ base: "/node/n2" });
const btnA = new El("button"), btnB = new El("button");
a.play(reply("The first session's reply, still reading."), btnA);
await tick();
const before = [a.isPlaying(), btnA.classList.contains("is-playing")];
b.play(reply("The second session's reply starts now."), btnB);
const handover = [a.isPlaying(), btnA.classList.contains("is-playing"), btnA.attrs["aria-pressed"], log.audios[0].paused];
await tick();
const after = [b.isPlaying(), btnB.classList.contains("is-playing")];
b.stop();
a.play(reply("Back to the first session."), btnA);      // a stopped player can start again
await tick();
const again = [a.isPlaying(), b.isPlaying()];
a.stop();
console.log(JSON.stringify({ before, handover, after, again }));
"""
    )
    assert out["before"] == [True, True]
    assert out["handover"] == [False, False, "false", True]
    assert out["after"] == [True, True]
    assert out["again"] == [True, False]


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


# -- speaking-style picker --------------------------------------------------
# The styles probe mirrors the TTS POST's transport base, is memoized per base
# and fails closed: a failed or unusable response means no picker and no style
# on the wire, never an error surface.


def test_speaking_styles_are_fetched_once_per_base_and_fail_closed() -> None:
    out = _run(
        """
stylesMode = "ok";
const same = [await getTtsStyles(""), await getTtsStyles(""), await getTtsStyles(undefined)];
const node = await getTtsStyles("/node/n1");
stylesMode = "down";
const down = await getTtsStyles("/node/down");
console.log(JSON.stringify({
  same: same[0] === same[1] && same[1] === same[2],
  config: same[0],
  node,
  down,
  urls: log.fetches.filter((f) => f.url.endsWith("/tts/styles")).map((f) => f.url),
}));
"""
    )
    assert out["same"] is True  # one fetch per base, however many panes ask
    assert out["down"] is None
    assert out["config"] == {
        "styles": [
            {"name": "Professional", "in_menu": True},
            {"name": "Social", "in_menu": True},
            {"name": "Plain", "in_menu": True},
            {"name": "Hidden", "in_menu": False},
        ],
        "default": "Professional",
    }
    assert out["node"] == out["config"]
    assert out["urls"] == [
        "/v1/api/tts/styles",
        "/node/n1/v1/api/tts/styles",
        "/node/down/v1/api/tts/styles",
    ]


def test_the_style_button_follows_tts_and_shows_the_chosen_style() -> None:
    out = _run(
        """
stylesMode = "ok";
localStorage.setItem("turnstone.tts.style", "social");   // casing is the stored name, not the menu's
const el = reply("A reply with a speaker and a style picker.");
const bar = ensureMsgActionsBar(el);
const copy = new El("button"); copy.className = "msg-action-btn msg-copy-btn"; bar.appendChild(copy);
const retry = new El("button"); retry.className = "msg-action-btn msg-retry-btn";
bar.insertBefore(retry, bar.firstChild);
addTtsAction(el, () => {});
await tick();
const btn = bar.querySelector(".msg-style-btn");
console.log(JSON.stringify({
  order: bar.children.map((x) => x.className.split(" ").find((c) => /^msg-(retry|tts|style|copy)-btn$/.test(c))),
  label: btn.querySelector(".msg-style-label").textContent,
  title: btn.title,
  aria: btn.attrs["aria-label"],
  popup: btn.attrs["aria-haspopup"],
  expanded: btn.attrs["aria-expanded"],
  caret: btn.querySelector(".msg-style-caret").attrs["aria-hidden"],
}));
"""
    )
    assert out == {
        "order": [
            "msg-retry-btn",
            "msg-tts-btn",
            "msg-style-btn",
            "msg-copy-btn",
        ],
        "label": "Social",
        "title": "Speaking style: Social",
        "aria": "Speaking style: Social",
        "popup": "menu",
        "expanded": "false",
        "caret": "true",
    }


def test_the_style_menu_is_a_radio_menu_and_escape_returns_focus() -> None:
    out = _run(
        """
stylesMode = "ok";
localStorage.setItem("turnstone.tts.style", "Social");
const el = reply("A reply that offers three speaking styles.");
document.body.append(el);                                  // openPopupMenu needs a connected anchor
addTtsAction(el, () => {});
await tick();
const btn = el.querySelector(".msg-style-btn");
const bar = el.querySelector(".msg-actions");
btn.click();
const menu = document.body.querySelector(".tab-menu");
const items = menu.querySelectorAll(".tab-menu-item");
const snap = {
  expanded: btn.attrs["aria-expanded"],
  barPinned: bar.classList.contains("is-style-menu-open"),
  role: menu.attrs.role,
  label: menu.attrs["aria-label"],
  itemRoles: items.map((i) => i.attrs.role),
  checked: items.map((i) => i.attrs["aria-checked"]),
  labels: items.map((i) => i.querySelector(".tab-menu-label").textContent),
  keys: items.map((i) => { const k = i.querySelector(".tab-menu-key"); return k ? k.textContent : ""; }),
  focusedChecked: document.activeElement === items[2],
};
fireDocument("keydown", { key: "Escape", preventDefault() {} });
console.log(JSON.stringify({
  snap,
  closed: document.body.querySelector(".tab-menu") === null,
  expanded: btn.attrs["aria-expanded"],
  barPinned: bar.classList.contains("is-style-menu-open"),
  focus: document.activeElement === btn,
}));
"""
    )
    assert out["snap"] == {
        "expanded": "true",
        "barPinned": True,
        "role": "menu",
        "label": "Speaking style",
        "itemRoles": ["menuitemradio"] * 4,
        "checked": ["false", "false", "true", "false"],
        "labels": [
            "Default (Professional)",
            "Professional",
            "Social",
            "Plain",
        ],
        "keys": ["", "", "✓", ""],
        "focusedChecked": True,
    }
    assert out["closed"] is True
    assert out["expanded"] == "false"
    assert out["barPinned"] is False
    assert out["focus"] is True


def test_choosing_a_style_stores_it_and_paints_every_picker() -> None:
    out = _run(
        """
stylesMode = "ok";
const a = reply("The first reply."), b = reply("The second reply.");
document.body.append(a); document.body.append(b);
addTtsAction(a, () => {}); addTtsAction(b, () => {});
await tick();
const ba = a.querySelector(".msg-style-btn");
ba.click();
document.body.querySelectorAll(".tab-menu-item")[3].click();   // Plain
const closedAfter = document.body.querySelector(".tab-menu") === null;
const focusAfter = document.activeElement === ba;
const labels = [a, b].map((el) => el.querySelector(".msg-style-label").textContent);
ba.click();                                                    // reopen: the check moved
console.log(JSON.stringify({
  stored: localStorage.getItem("turnstone.tts.style"),
  labels,
  checked: document.body.querySelectorAll(".tab-menu-item").map((i) => i.attrs["aria-checked"]),
  closedAfter,
  focusAfter,
}));
"""
    )
    # The second picker repaints too: one browser-wide choice, every reply.
    assert out["stored"] == "Plain"
    assert out["labels"] == ["Plain", "Plain"]
    assert out["checked"] == ["false", "false", "false", "true"]
    assert out["closedAfter"] is True
    assert out["focusAfter"] is True


def test_a_stored_style_no_longer_offered_falls_back_to_default() -> None:
    out = _run(
        """
stylesMode = "ok";
localStorage.setItem("turnstone.tts.style", "Gone");
const el = reply("A reply whose saved style was removed.");
document.body.append(el);
addTtsAction(el, () => {});
await tick();
const btn = el.querySelector(".msg-style-btn");
btn.click();
console.log(JSON.stringify({
  label: btn.querySelector(".msg-style-label").textContent,
  checked: document.body.querySelectorAll(".tab-menu-item").map((i) => i.attrs["aria-checked"]),
}));
"""
    )
    assert out == {"label": "Default", "checked": ["true", "false", "false", "false"]}


def test_the_picker_is_hidden_when_unsupported_unavailable_or_empty() -> None:
    out = _run(
        """
stylesMode = "off";
const unsupported = reply("A base whose TTS model takes no instructions.");
addTtsAction(unsupported, () => {}, "");
stylesMode = "down";
const failed = reply("A base whose styles probe fails.");
addTtsAction(failed, () => {}, "/node/down");
stylesMode = "empty";
const empty = reply("A base with no style in the menu.");
addTtsAction(empty, () => {}, () => "/node/empty");   // a function base, as the interactive pane passes it
await tick();
console.log(JSON.stringify({
  tts: [unsupported, failed, empty].map((el) => !!el.querySelector(".msg-tts-btn")),
  style: [unsupported, failed, empty].map((el) => !!el.querySelector(".msg-style-btn")),
  urls: log.fetches.filter((f) => f.url.endsWith("/tts/styles")).map((f) => f.url),
}));
"""
    )
    assert out["tts"] == [True, True, True]
    assert out["style"] == [False, False, False]
    assert out["urls"] == [
        "/v1/api/tts/styles",
        "/node/down/v1/api/tts/styles",
        "/node/empty/v1/api/tts/styles",
    ]


# -- the style on the wire --------------------------------------------------


def _two_paragraph_reply_js() -> str:
    return (
        'const el = reply("First paragraph long enough.");\n'
        'const p2 = new El("p"); p2.appendChild(new Text_("Second paragraph long enough."));\n'
        'el.querySelector(".msg-body").appendChild(p2);\n'
    )


def test_default_read_aloud_body_is_byte_identical_to_the_pre_styles_request() -> None:
    out = _run(
        """
stylesMode = "ok";                                  // supported, but nothing chosen
"""
        + _two_paragraph_reply_js()
        + """
const player = createTtsPlayer({ base: "" });
player.play(el, new El("button"));
await tick();
const posts = log.fetches.filter((f) => f.url === "/v1/api/tts");
console.log(JSON.stringify({ bodies: posts.map((f) => f.body), raws: posts.map((f) => f.raw) }));
"""
    )
    assert out["bodies"] == [
        {"text": "First paragraph long enough."},
        {"text": "Second paragraph long enough."},
    ]
    # The exact bytes a client without style support sends: no style field at all.
    assert out["raws"] == [
        json.dumps({"text": "First paragraph long enough."}, separators=(",", ":")),
        json.dumps({"text": "Second paragraph long enough."}, separators=(",", ":")),
    ]


def test_a_chosen_style_rides_every_segment_request() -> None:
    out = _run(
        """
stylesMode = "ok";
localStorage.setItem("turnstone.tts.style", "social");   // canonicalised from the menu list
"""
        + _two_paragraph_reply_js()
        + """
const player = createTtsPlayer({ base: "" });
player.play(el, new El("button"));
await tick();
const posts = log.fetches.filter((f) => f.url === "/v1/api/tts");
console.log(JSON.stringify(posts.map((f) => f.body)));
"""
    )
    assert out == [
        {"text": "First paragraph long enough.", "style": "Social"},
        {"text": "Second paragraph long enough.", "style": "Social"},
    ]


def test_a_stale_or_unsupported_style_is_omitted_from_the_request() -> None:
    out = _run(
        """
stylesMode = "ok";
localStorage.setItem("turnstone.tts.style", "Gone");     // no longer in the menu
const stale = reply("A reply whose saved style was removed.");
const p1 = createTtsPlayer({ base: "" });
p1.play(stale, new El("button"));
await tick();
p1.stop();
stylesMode = "off";                                      // model takes no instructions
localStorage.setItem("turnstone.tts.style", "Social");
const unsupported = reply("A reply on a base without instruction support.");
const p2 = createTtsPlayer({ base: "/node/off" });
p2.play(unsupported, new El("button"));
await tick();
console.log(JSON.stringify(log.fetches.filter((f) => f.method === "POST").map((f) => f.body)));
"""
    )
    assert out == [
        {"text": "A reply whose saved style was removed."},
        {"text": "A reply on a base without instruction support."},
    ]


def test_a_failed_styles_probe_still_reads_aloud_with_the_default_body() -> None:
    out = _run(
        """
stylesMode = "down";
localStorage.setItem("turnstone.tts.style", "Social");
const el = reply("A reply on a base whose styles probe fails.");
const player = createTtsPlayer({ base: "/node/down" });
player.play(el, new El("button"));
await tick();
const posts = log.fetches.filter((f) => f.method === "POST");
console.log(JSON.stringify(posts.map((f) => f.body)));
"""
    )
    # A failed probe is silent: no picker, and the read-aloud still works with
    # the server default (no style field).
    assert out == [{"text": "A reply on a base whose styles probe fails."}]


def test_storage_failures_never_break_the_picker() -> None:
    out = _run(
        """
stylesMode = "ok";
storageGetThrows = true;                                  // private mode / sandboxed frame
const el = reply("A reply whose storage is blocked.");
document.body.append(el);
addTtsAction(el, () => {}, "/node/blocked");
await tick();
const btn = el.querySelector(".msg-style-btn");
const first = btn.querySelector(".msg-style-label").textContent;
storageGetThrows = false;
storageSetThrows = true;
btn.click();
document.body.querySelectorAll(".tab-menu-item")[2].click();   // Social: the write throws
console.log(JSON.stringify({
  first,
  after: btn.querySelector(".msg-style-label").textContent,
  stored: localStorage.getItem("turnstone.tts.style"),
}));
"""
    )
    # The read fallback is Default; a write that throws still keeps the in-page
    # choice (it just will not survive a reload).
    assert out == {"first": "Default", "after": "Social", "stored": None}


# -- playback rate (admin setting audio.tts_playback_rate) -------------------
# The rate is a deployment-wide admin choice, applied in the browser so it
# works for every TTS model.  It must ride every clip: a browser resets an
# element's playbackRate to defaultPlaybackRate when a new src loads.


def test_the_admin_playback_rate_applies_to_every_clip_without_styles() -> None:
    out = _run(
        """
stubRate = 1.25;                                   // instructions unsupported ("off")
"""
        + _two_paragraph_reply_js()
        + """
const player = createTtsPlayer({ base: "" });
player.play(el, new El("button"));
await tick();
const a = log.audios[0];
const first = [a.defaultPlaybackRate, a.playbackRate, a.preservesPitch];
a.playbackRate = 1;                                 // what a browser does on a new src
a.fire("ended");
await new Promise((r) => setTimeout(r, 1200));     // the real pause between paragraphs
await tick();
const second = [a.defaultPlaybackRate, a.playbackRate];
console.log(JSON.stringify({ first, second }));
"""
    )
    assert out["first"] == [1.25, 1.25, True]
    # Forced back to 1 between clips (as a browser does on a new src); only
    # the second clip's own load can have restored it.
    assert out["second"] == [1.25, 1.25]


def test_an_unusable_or_missing_playback_rate_plays_at_normal_speed() -> None:
    out = _run(
        """
const rates = [];
for (const [mode, rate] of [["off", 7], ["off", "fast"], ["off", undefined], ["down", 1.5]]) {
  stylesMode = mode; stubRate = rate;
  const player = createTtsPlayer({ base: "/node/" + rates.length });
  player.play(reply("A reply long enough to be read."), new El("button"));
  await tick();
  rates.push(log.audios[log.audios.length - 1].playbackRate);
  player.stop();
}
console.log(JSON.stringify(rates));
"""
    )
    assert out == [1, 1, 1, 1]


# -- read-aloud profile (audio.tts_profiles) --------------------------------
# The profile rides the styles probe.  It replaces every pacing/structure
# constant the player used to bake in, field by field: a missing, malformed or
# out-of-range field reads as the built-in default, so a model with no profile
# sounds exactly as it did before profiles existed.

_DEFAULT_PROFILE = {
    "pause_heading_ms": 600,
    "pause_paragraph_ms": 500,
    "pause_table_row_ms": 150,
    "pause_code_ms": 400,
    "pause_list_item_ms": 0,
    "table_cell_joiner": "comma",
    "table_header_mode": "row",
    "inline_code_max_chars": 40,
    "code_marker": "Code omitted.",
    "diagram_marker": "Diagram omitted.",
    "clip_min_chars": 20,
    "clip_max_chars": 600,
    "max_clips": 200,
}


def test_the_profile_rides_the_styles_probe_once_per_base() -> None:
    out = _run(
        """
stubProfile = { pause_paragraph_ms: 250, table_cell_joiner: "semicolon" };
const same = await Promise.all([getTtsProfile(""), getTtsProfile(""), getTtsStyles("")]);
stubProfile = undefined;                            // a server that predates profiles
const missing = await getTtsProfile("/node/plain");
stylesMode = "down";                                // a failed probe is the defaults
const down = await getTtsProfile("/node/down");
console.log(JSON.stringify({
  profile: same[0], missing, down,
  urls: log.fetches.filter((f) => f.url.endsWith("/tts/styles")).map((f) => f.url),
}));
"""
    )
    assert out["profile"] == {
        **_DEFAULT_PROFILE,
        "pause_paragraph_ms": 250,
        "table_cell_joiner": "semicolon",
    }
    assert out["missing"] == _DEFAULT_PROFILE
    assert out["down"] == _DEFAULT_PROFILE
    # Three panes asking share one probe per base, exactly like the styles and rate.
    assert out["urls"] == [
        "/v1/api/tts/styles",
        "/node/plain/v1/api/tts/styles",
        "/node/down/v1/api/tts/styles",
    ]


def test_a_malformed_profile_field_reads_as_its_default() -> None:
    out = _run(
        """
stubProfile = {
  pause_heading_ms: 9000,              // above the 0-5000 bound
  pause_paragraph_ms: true,            // bool is not a whole number
  pause_table_row_ms: 40.5,            // not a whole number
  pause_code_ms: 750,                  // valid
  pause_list_item_ms: -1,              // below the 0 bound
  table_cell_joiner: "tab",            // not one of comma/semicolon/pause
  table_header_mode: "prefix",         // valid
  inline_code_max_chars: "40",         // not a number
  code_marker: "  Silence.  ",         // valid, trimmed like the server does
  diagram_marker: "x".repeat(61),      // longer than MAX_MARKER_CHARS
  clip_min_chars: 150,
  clip_max_chars: 120,                 // inverted pair: both read as defaults
  max_clips: 0,                        // below the 1 bound
};
const bad = await getTtsProfile("");
stubProfile = "not an object";
const notObject = await getTtsProfile("/node/string");
stubProfile = [];
const list = await getTtsProfile("/node/list");
console.log(JSON.stringify({ bad, notObject, list }));
"""
    )
    assert out["bad"] == {
        **_DEFAULT_PROFILE,
        "pause_code_ms": 750,
        "table_header_mode": "prefix",
        "code_marker": "Silence.",
    }
    assert out["notObject"] == _DEFAULT_PROFILE
    assert out["list"] == _DEFAULT_PROFILE


def test_the_profile_sets_every_block_pause() -> None:
    out = _run(
        """
const body = new El("div");
const add = (tag, text) => {
  const n = new El(tag); n.appendChild(new Text_(text)); body.appendChild(n);
};
add("h2", "A heading for the reply");
add("p", "A paragraph that is long enough.");
add("blockquote", "A quoted sentence of some length.");
const table = new El("table");
for (const [a, b] of [["alpha first", "beta second"], ["gamma third", "delta fourth"]]) {
  const tr = new El("tr");
  for (const text of [a, b]) {
    const td = new El("td"); td.appendChild(new Text_(text)); tr.appendChild(td);
  }
  table.appendChild(tr);
}
body.appendChild(table);
console.log(JSON.stringify(extractSpeechSegments(body, {
  pause_heading_ms: 900, pause_paragraph_ms: 250, pause_table_row_ms: 40,
})));
"""
    )
    assert out == [
        {"text": "A heading for the reply.", "pauseMs": 900},
        {"text": "A paragraph that is long enough.", "pauseMs": 250},
        {"text": "A quoted sentence of some length.", "pauseMs": 250},
        {"text": "alpha first, beta second.", "pauseMs": 40},
        {"text": "gamma third, delta fourth.", "pauseMs": 0},
    ]


def test_markers_and_the_marker_pause_come_from_the_profile() -> None:
    out = _run(
        """
const body = new El("div");
const add = (tag, text) => {
  const n = new El(tag); n.appendChild(new Text_(text)); body.appendChild(n);
};
add("p", "Before the code block.");
add("pre", "ls -la");
const d = new El("div"); d.className = "mermaid-container mermaid-loading";
body.appendChild(d);
add("p", "After the diagram.");
console.log(JSON.stringify(extractSpeechSegments(body, {
  pause_code_ms: 750, code_marker: "Code skipped.", diagram_marker: "Diagram skipped.",
})));
"""
    )
    assert out == [
        {"text": "Before the code block.", "pauseMs": 500},
        {"text": "Code skipped.", "pauseMs": 750},
        {"text": "Diagram skipped.", "pauseMs": 750},
        {"text": "After the diagram.", "pauseMs": 0},
    ]


def test_an_empty_marker_skips_the_element_silently() -> None:
    out = _run(
        """
const body = new El("div");
const add = (tag, text) => {
  const n = new El(tag); n.appendChild(new Text_(text)); body.appendChild(n);
};
add("p", "Before the code block.");
add("pre", "ls -la");
const d = new El("div"); d.className = "mermaid-container mermaid-loading";
body.appendChild(d);
add("p", "After the diagram.");
const only = new El("div");
const lonely = new El("div"); lonely.className = "mermaid-container mermaid-loading";
only.appendChild(lonely);
console.log(JSON.stringify({
  segs: extractSpeechSegments(body, { code_marker: "", diagram_marker: "" }),
  only: extractSpeechSegments(only, { code_marker: "", diagram_marker: "" }),
}));
"""
    )
    # No marker clip and no marker pause; the surrounding text keeps its own pacing.
    assert out["segs"] == [
        {"text": "Before the code block.", "pauseMs": 500},
        {"text": "After the diagram.", "pauseMs": 0},
    ]
    assert out["only"] == []


def test_pause_list_item_splits_a_list_into_one_clip_per_item() -> None:
    out = _run(
        """
const body = new El("div");
const ul = new El("ul");
for (const text of ["The first list item is red", "The second list item is blue",
                    "The third list item is green"]) {
  const li = new El("li"); li.appendChild(new Text_(text)); ul.appendChild(li);
}
body.appendChild(ul);
const nested = new El("ul");
const outer = new El("li"); outer.appendChild(new Text_("Parent item text here"));
const inner = new El("ul");
for (const text of ["Child item one is here", "Child item two is here"]) {
  const li = new El("li"); li.appendChild(new Text_(text)); inner.appendChild(li);
}
outer.appendChild(inner); nested.appendChild(outer);
const sibling = new El("li"); sibling.appendChild(new Text_("Sibling item text here"));
nested.appendChild(sibling);
const short = new El("ul");
for (const text of ["Milk", "Eggs", "Bread"]) {
  const li = new El("li"); li.appendChild(new Text_(text)); short.appendChild(li);
}
console.log(JSON.stringify({
  byDefault: extractSpeechSegments(body),
  perItem: extractSpeechSegments(body, { pause_list_item_ms: 300 }),
  nested: extractSpeechSegments(nested, { pause_list_item_ms: 300 }),
  short: extractSpeechSegments(short, { pause_list_item_ms: 300 }),
}));
"""
    )
    assert out["byDefault"] == [
        {
            "text": "The first list item is red. The second list item is blue."
            " The third list item is green.",
            "pauseMs": 0,
        }
    ]
    assert out["perItem"] == [
        {"text": "The first list item is red.", "pauseMs": 300},
        {"text": "The second list item is blue.", "pauseMs": 300},
        {"text": "The third list item is green.", "pauseMs": 0},
    ]
    assert out["nested"] == [
        {"text": "Parent item text here.", "pauseMs": 300},
        {"text": "Child item one is here.", "pauseMs": 300},
        {"text": "Child item two is here.", "pauseMs": 300},
        {"text": "Sibling item text here.", "pauseMs": 0},
    ]
    # An item is a clip of its own even when it is shorter than clip_min_chars:
    # the profile asked for one, and folding it forward would swallow the pause.
    assert out["short"] == [
        {"text": "Milk.", "pauseMs": 300},
        {"text": "Eggs.", "pauseMs": 300},
        {"text": "Bread.", "pauseMs": 0},
    ]


def test_table_cell_joiner_semicolon_and_pause() -> None:
    out = _run(
        """
const table = new El("table");
for (const row of [["alpha first", "beta second"], ["gamma third", "delta fourth"]]) {
  const tr = new El("tr");
  for (const text of row) {
    const td = new El("td"); td.appendChild(new Text_(text)); tr.appendChild(td);
  }
  table.appendChild(tr);
}
console.log(JSON.stringify({
  semicolon: extractSpeechSegments(table, { table_cell_joiner: "semicolon" }),
  pause: extractSpeechSegments(table, { table_cell_joiner: "pause", pause_table_row_ms: 120 }),
}));
"""
    )
    assert out["semicolon"] == [
        {"text": "alpha first; beta second.", "pauseMs": 150},
        {"text": "gamma third; delta fourth.", "pauseMs": 0},
    ]
    # Each cell is its own clip, the row pause between cells and between rows.
    assert out["pause"] == [
        {"text": "alpha first.", "pauseMs": 120},
        {"text": "beta second.", "pauseMs": 120},
        {"text": "gamma third.", "pauseMs": 120},
        {"text": "delta fourth.", "pauseMs": 0},
    ]


def test_table_header_mode_prefix_and_skip() -> None:
    out = _run(
        """
const mkTable = (withThead, secondHeader) => {
  const t = new El("table");
  const header = new El("tr");
  for (const text of ["Plan name", secondHeader]) {
    const th = new El("th"); th.appendChild(new Text_(text)); header.appendChild(th);
  }
  if (withThead) {
    const thead = new El("thead"); thead.appendChild(header); t.appendChild(thead);
  } else {
    t.appendChild(header);
  }
  const row = new El("tr");
  for (const text of ["Pro tier", "Nine dollars"]) {
    const td = new El("td"); td.appendChild(new Text_(text)); row.appendChild(td);
  }
  t.appendChild(row);
  return t;
};
console.log(JSON.stringify({
  row: extractSpeechSegments(mkTable(true, "Price per month")),
  theadPrefix: extractSpeechSegments(mkTable(true, "Price per month"), { table_header_mode: "prefix" }),
  firstRowPrefix: extractSpeechSegments(mkTable(false, "Price per month"), { table_header_mode: "prefix" }),
  skip: extractSpeechSegments(mkTable(true, "Price per month"), { table_header_mode: "skip" }),
  emptyHeader: extractSpeechSegments(mkTable(true, ""), { table_header_mode: "prefix" }),
}));
"""
    )
    assert out["row"] == [
        {"text": "Plan name, Price per month.", "pauseMs": 150},
        {"text": "Pro tier, Nine dollars.", "pauseMs": 0},
    ]
    assert out["theadPrefix"] == [
        {"text": "Plan name: Pro tier, Price per month: Nine dollars.", "pauseMs": 0}
    ]
    assert out["firstRowPrefix"] == out["theadPrefix"]
    assert out["skip"] == [{"text": "Pro tier, Nine dollars.", "pauseMs": 0}]
    # An empty header cell adds no "": " prefix.
    assert out["emptyHeader"] == [{"text": "Plan name: Pro tier, Nine dollars.", "pauseMs": 0}]


def test_inline_code_max_chars_decides_what_is_spoken() -> None:
    out = _run(
        """
const para = (codeText) => {
  const p = new El("p");
  p.appendChild(new Text_("Call the "));
  const code = new El("code"); code.appendChild(new Text_(codeText)); p.appendChild(code);
  p.appendChild(new Text_(" now please."));
  return p;
};
console.log(JSON.stringify({
  spoken: extractSpeechSegments(para("foo_bar_function")),
  marked: extractSpeechSegments(para("foo_bar_function"), { inline_code_max_chars: 5 }),
  raised: extractSpeechSegments(para("x".repeat(100)), { inline_code_max_chars: 500 }),
}));
"""
    )
    assert out["spoken"] == [{"text": "Call the foo_bar_function now please.", "pauseMs": 0}]
    assert out["marked"] == [{"text": "Call the Code omitted. now please.", "pauseMs": 0}]
    assert out["raised"] == [{"text": "Call the " + "x" * 100 + " now please.", "pauseMs": 0}]


def test_clip_min_and_max_chars_shape_the_clips() -> None:
    out = _run(
        """
const long = "alpha beta gamma delta. ".repeat(20);
console.log(JSON.stringify({
  separate: shapeSpeechSegments(
    [{ text: "Summary", pauseMs: 600 }, { text: "This paragraph is long enough to stand.", pauseMs: 0 }],
    { clip_min_chars: 0 }),
  split: shapeSpeechSegments([{ text: long, pauseMs: 0 }], { clip_max_chars: 120 }),
}));
"""
    )
    assert out["separate"] == [
        {"text": "Summary.", "pauseMs": 600},
        {"text": "This paragraph is long enough to stand.", "pauseMs": 0},
    ]
    assert len(out["split"]) > 1
    assert all(len(row["text"]) <= 120 for row in out["split"])
    assert (
        " ".join(row["text"] for row in out["split"]) == ("alpha beta gamma delta. " * 20).strip()
    )


def test_the_player_reads_a_reply_with_the_profiles_clip_sizes() -> None:
    out = _run(
        """
stubProfile = { clip_min_chars: 0 };                 // a short line stays its own clip
const el = new El("div"); el.className = "msg assistant";
const body = new El("div"); body.className = "msg-body";
const h = new El("h2"); h.appendChild(new Text_("Hi."));
const p = new El("p"); p.appendChild(new Text_("A paragraph long enough to stand alone."));
body.append(h); body.append(p); el.appendChild(body);
const player = createTtsPlayer({ base: "" });
player.play(el, new El("button"));
await tick();
console.log(JSON.stringify(log.fetches.filter((f) => f.method === "POST").map((f) => f.text)));
"""
    )
    assert out == ["Hi.", "A paragraph long enough to stand alone."]


def test_max_clips_comes_from_the_profile_and_is_in_the_notice() -> None:
    out = _run(
        """
stubProfile = { max_clips: 2 };
const el = new El("div"); el.className = "msg assistant";
const body = new El("div"); body.className = "msg-body";
for (const text of ["First paragraph is long enough.", "Second paragraph is long enough.",
                    "Third paragraph is long enough."]) {
  const p = new El("p"); p.appendChild(new Text_(text)); body.appendChild(p);
}
el.appendChild(body);
const player = createTtsPlayer({ base: "" });
player.play(el, new El("button"));
await tick();
console.log(JSON.stringify({
  posts: log.fetches.filter((f) => f.method === "POST").length,
  toasts: log.toasts,
}));
"""
    )
    assert out["posts"] == 2
    assert out["toasts"] == [
        [
            "Read-aloud stopped after the first 2 sections; the rest of this reply was skipped.",
            "warn",
        ]
    ]
