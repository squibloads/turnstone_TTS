"""voice.js: the Admin › Voice shell's settings model and save/reset calls.

The page is ordinary DOM layout, so the harness pins the parts that can be
wrong silently: which ``audio.*`` settings the Settings tab owns (the model
roles and the two JSON-list editors belong elsewhere), how a default style is
offered when the stored name no longer matches a style, and the request
bodies the settings API sees.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source

pytestmark = node_skip

_VOICE = Path(__file__).resolve().parents[1] / "turnstone" / "console" / "static" / "voice.js"
_ADMIN = Path(__file__).resolve().parents[1] / "turnstone" / "console" / "static" / "admin.js"

_GROUPS = """
const VOICE_SETTING_GROUPS = [
  {
    id: "dictation",
    label: "Dictation",
    keys: ["audio.stt_prompt", "audio.stt_dictionary_hint", "audio.stt_dictionary_correct"],
  },
  {
    id: "read-aloud",
    label: "Read aloud",
    keys: [
      "audio.tts_voice",
      "audio.tts_playback_rate",
      "audio.tts_style",
      "audio.tts_pronunciations",
    ],
  },
];
const VOICE_MAX_STYLES = 20;
"""

_ROLES = 'const MODEL_ROLES = [{aliasKey: "audio.stt_model_alias"}, {aliasKey: "audio.tts_model_alias"}];'


def _source(*signatures: str, prelude: str = "") -> str:
    body = _VOICE.read_text(encoding="utf-8")
    return prelude + "\n".join(extract_braced(body, sig) for sig in signatures)


def _run(source: str, body: str) -> None:
    result = run_node_source("import assert from 'node:assert/strict';\n" + source + "\n" + body)
    assert result.returncode == 0, result.stderr


def test_voice_js_parses() -> None:
    """The shell is a classic script loaded by index.html; a syntax error in
    the top-level IIFEs would take the whole tab down and the sliced unit
    tests would still pass."""
    proc = subprocess.run(
        ["node", "--check", str(_VOICE)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr


def test_the_voice_rows_use_the_shared_settings_component() -> None:
    """The default style is the shared select branch and pronunciations is the
    new multi-line branch — both inside .settings-row, so the Settings tab's
    dirty/save/reset wiring keeps working."""
    source = r"""
const ALIAS_SETTING_KEYS = [];
const INHERIT_EMPTY_LABEL_KEYS = [];
function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
""" + extract_braced(_ADMIN.read_text(encoding="utf-8"), "function _renderSettingRow(item) {")
    _run(
        source,
        r"""
const base = {
  key: "audio.tts_style", value: "Social", source: "storage", type: "str",
  default_value: "", description: "Default speaking style", section: "audio",
  is_secret: false, choices: ["", "Professional", "Social"], restart_required: false,
  help: "", reference_url: "",
};
const styleRow = _renderSettingRow(base);
assert.match(styleRow, /class="settings-row" data-row-key="audio\.tts_style"/);
assert.match(styleRow, /<select data-setting-key="audio\.tts_style"/);
assert.match(styleRow, /<option value="Social" selected>Social<\/option>/);
assert.match(styleRow, /data-save-key="audio\.tts_style"/);
assert.match(styleRow, /data-reset-key="audio\.tts_style"/);

const pronRow = _renderSettingRow({
  ...base,
  key: "audio.tts_pronunciations",
  value: "SQL = sequel\nnginx = engine x < fast & clear",
  choices: null,
  source: "default",
  multiline: true,
});
assert.match(pronRow, /<textarea data-setting-key="audio\.tts_pronunciations"[^>]*rows="6"/);
assert.match(pronRow, /SQL = sequel\nnginx = engine x &lt; fast &amp; clear<\/textarea>/);
assert.doesNotMatch(pronRow, /<input type="text" data-setting-key="audio\.tts_pronunciations"/);
assert.doesNotMatch(pronRow, /data-reset-key=/, "a default-sourced row offers no reset");
""",
    )


def test_style_names_are_tolerant_of_stored_junk() -> None:
    _run(
        _source("function _voiceStyleNames(raw) {", prelude=_GROUPS),
        """
