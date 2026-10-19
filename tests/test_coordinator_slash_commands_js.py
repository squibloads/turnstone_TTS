"""Coordinator composer slash-command routing (coordinator.js).

The coordinator pane posts chat to ``POST .../send``.  ``/model`` and the
rest of the interactive command registry must never take that path (issue
#1440): ``coordSend`` detects a leading ``/`` and posts to the console's
path-keyed ``POST .../command`` instead.  The shipped functions are sliced
out of ``createCoordinatorPane`` and run in node against recording stubs,
so the assertions are about the real control flow, not a source pattern.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import (
    FAKE_DOM,
    extract_braced,
    node_skip,
    run_node_source,
)

pytestmark = node_skip

_STATIC = Path(__file__).resolve().parents[1] / "turnstone"
_COORD = (_STATIC / "console/static/coordinator/coordinator.js").read_text(encoding="utf-8")


def _fn(sig: str) -> str:
    return extract_braced(_COORD, sig)


_PRELUDE = (
    FAKE_DOM
    + r"""
const log = { fetches: [], messages: [], settle: [], rewinds: [], retries: 0, scrolls: 0 };
const messagesEl = document.createElement("div");
let wsId = "coord-1", busy = false;
function _scheduleScroll() { log.scrolls += 1; }
const composer = {
  value: "",
  clear() { this.value = ""; this.clears = (this.clears || 0) + 1; },
};
const attachments = {
  isEmpty: () => true,
  snapshot: () => ({ uploading: false, attachments: [], attachment_ids: [] }),
  consume: () => {},
};
const queue = { addQueuedMessage: () => { throw new Error("queued"); } };
function setBusy(b) { busy = b; }
function appendText(role, text) { log.messages.push([role, text]); }
function appendUserMessageWithAttachments() { return {}; }
const scrollFollow = { jumpToLatest() {} };
function mintClientSendId() { return "client-1"; }
function parsePriority(text) { return { displayText: text, priority: "notice" }; }
function postAndSettleSend(_queue, _req, opts) { log.settle.push(opts); }
function mergeRejectedComposerText(text) { return text; }
let fetchResponse = { ok: true, status: 200, body: { status: "ok" } };
function authFetch(url, opts) {
  log.fetches.push({ url, method: opts && opts.method, body: opts && opts.body });
  return Promise.resolve({
    ok: fetchResponse.ok,
    status: fetchResponse.status,
    json: async () => fetchResponse.body,
  });
}
function _rewindToTurns(n) { log.rewinds.push(n); }
function _retryLast() { log.retries += 1; }
const tick = async () => {
  for (let i = 0; i < 8; i++) await new Promise((r) => setImmediate(r));
};
"""
    + _fn("  function appendCommandEcho(text) {")
    + "\n"
    + _fn("  function _runCoordCommand(text) {")
    + "\n"
    + _fn("  function coordSend() {")
)

_FUNCS = ("appendCommandEcho", "_runCoordCommand", "coordSend")


def _run(body: str) -> Any:
    src = (
        _PRELUDE
        + f"\n(async () => {{\n{body}\n}})().catch((e) => {{ console.error(e); process.exit(1); }});"
    )
    r = run_node_source(src)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_the_sliced_surface_exists() -> None:
    """Missing helpers would otherwise silently turn the wiring assert into
    a vacuous pass."""
    for name in _FUNCS:
        assert re.search(rf"function {name}\(", _COORD), f"coordinator.js has no {name}"


def test_voice_transcripts_share_the_composer_rule() -> None:
    """Dictation fills the composer and the voice loop calls ``_sendVoiceText``,
    which calls ``coordSend``: the slash rule cannot diverge between typed and
    spoken input."""
    send_voice = extract_braced(_COORD, "  function _sendVoiceText(text) {")
    assert "coordSend();" in send_voice
    assert 'trimmed.startsWith("/")' in _COORD


def test_a_slash_command_posts_to_the_command_route_not_send() -> None:
    out = _run(
        """
