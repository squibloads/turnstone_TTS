"""Entry points for hands-free Voice mode.

Two surfaces, both able to regress silently:

* the console launcher's **Voice** tab — voice sessions are coordinators with
  the ``voice`` persona, so the tab must post that persona, open the
  coordinator pane through the ordinary create path, and stay hidden unless
  the persona is on this user's shelf (and ``audio.voice_mode_enabled``, when
  the page can read it, is not off);
* the Admin › Voice settings groups — every ``audio.*`` key that belongs to
  the page must sit in its group, or it renders under "Other".

``app.js`` is a classic script, so the harness slices the real functions and
stubs the page (fetch, persona cache, PaneManager).  The Voice groups are read
out of ``voice.js`` itself: ``test_voice_js.py`` hardcodes the group table as a
prelude, so a key added to the source but forgotten in a group would otherwise
land in "Other" with every existing test green.
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
    slice_braced_block,
)

pytestmark = node_skip

_STATIC = Path(__file__).resolve().parents[1] / "turnstone" / "console" / "static"
_APP = (_STATIC / "app.js").read_text(encoding="utf-8")
_VOICE = (_STATIC / "voice.js").read_text(encoding="utf-8")
_INDEX = (_STATIC / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The launcher action
# ---------------------------------------------------------------------------

_APP_PRELUDE = """
let _voiceModeEnabled = null;
let _voiceModeRead = false;
let _voiceModeReading = false;
const _VOICE_PERSONA = "voice";
let _launcherKind = "coordinator";
let _homeCoordComposer = null;
let _homeCoordBusy = false;
const _homeStagedFiles = [];
const _PROJECT_NEW = "__new__";
const _SCHEDULE_NO_FILES_MSG = "";
const _freshVoiceLaunches = new Set();
""" + "\n".join(
    extract_braced(_APP, sig)
    for sig in (
        "function _createWorkstreamFetchOpts(body, files) {",
        "function _createCoordinator(opts) {",
        "function _voicePersonaAvailable() {",
        "function _readVoiceModeSetting() {",
        "function _voiceLauncherAvailable() {",
        "function _refreshVoiceLauncher(readSetting) {",
        "function _paintLauncherVisibility() {",
        "function _setLauncherKind(kind, focus) {",
        "function startVoiceSession(opts) {",
        "function takeVoiceLaunch(wsId) {",
        "function submitHomeCoord(textFromComposer) {",
    )
)


def _launcher_kinds_source() -> str:
    """The real kind table, including its ``can()`` wiring.

    Extracted rather than retyped so a tab added to (or dropped from) app.js is
    exercised here, not shadowed by a harness copy.
    """
    start = _APP.index("const _LAUNCHER_KINDS = [")
    signature = "].map(function (k) {"
    brace = _APP.index(signature, start) + len(signature) - 1
    block = slice_braced_block(_APP, brace)
    assert block is not None, "unterminated _LAUNCHER_KINDS"
    end = _APP.index("});", brace + len(block) - 1) + len("});")
    return _APP[start:end]


def _run_app(body: str, extra: str = "") -> Any:
    """Run *body* inside the shared async harness.

    *extra* is module-scope source inserted after the sliced app functions —
    consts the functions resolve lexically (``_LAUNCHER_KINDS``) must live
    there, not inside the async body.
    """
    src = (
        _APP_PRELUDE
        + "\n"
        + extra
        + "\n(async () => {\n"
        + body
        + "\n})().catch((e) => { console.error(e); process.exit(1); });"
    )
    result = run_node_source(src)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


# The page stubs every launcher probe needs.  Individual tests override the
# persona list / fetch behaviour they care about; everything else stays inert.
_PAGE = """
const els = {
  "home-coord-error": { textContent: "" },
  "kind-voice": { disabled: false, style: { display: "" } },
};
globalThis.document = {
  getElementById: function (id) { return els[id] || null; },
};
globalThis.showToast = function () {};
globalThis._homeShowError = function (msg) { els["home-coord-error"].textContent = msg; };
globalThis._homeClearStagedFiles = function () {};
globalThis._refreshHomeCoordSubmitEnabled = function () {};
globalThis._hasCoordPermission = function () { return true; };
globalThis._consoleHasPermission = function () { return true; };
const fetched = [];
const sent = [];
const opened = [];
let createFails = false;          // the create answers 500 with no ws_id
globalThis.authFetch = function (url, opts) {
  fetched.push(url);
  sent.push({ url: url, opts: opts });
  if (createFails) {
    return Promise.resolve({ ok: false, status: 500, json: function () { return Promise.resolve({}); } });
  }
  return Promise.resolve({
    ok: true,
    status: 200,
    json: function () { return Promise.resolve({ ws_id: "ws-voice" }); },
  });
};
globalThis.window = {
  TurnstonePersonas: {
    personaChoices: function () {
      return [
        { value: "orchestrator", text: "Orchestrator" },
        { value: "voice", text: "Voice" },
      ];
    },
  },
  TS_SHELL: {
    panes: {
      openPane: function () { opened.push([].slice.call(arguments)); },
    },
  },
  location: { href: "" },
};
"""


def test_voice_button_is_gone_from_the_dashboard() -> None:
    assert 'id="start-voice-session"' not in _INDEX
    assert "Start voice session" not in _INDEX
    assert "start-voice-session" not in _APP


def test_kind_tab_order_puts_voice_between_coordinator_and_interactive() -> None:
    group = re.search(r'<div[^>]*\bid="launcher-kinds".*?</div>', _INDEX, re.DOTALL)
    assert group is not None, "the launcher's kind radiogroup must exist"
    ids = re.findall(r'id="(kind-[a-z]+)"', group.group(0))
    assert ids == [
        "kind-coordinator",
        "kind-voice",
        "kind-interactive",
        "kind-scheduled",
    ]
    assert group.group(0).count('role="radio"') == 4, "every tab keeps the radiogroup's radio role"


def test_voice_tab_is_a_launcher_kind_with_the_coordinator_permission() -> None:
    kinds = _launcher_kinds_source()
    assert 'kind: "voice"' in kinds
    assert 'id: "kind-voice"' in kinds
    assert 'perm: "admin.coordinator"' in kinds


def test_selecting_the_voice_tab_moves_the_roving_tabindex() -> None:
    out = _run_app(
        """