assert.deepEqual(_voiceStyleNames(""), []);
assert.deepEqual(_voiceStyleNames("not json"), []);
assert.deepEqual(_voiceStyleNames('{"name": "Plain"}'), []);
assert.deepEqual(
  _voiceStyleNames(JSON.stringify([
    {name: "Professional", instruction: ""},
    null,
    {name: 7},
    {name: "  "},
    {instruction: "no name"},
    {name: " Social "},
  ])),
  ["Professional", "Social"],
  "only non-empty string names survive; names are trimmed",
);
// Names are unique case-insensitively and the later entry wins, matching
// parse_styles in turnstone/core/voice_config.py.
assert.deepEqual(
  _voiceStyleNames(JSON.stringify([{name: "Plain"}, {name: "plain"}])),
  ["plain"],
);
const capped = [];
for (let i = 0; i < 25; i++) capped.push({name: "style " + i});
assert.equal(_voiceStyleNames(JSON.stringify(capped)).length, 20);
""",
    )


def test_the_default_style_choice_keeps_an_unknown_stored_name() -> None:
    _run(
        _source(
            "function _voiceStyleChoices(raw, current) {",
            "function _voiceStyleNames(raw) {",
            prelude=_GROUPS,
        ),
        """
const styles = JSON.stringify([{name: "Professional"}, {name: "Plain"}]);
assert.deepEqual(_voiceStyleChoices(styles, ""), {
  choices: ["", "Professional", "Plain"],
  value: "",
});
// A stored name that differs only in case selects the canonical style.
assert.deepEqual(_voiceStyleChoices(styles, "professional"), {
  choices: ["", "Professional", "Plain"],
  value: "Professional",
});
// A name removed from the styles list is still offered, or the select would
// silently paint the first style while the stored value is something else.
assert.deepEqual(_voiceStyleChoices(styles, "Radio"), {
  choices: ["", "Professional", "Plain", "Radio"],
  value: "Radio",
});
// Bad JSON must not lose the stored default either.
assert.deepEqual(_voiceStyleChoices("broken", "Radio"), {
  choices: ["", "Radio"],
  value: "Radio",
});
""",
    )


def test_the_settings_tab_owns_the_non_editor_audio_settings() -> None:
    _run(
        _source(
            "function _voiceSettingsFrom(valuesArr, schemaArr) {",
            "function _voiceSettingGroups(items) {",
            "function _voiceStyleChoices(raw, current) {",
            "function _voiceStyleNames(raw) {",
            prelude=_GROUPS + _ROLES,
        ),
        """
const schema = (key, extra) => Object.assign(
  {key, type: "str", default: "", description: key + " description", section: "audio", help: key + " help"},
  extra,
);
const value = (key, val, source) => ({key, value: val, source: source || "default", type: "str"});
const styles = [{name: "Professional"}, {name: "Social"}, {name: "Plain"}];
const values = [
  value("audio.stt_model_alias", ""),
  value("audio.tts_model_alias", ""),
  value("audio.voice_dictionary", "[]"),
  value("audio.tts_styles", JSON.stringify(styles)),
  value("audio.tts_profiles", '{"tts-qwen":{"max_clips":50}}'),
  value("audio.tts_rewrite_rules", "[]"),
  value("audio.tts_cleanup_disabled", '["versions"]'),
  value("audio.tts_builtin_terms_off", '["YAML"]'),
  value("audio.stt_prompt", "domain words", "storage"),
  value("audio.stt_dictionary_hint", true),
  value("audio.stt_dictionary_correct", false),
  value("audio.tts_voice", "alloy"),
  value("audio.tts_playback_rate", 1.25, "storage"),
  value("audio.tts_normalize", true),
  value("audio.tts_style", "social"),
  value("audio.tts_pronunciations", "SQL = sequel"),
  value("model.default_alias", "chat"),
  value("audio.zz_future", "new"),
];
const items = _voiceSettingsFrom(values, [
  schema("audio.stt_prompt"),
  schema("audio.stt_dictionary_hint", {type: "bool", default: true}),
  schema("audio.stt_dictionary_correct", {type: "bool", default: true}),
  schema("audio.tts_voice"),
  schema("audio.tts_playback_rate", {type: "float", default: 1.0}),
  schema("audio.tts_normalize", {type: "bool", default: true}),
  schema("audio.tts_style"),
  schema("audio.tts_pronunciations"),
  schema("audio.zz_future"),
]);
assert.deepEqual(items.map((item) => item.key), [
  "audio.stt_prompt",
  "audio.stt_dictionary_hint",
  "audio.stt_dictionary_correct",
  "audio.tts_voice",
  "audio.tts_playback_rate",
  "audio.tts_style",
  "audio.tts_pronunciations",
  "audio.zz_future",
], "the deliberate group order, then a future setting alphabetically");
const byKey = Object.fromEntries(items.map((item) => [item.key, item]));
assert.equal(byKey["audio.voice_dictionary"], undefined, "the dictionary has its own tab");
assert.equal(byKey["audio.tts_styles"], undefined, "the styles editor has its own tab");
assert.equal(byKey["audio.stt_model_alias"], undefined, "role pickers live in Models -> Roles");
assert.equal(byKey["audio.tts_model_alias"], undefined);
assert.equal(byKey["audio.tts_profiles"], undefined, "the pacing profile editor owns it");
assert.equal(byKey["audio.tts_rewrite_rules"], undefined, "the Text cleanup tab owns it");
assert.equal(byKey["audio.tts_cleanup_disabled"], undefined, "the Text cleanup tab owns it");
assert.equal(byKey["audio.tts_builtin_terms_off"], undefined, "the Dictionary tab owns it");
assert.equal(byKey["audio.tts_normalize"], undefined, "the cleanup master switch moved tabs");
assert.equal(byKey["audio.stt_prompt"].source, "storage");
assert.equal(byKey["audio.stt_prompt"].description, "audio.stt_prompt description");
assert.equal(byKey["audio.stt_prompt"].help, "audio.stt_prompt help");
assert.equal(byKey["audio.tts_pronunciations"].multiline, true);
assert.deepEqual(byKey["audio.tts_style"].choices, ["", "Professional", "Social", "Plain"]);
assert.equal(byKey["audio.tts_style"].value, "Social", "case-insensitive match, canonical spelling");