composer.value = "/model alt";
coordSend();
await tick();
const echo = messagesEl.children.find((el) => el.classList.contains("command-echo"));
console.log(JSON.stringify({
  fetches: log.fetches,
  echo: echo ? echo.textContent : null,
  cleared: composer.clears,
  messages: log.messages,
}));
"""
    )
    assert len(out["fetches"]) == 1
    assert out["fetches"][0]["url"] == "/v1/api/workstreams/coord-1/command"
    assert json.loads(out["fetches"][0]["body"]) == {"command": "/model alt"}
    assert out["echo"] == "/model alt"
    assert out["cleared"] == 1
    # No optimistic user bubble and no send-settle path.
    assert out["messages"] == []


def test_ordinary_text_still_goes_to_send() -> None:
    out = _run(
        """
composer.value = "the /model command switches the model";
coordSend();
await tick();
console.log(JSON.stringify({ fetches: log.fetches, settle: log.settle.length }));
"""
    )
    assert len(out["fetches"]) == 1
    assert out["fetches"][0]["url"] == "/v1/api/workstreams/coord-1/send"
    assert json.loads(out["fetches"][0]["body"])["message"] == (
        "the /model command switches the model"
    )
    assert out["settle"] == 1


def test_a_url_is_not_a_slash_command() -> None:
    out = _run(
        """
composer.value = "https://example.com/model/alt";
coordSend();
await tick();
console.log(JSON.stringify({ urls: log.fetches.map((f) => f.url) }));
"""
    )
    assert out["urls"] == ["/v1/api/workstreams/coord-1/send"]


def test_rewind_and_retry_use_the_path_keyed_mutations() -> None:
    out = _run(
        """
composer.value = "/rewind 2";
coordSend();
composer.value = "/retry";
coordSend();
await tick();
console.log(JSON.stringify({ fetches: log.fetches, rewinds: log.rewinds, retries: log.retries }));
"""
    )
    assert out["fetches"] == []
    assert out["rewinds"] == [2]
    assert out["retries"] == 1


def test_a_bad_rewind_argument_is_a_visible_usage_error() -> None:
    out = _run(
        """
composer.value = "/rewind nope";
coordSend();
await tick();
console.log(JSON.stringify({ fetches: log.fetches, rewinds: log.rewinds, messages: log.messages }));
"""
    )
    assert out["fetches"] == []
    assert out["rewinds"] == []
    assert out["messages"] == [["error", "Usage: /rewind <N> — N must be a positive integer"]]


def test_a_rejected_command_error_is_rendered() -> None:
    out = _run(
        """
fetchResponse = {
  ok: false,
  status: 400,
  body: { error: "This workstream command is only available in the local CLI." },
};
composer.value = "/new";
coordSend();
await tick();
console.log(JSON.stringify({ messages: log.messages }));
"""
    )
    assert out["messages"] == [
        ["error", "This workstream command is only available in the local CLI."]
    ]


def test_a_running_command_says_so() -> None:
    """Past the endpoint's 25 s backstop the worker keeps going; the pane must
    not read silence as success or failure."""
    out = _run(
        """
fetchResponse = { ok: true, status: 200, body: { status: "running" } };
composer.value = "/compact";
coordSend();
await tick();
console.log(JSON.stringify({ messages: log.messages }));
"""
    )
    assert out["messages"] == [
        ["info", "Command is still running — results will appear here when it finishes."]
    ]


def test_a_busy_command_refusal_is_rendered() -> None:
    out = _run(
        """
fetchResponse = {
  ok: false,
  status: 409,
  body: { status: "busy", error: "Session is busy — wait for the current turn to finish, then retry." },
};
composer.value = "/model alt";
coordSend();
await tick();
console.log(JSON.stringify({ messages: log.messages }));
"""
    )
    assert out["messages"] == [
        ["error", "Session is busy — wait for the current turn to finish, then retry."]
    ]


def test_a_command_is_refused_while_a_turn_runs() -> None:
    out = _run(
        """
busy = true;
composer.value = "/model alt";
coordSend();
await tick();
console.log(JSON.stringify({
  fetches: log.fetches,
  messages: log.messages,
  value: composer.value,
  cleared: composer.clears || 0,
}));
"""
    )
    assert out["fetches"] == []
    assert out["messages"] == [["info", "Session is busy — commands can't run mid-turn."]]
    # The text stays in the composer so the user can retry.
    assert out["value"] == "/model alt"
    assert out["cleared"] == 0