function fakeBtn() {
  const classes = new Set();
  return {
    tabIndex: -1,
    focused: false,
    attrs: {},
    classList: {
      toggle: function (name, on) {
        if (on) classes.add(name); else classes.delete(name);
      },
      contains: function (name) { return classes.has(name); },
    },
    setAttribute: function (name, value) { this.attrs[name] = String(value); },
    getAttribute: function (name) { return this.attrs[name] || null; },
    focus: function () { this.focused = true; },
  };
}
const els = {
  "kind-coordinator": fakeBtn(),
  "kind-voice": fakeBtn(),
  "kind-interactive": fakeBtn(),
  "kind-scheduled": fakeBtn(),
};
globalThis.document = {
  getElementById: function (id) { return els[id] || null; },
};
globalThis._homeShowError = function () {};
globalThis._applyLauncherFields = function () {};
globalThis._populateHomePersonaDropdown = function () {};
_setLauncherKind("voice");
const result = {};
Object.keys(els).forEach(function (id) {
  result[id] = {
    active: els[id].classList.contains("active"),
    checked: els[id].getAttribute("aria-checked"),
    tabIndex: els[id].tabIndex,
  };
});
console.log(JSON.stringify(result));
""",
        extra=_launcher_kinds_source(),
    )
    assert out["kind-voice"] == {"active": True, "checked": "true", "tabIndex": 0}
    for other in ("kind-coordinator", "kind-interactive", "kind-scheduled"):
        assert out[other] == {"active": False, "checked": "false", "tabIndex": -1}


def test_voice_create_posts_the_persona_and_opens_the_coordinator_pane() -> None:
    out = _run_app(
        _PAGE
        + """
startVoiceSession();
await new Promise(setImmediate);
console.log(JSON.stringify({ sent: sent, opened: opened }));
"""
    )
    # One create, on the ordinary coordinator endpoint — the voice session is
    # a persona, not a separate kind or route.
    assert len(out["sent"]) == 1
    assert out["sent"][0]["url"] == "/v1/api/workstreams/new"
    assert out["sent"][0]["opts"]["method"] == "POST"
    assert json.loads(out["sent"][0]["opts"]["body"]) == {"persona": "voice"}
    assert out["opened"] == [["coordinator", "ws-voice"]]


def test_voice_create_sends_the_opening_line_from_the_tab() -> None:
    out = _run_app(
        _PAGE
        + """
startVoiceSession({ task: "  what needs my attention?  " });
await new Promise(setImmediate);
console.log(JSON.stringify({ sent: sent }));
"""
    )
    assert json.loads(out["sent"][0]["opts"]["body"]) == {
        "persona": "voice",
        "initial_message": "what needs my attention?",
    }


def test_voice_tab_submit_uses_the_voice_create_path() -> None:
    out = _run_app(
        _PAGE
        + """