const groups = _voiceSettingGroups(items);
assert.deepEqual(groups.map((group) => group.id), ["dictation", "read-aloud", "other"]);
assert.deepEqual(groups[0].items.map((item) => item.key), [
  "audio.stt_prompt",
  "audio.stt_dictionary_hint",
  "audio.stt_dictionary_correct",
]);
assert.deepEqual(groups[2].items.map((item) => item.key), ["audio.zz_future"]);
""",
    )


def test_saving_a_setting_puts_the_typed_value() -> None:
    _run(
        _source("function saveVoiceSetting(key, value) {"),
        """
const calls = [];
const authFetch = (url, init) => {
  calls.push([url, init]);
  return Promise.resolve({ok: true, json: () => Promise.resolve({})});
};
const toasts = [];
const showToast = (text) => toasts.push(text);
let reloads = 0;
const loadVoiceSettings = () => { reloads++; };
const drain = async () => { for (let i = 0; i < 3; i++) await new Promise(setImmediate); };

saveVoiceSetting("audio.tts_voice", "alloy");
await drain();
assert.equal(calls.length, 1);
assert.equal(calls[0][0], "/v1/api/admin/settings/audio.tts_voice");
assert.equal(calls[0][1].method, "PUT");
assert.deepEqual(calls[0][1].headers, {"Content-Type": "application/json"});
assert.deepEqual(JSON.parse(calls[0][1].body), {value: "alloy"});
assert.deepEqual(toasts, ["Setting saved"]);
assert.equal(reloads, 1);
""",
    )


def test_a_refused_setting_shows_the_servers_message() -> None:
    _run(
        _source("function saveVoiceSetting(key, value) {"),
        """
const calls = [];
const authFetch = (url, init) => {
  calls.push([url, init]);
  return Promise.resolve({ok: false, json: () => Promise.resolve({error: "too long"})});
};
const toasts = [];
const showToast = (text) => toasts.push(text);
let reloads = 0;
const loadVoiceSettings = () => { reloads++; };
const drain = async () => { for (let i = 0; i < 3; i++) await new Promise(setImmediate); };

saveVoiceSetting("audio.tts_pronunciations", "SQL = sequel");
await drain();
assert.equal(calls.length, 1);
assert.deepEqual(toasts, ["Error: too long"]);
assert.equal(reloads, 0, "a refused save keeps the editor as it is");
""",
    )


def test_resetting_a_setting_asks_first_then_deletes_it() -> None:
    _run(
        _source("function resetVoiceSetting(key) {"),
        """
let confirmed = null;
const showConfirmModal = (title, message, action, onConfirm) => {
  confirmed = {title, message, action, onConfirm};
};
const calls = [];
const authFetch = (url, init) => {
  calls.push([url, init]);
  return Promise.resolve({ok: true, json: () => Promise.resolve({})});
};
const toasts = [];
const showToast = (text) => toasts.push(text);
let reloads = 0;
const loadVoiceSettings = () => { reloads++; };
const drain = async () => { for (let i = 0; i < 3; i++) await new Promise(setImmediate); };

resetVoiceSetting("audio.tts_style");
assert.equal(calls.length, 0, "nothing is removed before the confirm");
assert.equal(confirmed.title, "Reset Setting");
assert.equal(confirmed.action, "Reset");
assert.match(confirmed.message, /audio\\.tts_style/);
confirmed.onConfirm();
await drain();
assert.deepEqual(calls, [["/v1/api/admin/settings/audio.tts_style", {method: "DELETE"}]]);
assert.deepEqual(toasts, ["Reset to default"]);
assert.equal(reloads, 1);
""",
    )
