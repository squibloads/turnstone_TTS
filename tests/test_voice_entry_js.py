"""Entry points for hands-free Voice mode.

Two surfaces, both able to regress silently:

* the console launcher's one-click "Start voice session" action — it must post
  the ``voice`` persona, open the coordinator pane exactly like a normal
  create, and stay hidden unless the voice persona is on this user's shelf
  (and ``audio.voice_mode_enabled``, when the page can read it, is not off);
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

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source

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
""" + "\n".join(
    extract_braced(_APP, sig)
    for sig in (
        "function _createWorkstreamFetchOpts(body, files) {",
        "function _createCoordinator(opts) {",
        "function _voicePersonaAvailable() {",
        "function _readVoiceModeSetting() {",
        "function _refreshVoiceLauncher(readSetting) {",
        "function startVoiceSession() {",
    )
)


def _run_app(body: str) -> Any:
    src = (
        _APP_PRELUDE
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
  "start-voice-session": { disabled: false, style: { display: "" } },
};
globalThis.document = {
  getElementById: function (id) { return els[id] || null; },
};
globalThis.showToast = function () {};
globalThis._homeShowError = function () {};
globalThis._hasCoordPermission = function () { return true; };
globalThis._consoleHasPermission = function () { return true; };
const fetched = [];
const sent = [];
const opened = [];
globalThis.authFetch = function (url, opts) {
  fetched.push(url);
  sent.push({ url: url, opts: opts });
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


def test_launcher_posts_the_voice_persona_and_opens_the_coordinator_pane() -> None:
    out = _run_app(
        _PAGE
        + """
startVoiceSession();
await new Promise(setImmediate);
console.log(JSON.stringify({
  sent: sent,
  opened: opened,
  disabled: els["start-voice-session"].disabled,
}));
"""
    )
    # One create, on the ordinary coordinator endpoint — the voice session is
    # a persona, not a separate kind or route.
    assert len(out["sent"]) == 1
    assert out["sent"][0]["url"] == "/v1/api/workstreams/new"
    assert out["sent"][0]["opts"]["method"] == "POST"
    assert json.loads(out["sent"][0]["opts"]["body"]) == {"persona": "voice"}
    assert out["opened"] == [["coordinator", "ws-voice"]]
    assert out["disabled"] is False


def test_launcher_hidden_without_the_voice_persona() -> None:
    out = _run_app(
        _PAGE
        + """
window.TurnstonePersonas.personaChoices = function () {
  return [{ value: "orchestrator", text: "Orchestrator" }];
};
_refreshVoiceLauncher();
console.log(JSON.stringify({
  display: els["start-voice-session"].style.display,
  fetched: fetched,
}));
"""
    )
    assert out["display"] == "none"
    # No persona, no reason to read the switch.
    assert out["fetched"] == []


def test_launcher_hidden_when_the_setting_is_already_off() -> None:
    out = _run_app(
        _PAGE
        + """
_voiceModeEnabled = false;
_refreshVoiceLauncher();
console.log(JSON.stringify({
  display: els["start-voice-session"].style.display,
  fetched: fetched,
}));
"""
    )
    assert out["display"] == "none"
    # A settled false is not re-read on every repaint.
    assert out["fetched"] == []


def test_launcher_reads_the_switch_and_hides_when_it_is_off() -> None:
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
  display: els["start-voice-session"].style.display,
  fetched: fetched,
  enabled: _voiceModeEnabled,
}));
"""
    )
    assert out["fetched"] == ["/v1/api/admin/settings"]
    assert out["enabled"] is False
    assert out["display"] == "none"


def test_launcher_fails_open_when_the_switch_is_unreadable() -> None:
    """admin.settings is a separate scope from admin.coordinator: a refused
    read must leave the action visible (the pane enforces the switch) and must
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
  display: els["start-voice-session"].style.display,
  fetched: fetched,
  enabled: _voiceModeEnabled,
}));
"""
    )
    assert out["fetched"] == ["/v1/api/admin/settings"]
    assert out["enabled"] is None
    assert out["display"] == ""


def test_launcher_skips_the_switch_read_without_admin_settings() -> None:
    """A coordinator-only user must not fire a doomed admin.settings request
    on every page load.  The switch stays unknown, so the action stays visible
    — the same fail-open state as a refused read."""
    out = _run_app(
        _PAGE
        + """
globalThis._consoleHasPermission = function (perm) {
  return perm !== "admin.settings";
};
_refreshVoiceLauncher();
await new Promise(setImmediate);
console.log(JSON.stringify({
  display: els["start-voice-session"].style.display,
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


def test_launcher_markup_exists_and_is_wired_once() -> None:
    """The action is static markup (the pane has no composer slot for it) and
    the classic script wires it in _ensureHomeComposerInit — a missing id or a
    dropped listener leaves a dead button that no JS test would otherwise
    notice."""
    assert 'id="start-voice-session"' in _INDEX
    assert "Start voice session" in _INDEX
    assert 'addEventListener("click", startVoiceSession)' in _APP


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