_launcherKind = "voice";
_homeCoordComposer = {
  value: "hello",
  sendBtn: { disabled: false },
  getOptionValues: function () { return {}; },
  setBusy: function () {},
  getOptionValue: function () { return ""; },
  setOptionValue: function () {},
};
submitHomeCoord();
await new Promise(setImmediate);
console.log(JSON.stringify({
  sent: sent,
  opened: opened,
  err: els["home-coord-error"].textContent,
}));
""",
        extra=_launcher_kinds_source(),
    )
    assert len(out["sent"]) == 1
    assert out["sent"][0]["url"] == "/v1/api/workstreams/new"
    assert json.loads(out["sent"][0]["opts"]["body"]) == {
        "persona": "voice",
        "initial_message": "hello",
    }
    assert out["opened"] == [["coordinator", "ws-voice"]]
    assert out["err"] == ""


def test_voice_tab_submit_without_text_keeps_the_one_click_behaviour() -> None:
    out = _run_app(
        _PAGE
        + """
_launcherKind = "voice";
_homeCoordComposer = {
  value: "",
  sendBtn: { disabled: false },
  getOptionValues: function () { return {}; },
  setBusy: function () {},
  getOptionValue: function () { return ""; },
  setOptionValue: function () {},
};
submitHomeCoord();
await new Promise(setImmediate);
console.log(JSON.stringify({ sent: sent }));
""",
        extra=_launcher_kinds_source(),
    )
    assert json.loads(out["sent"][0]["opts"]["body"]) == {"persona": "voice"}


def test_voice_tab_hidden_without_the_voice_persona() -> None:
    out = _run_app(
        _PAGE
        + """
window.TurnstonePersonas.personaChoices = function () {
  return [{ value: "orchestrator", text: "Orchestrator" }];
};
_refreshVoiceLauncher();
console.log(JSON.stringify({
  display: els["kind-voice"].style.display,
  fetched: fetched,
}));
"""
    )
    assert out["display"] == "none"
    # No persona, no reason to read the switch.
    assert out["fetched"] == []


def test_voice_tab_hidden_when_the_setting_is_already_off() -> None:
    out = _run_app(
        _PAGE
        + """
_voiceModeEnabled = false;
_refreshVoiceLauncher();
console.log(JSON.stringify({
  display: els["kind-voice"].style.display,
  fetched: fetched,
}));
"""
    )
    assert out["display"] == "none"
    # A settled false is not re-read on every repaint.
    assert out["fetched"] == []


def test_voice_tab_reads_the_switch_and_hides_when_it_is_off() -> None:
    out = _run_app(
        _PAGE
        + """
authFetch = function (url) {
  fetched.push(url);
  return Promise.resolve({
    ok: true,
    status: 200,
    json: function () {
      return Promise.resolve({
        settings: [{ key: "audio.voice_mode_enabled", value: false }],
      });
    },
  });
};
_refreshVoiceLauncher();
await new Promise(setImmediate);
console.log(JSON.stringify({
  display: els["kind-voice"].style.display,
  fetched: fetched,
  enabled: _voiceModeEnabled,
}));
"""
    )
    assert out["fetched"] == ["/v1/api/admin/settings"]
    assert out["enabled"] is False
    assert out["display"] == "none"


def test_voice_tab_fails_open_when_the_switch_is_unreadable() -> None:
    """admin.settings is a separate scope from admin.coordinator: a refused
    read must leave the tab visible (the pane enforces the switch) and must
    not retry itself in a loop."""
    out = _run_app(
        _PAGE
        + """
authFetch = function (url) {
  fetched.push(url);
  return Promise.resolve({
    ok: false,
    status: 403,
    json: function () { return Promise.resolve({ error: "forbidden" }); },
  });
};
_refreshVoiceLauncher();
await new Promise(setImmediate);
console.log(JSON.stringify({
  display: els["kind-voice"].style.display,
  fetched: fetched,
  enabled: _voiceModeEnabled,
}));
"""
    )
    assert out["fetched"] == ["/v1/api/admin/settings"]
    assert out["enabled"] is None
    assert out["display"] == ""


def test_voice_tab_skips_the_switch_read_without_admin_settings() -> None:
    """A coordinator-only user must not fire a doomed admin.settings request
    on every page load.  The switch stays unknown, so the tab stays visible —
    the same fail-open state as a refused read."""
    out = _run_app(
        _PAGE
        + """
globalThis._consoleHasPermission = function (perm) {
  return perm !== "admin.settings";
};
_refreshVoiceLauncher();
await new Promise(setImmediate);
console.log(JSON.stringify({
  display: els["kind-voice"].style.display,
  fetched: fetched,
  enabled: _voiceModeEnabled,
}));
"""
    )
    assert out["fetched"] == []
    assert out["enabled"] is None
    assert out["display"] == ""


def test_launcher_persona_name_matches_the_backend_contract() -> None:
    """The persona slug is spelled in two languages (voice_config.py for the
    server, a classic-script constant here).  app.js cannot import it, so pin
    the pair — a rename on either side must fail loudly."""
    from turnstone.core.voice_config import VOICE_PERSONA

    m = re.search(r'^const _VOICE_PERSONA = "([^"]+)";', _APP, re.MULTILINE)
    assert m is not None, "_VOICE_PERSONA not found in console app.js"
    assert m.group(1) == VOICE_PERSONA


def test_voice_tab_markup_is_wired_through_the_kind_table() -> None:
    """The tab is static markup and the generic kind wiring owns its click —
    there is no bespoke voice button handler left to drift."""
    assert 'id="kind-voice"' in _INDEX
    assert 'id: "kind-voice"' in _APP
    assert 'addEventListener("click", startVoiceSession)' not in _APP


# ---------------------------------------------------------------------------
# Admin › Voice settings groups
# ---------------------------------------------------------------------------

_GROUPS_RE = re.compile(r"^const VOICE_SETTING_GROUPS = \[[\s\S]*?^\];", re.MULTILINE)


def _groups_source() -> str:
    m = _GROUPS_RE.search(_VOICE)
    assert m is not None, "VOICE_SETTING_GROUPS not found in voice.js"
    return m.group(0)


_VOICE_PRELUDE = (
    _groups_source()
    + "\nconst MODEL_ROLES = [];\n"
    + "\n".join(
        extract_braced(_VOICE, sig)
        for sig in (
            "function _voiceStyleNames(raw) {",
            "function _voiceStyleChoices(raw, current) {",
            "function _voiceSettingsFrom(valuesArr, schemaArr) {",
            "function _voiceSettingGroups(items) {",
        )
    )
)


def test_new_audio_keys_land_in_their_voice_groups_not_other() -> None:
    # Drive the real merge as well as the grouping: a key in the group table
    # but omitted from the page (or vice versa) still lands in "Other".
    src = (
        _VOICE_PRELUDE
        + """
const newKeys = [
  "audio.stt_auto_stop",
  "audio.stt_silence_ms",
  "audio.stt_vad_threshold",
  "audio.tts_stream",
  "audio.voice_mode_enabled",
];
const values = newKeys.map(function (key) {
  return { key: key, value: true, source: "default", type: "bool" };
});
const schema = newKeys.map(function (key) {
  return {
    key: key,
    type: "bool",
    default: true,
    description: key,
    section: "audio",
    help: key,
  };
});
const items = _voiceSettingsFrom(values, schema);
const groups = _voiceSettingGroups(items);
const byId = {};
groups.forEach(function (group) {
  byId[group.id] = group.items.map(function (item) { return item.key; });
});
console.log(JSON.stringify({
  order: groups.map(function (group) { return group.id; }),
  byId: byId,
}));
"""
    )
    result = run_node_source(src)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])

    assert out["order"] == ["voice-mode", "dictation", "read-aloud"]
    assert out["byId"]["voice-mode"] == ["audio.voice_mode_enabled"]
    assert out["byId"]["dictation"] == [
        "audio.stt_auto_stop",
        "audio.stt_silence_ms",
        "audio.stt_vad_threshold",
    ]
    assert out["byId"]["read-aloud"] == ["audio.tts_stream"]
    assert "other" not in out["byId"]


# ---------------------------------------------------------------------------
# A launch is the only thing that opens a voice session listening
# ---------------------------------------------------------------------------


def test_a_voice_launch_is_taken_exactly_once() -> None:
    # The pane takes it when it mounts; a later mount of the same session (a
    # rebuild, a second pane) must find nothing and open with voice mode off.
    out = _run_app(
        _PAGE
        + """
startVoiceSession();
await new Promise(setImmediate);
console.log(JSON.stringify({ first: takeVoiceLaunch("ws-voice"),
  again: takeVoiceLaunch("ws-voice"), other: takeVoiceLaunch("ws-other") }));
"""
    )
    assert out == {"first": True, "again": False, "other": False}


def test_a_failed_voice_create_marks_nothing() -> None:
    out = _run_app(
        _PAGE
        + """
createFails = true;
startVoiceSession();
await new Promise(setImmediate);
console.log(JSON.stringify({ taken: takeVoiceLaunch("ws-voice"), size: _freshVoiceLaunches.size }));
"""
    )
    assert out == {"taken": False, "size": 0}


def test_the_launch_record_reaches_the_pane_through_ts_app() -> None:
    assert "window.TS_APP.takeVoiceLaunch = takeVoiceLaunch;" in _APP
    # Page memory only: a reload must forget it, so nothing may persist it.
    body = _APP[_APP.index("function takeVoiceLaunch(wsId) {") :][:600]
    assert "Storage" not in body
