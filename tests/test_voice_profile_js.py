"""voice.js: the per-model pacing profile editor in Admin › Voice › Settings.

The editor is mostly DOM, so the harness pins the logic that can be wrong
silently: which models the bar offers, how meta fields become Pacing /
Structure / Clips sections, how the override object is built (defaults are
never stored), copy/reset, the default/custom badge, the row markup that has
to stay a .settings-row, and the request body the settings API sees.
"""

from __future__ import annotations

from pathlib import Path

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source

pytestmark = node_skip

_VOICE = Path(__file__).resolve().parents[1] / "turnstone" / "console" / "static" / "voice.js"

_PRELUDE = """
const VOICE_PROFILE_KEY = "audio.tts_profiles";
const VOICE_PROFILE_MAX_STR = 60;
const VOICE_PROFILE_GROUPS = [
  {id: "pacing", label: "Pacing"},
  {id: "structure", label: "Structure"},
  {id: "clips", label: "Clips"},
];
"""

_ESCAPE = """
function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
"""

_FIELDS = """
const pause = {
  key: "pause_heading_ms", group: "pacing", kind: "int", default: 600,
  label: "Silence after a heading", help: "Silence added after a heading.",
  reference_url: "https://example.test/voice.md#pacing",
  min: 0, max: 5000, choices: [], unit: "ms",
};
const joiner = {
  key: "table_cell_joiner", group: "structure", kind: "choice", default: "comma",
  label: "What goes between cells", help: "comma sounds most natural.",
  reference_url: "https://example.test/voice.md#structure",
  min: null, max: null, choices: ["comma", "semicolon", "pause"], unit: "",
};
const marker = {
  key: "code_marker", group: "structure", kind: "str", default: "Code omitted.",
  label: "Said in place of a code block", help: "Leave it empty to skip code.",
  reference_url: "https://example.test/voice.md#structure",
  min: null, max: null, choices: [], unit: "",
};
const clipMin = {
  key: "clip_min_chars", group: "clips", kind: "int", default: 20,
  label: "A shorter line folds into the next clip", help: "Raise it for short lines.",
  reference_url: "https://example.test/voice.md#clips",
  min: 0, max: 200, choices: [], unit: "chars",
};
const fields = [pause, joiner, marker, clipMin];
"""


def _source(*signatures: str, prelude: str = _PRELUDE) -> str:
    body = _VOICE.read_text(encoding="utf-8")
    return prelude + "\n".join(extract_braced(body, sig) for sig in signatures)


def _run(source: str, body: str) -> None:
    result = run_node_source("import assert from 'node:assert/strict';\n" + source + "\n" + body)
    assert result.returncode == 0, result.stderr


def test_the_active_tts_model_is_the_first_option() -> None:
    _run(
        _source(
            "function _voiceProfileModels(meta) {",
            "function _voiceProfileResolveAlias(models, current) {",
        ),
        """
assert.deepEqual(
  _voiceProfileModels({
    tts_models: [
      {alias: "tts-qwen", active: false},
      {alias: "tts-openai", active: true},
      {alias: "tts-qwen", active: false},
      {alias: "  "},
      {alias: 7},
      null,
    ],
  }),
  [
    {alias: "tts-openai", active: true},
    {alias: "tts-qwen", active: false},
  ],
  "active first, one option per alias, junk skipped",
);
assert.deepEqual(_voiceProfileModels(null), []);
assert.deepEqual(_voiceProfileModels({tts_models: []}), []);

// The bar opens on the active model, but keeps a selection that still exists
// across a re-read.
const models = _voiceProfileModels({
  tts_models: [{alias: "a", active: false}, {alias: "b", active: true}],
});
assert.equal(_voiceProfileResolveAlias(models, ""), "b");
assert.equal(_voiceProfileResolveAlias(models, "a"), "a");
assert.equal(_voiceProfileResolveAlias(models, "gone"), "b");
assert.equal(_voiceProfileResolveAlias([], "a"), "");
""",
    )


def test_the_profile_sections_follow_the_contract_order() -> None:
    _run(
        _source("function _voiceProfileGroups(fields) {"),
        _FIELDS
        + """
const groups = _voiceProfileGroups(fields);
assert.deepEqual(groups.map((group) => group.id), ["pacing", "structure", "clips"]);
assert.deepEqual(groups.map((group) => group.label), ["Pacing", "Structure", "Clips"]);
assert.deepEqual(groups[0].fields.map((f) => f.key), ["pause_heading_ms"]);
assert.deepEqual(
  groups[1].fields.map((f) => f.key),
  ["table_cell_joiner", "code_marker"],
  "meta order is kept inside a group",
);
assert.deepEqual(groups[2].fields.map((f) => f.key), ["clip_min_chars"]);

// A group the contract adds later still renders, after the known three; an
// empty group renders nothing.
const extended = _voiceProfileGroups([
  {key: "future_field", group: "speed", label: "Future"},
  {group: "pacing"},
]);
assert.deepEqual(extended.map((group) => group.id), ["speed"]);
assert.deepEqual(extended[0].fields.map((f) => f.key), ["future_field"]);
assert.deepEqual(_voiceProfileGroups([]), []);
""",
    )


def test_a_field_value_is_typed_or_rejected() -> None:
    _run(
        _source("function _voiceProfileScalar(field, raw, maxStrChars) {"),
        _FIELDS
        + """
assert.equal(_voiceProfileScalar(pause, "300", 60), 300);
assert.equal(_voiceProfileScalar(pause, 0, 60), 0, "0 is a real pause");
assert.equal(_voiceProfileScalar(pause, " 42 ", 60), 42);
assert.equal(_voiceProfileScalar(pause, "", 60), undefined);
assert.equal(_voiceProfileScalar(pause, "300.5", 60), undefined);
assert.equal(_voiceProfileScalar(pause, "abc", 60), undefined);
assert.equal(_voiceProfileScalar(pause, true, 60), undefined, "bool is not 1 ms");
assert.equal(_voiceProfileScalar(pause, 5001, 60), undefined);
assert.equal(_voiceProfileScalar(joiner, "semicolon", 60), "semicolon");
assert.equal(_voiceProfileScalar(joiner, "space", 60), undefined);
assert.equal(_voiceProfileScalar(marker, "  Note omitted.  ", 60), "Note omitted.");
assert.equal(_voiceProfileScalar(marker, "", 60), "", "empty marker = skip silently");
assert.equal(_voiceProfileScalar(marker, "x".repeat(61), 60), undefined);
""",
    )


def test_stored_profiles_keep_overrides_only() -> None:
    _run(
        _source(
            "function _voiceProfileScalar(field, raw, maxStrChars) {",
            "function _voiceProfilesFrom(raw, fields, maxStrChars) {",
        ),
        _FIELDS
        + """
assert.deepEqual(_voiceProfilesFrom("", fields, 60), {});
assert.deepEqual(_voiceProfilesFrom("not json", fields, 60), {});
assert.deepEqual(_voiceProfilesFrom("[1, 2]", fields, 60), {});
assert.deepEqual(_voiceProfilesFrom('{"a": 5}', fields, 60), {});

const raw = JSON.stringify({
  " tts-qwen ": {
    pause_heading_ms: 300,
    table_cell_joiner: "semicolon",
    code_marker: "",
    not_a_field: 1,
  },
  defaults: {pause_heading_ms: 600},
  bad_type: {pause_heading_ms: true, table_cell_joiner: "space"},
  too_long: {code_marker: "x".repeat(61)},
  empty: {},
});
assert.deepEqual(_voiceProfilesFrom(raw, fields, 60), {
  "tts-qwen": {
    pause_heading_ms: 300,
    table_cell_joiner: "semicolon",
    code_marker: "",
  },
}, "known fields only; a default value is not stored; aliases are trimmed");
""",
    )


def test_building_the_override_object_drops_defaults() -> None:
    _run(
        _source(
            "function _voiceProfileClone(profiles) {",
            "function _voiceProfileSet(profiles, alias, field, value) {",
        ),
        _FIELDS
        + """
const base = {other: {max_clips: 50}};
const one = _voiceProfileSet(base, "tts-qwen", pause, 300);
assert.deepEqual(one, {other: {max_clips: 50}, "tts-qwen": {pause_heading_ms: 300}});
assert.deepEqual(base, {other: {max_clips: 50}}, "the stored map is not mutated");

const cleared = _voiceProfileSet(one, "tts-qwen", pause, 600);
assert.deepEqual(cleared, {other: {max_clips: 50}}, "a default removes the key");

const second = _voiceProfileSet(one, "tts-qwen", joiner, "pause");
assert.deepEqual(second["tts-qwen"], {
  pause_heading_ms: 300,
  table_cell_joiner: "pause",
});

const emptyMarker = _voiceProfileSet({}, "tts-qwen", marker, "");
assert.deepEqual(emptyMarker, {"tts-qwen": {code_marker: ""}});
assert.equal(
  JSON.stringify(_voiceProfileSet({}, "tts-qwen", marker, "Code omitted.")),
  "{}",
  "an alias with no overrides is removed from the write shape",
);
""",
    )


def test_copying_and_resetting_a_profile() -> None:
    _run(
        _source(
            "function _voiceProfileClone(profiles) {",
            "function _voiceProfileCopy(profiles, from, to) {",
            "function _voiceProfileReset(profiles, alias) {",
            "function _voiceProfileHasOverrides(profiles, alias) {",
        ),
        """
const profiles = {
  a: {pause_heading_ms: 300},
  b: {max_clips: 10},
  c: {},
};
const copied = _voiceProfileCopy(profiles, "a", "b");
assert.deepEqual(copied, {
  a: {pause_heading_ms: 300},
  b: {pause_heading_ms: 300},
  c: {},
}, "the target holds the source's overrides, not a merge");
assert.deepEqual(profiles.b, {max_clips: 10}, "the source map is not mutated");
copied.b.pause_heading_ms = 1;
assert.equal(profiles.a.pause_heading_ms, 300, "the copy is a clone");

assert.deepEqual(
  _voiceProfileCopy(profiles, "missing", "c"),
  {a: {pause_heading_ms: 300}, b: {max_clips: 10}},
  "copying an empty profile clears the target",
);
assert.deepEqual(
  _voiceProfileReset(profiles, "a"),
  {b: {max_clips: 10}, c: {}},
);
assert.deepEqual(profiles.a, {pause_heading_ms: 300});

assert.equal(_voiceProfileHasOverrides(profiles, "a"), true);
assert.equal(_voiceProfileHasOverrides(profiles, "c"), false);
assert.equal(_voiceProfileHasOverrides(profiles, "missing"), false);
""",
    )


def test_the_badge_reads_the_override_map() -> None:
    _run(
        _source(
            "function _voiceProfileBadge(profile, field) {",
            "function _voiceProfileValue(profile, field) {",
        ),
        _FIELDS
        + """
assert.equal(_voiceProfileBadge({}, pause), "default");
assert.equal(_voiceProfileBadge(null, pause), "default");
assert.equal(_voiceProfileBadge({pause_heading_ms: 300}, pause), "custom");
assert.equal(_voiceProfileBadge({pause_heading_ms: 0}, pause), "custom", "0 is not absent");
assert.equal(_voiceProfileBadge({code_marker: ""}, marker), "custom", "empty is a choice");

assert.equal(_voiceProfileValue({}, pause), 600);
assert.equal(_voiceProfileValue({pause_heading_ms: 300}, pause), 300);
assert.equal(_voiceProfileValue(null, marker), "Code omitted.");
""",
    )


def test_a_profile_row_is_a_settings_row() -> None:
    _run(
        _source(
            "function _voiceProfileRowHtml(field, profile, maxStrChars) {",
            "function _voiceProfileValue(profile, field) {",
            "function _voiceProfileBadge(profile, field) {",
            prelude=_ESCAPE,
        ),
        _FIELDS
        + """
const has = (hay, needle) => {
  assert.ok(hay.indexOf(needle) !== -1, "missing: " + needle);
};
const row = _voiceProfileRowHtml(pause, {}, 60);
has(row, 'class="settings-row" data-profile-row="pause_heading_ms"');
has(row, 'class="settings-label-col"');
has(row, 'class="settings-label"');
has(row, 'class="settings-help-btn"');
has(row, 'data-help-target="voice-profile-pause_heading_ms-help"');
has(row, 'Silence added after a heading.');
has(row, 'href="https://example.test/voice.md#pacing"');
has(row, 'class="settings-help-ref">learn more</a>');
has(row, 'type="number" data-profile-field="pause_heading_ms"');
has(row, 'value="600"');
has(row, 'step="1"');
has(row, 'min="0"');
has(row, 'max="5000"');
has(row, 'class="voice-profile-unit">ms</span>');
has(row, 'settings-badge-default">default</span>');
assert.equal(row.indexOf("data-profile-reset"), -1, "a default row offers no reset");

const custom = _voiceProfileRowHtml(pause, {pause_heading_ms: 300}, 60);
has(custom, 'scope-badge scope-write">custom</span>');
has(custom, 'data-profile-reset="pause_heading_ms"');
has(custom, 'value="300"');

const select = _voiceProfileRowHtml(joiner, {}, 60);
has(select, '<select data-profile-field="table_cell_joiner"');
has(select, '<option value="comma" selected>comma</option>');
has(select, '<option value="semicolon">semicolon</option>');

const text = _voiceProfileRowHtml(marker, {}, 60);
has(text, 'type="text" data-profile-field="code_marker"');
has(text, 'maxlength="60"');
has(text, 'value="Code omitted."');

const escaped = _voiceProfileRowHtml(
  {...marker, help: "a < b & c", reference_url: "https://example.test/?a=1&b=2"},
  {},
  60,
);
has(escaped, "a &lt; b &amp; c");
has(escaped, "https://example.test/?a=1&amp;b=2");
""",
    )


def test_a_profile_save_puts_the_whole_json_object() -> None:
    _run(
        _source("function _saveVoiceProfiles() {"),
        """
let _voiceProfiles = {a: {pause_heading_ms: 300}, b: {code_marker: ""}};
let _voiceProfilePending = 0;
const calls = [];
const authFetch = (url, init) => {
  calls.push([url, init]);
  return Promise.resolve({ok: true, json: () => Promise.resolve({})});
};
let reloads = 0;
const loadVoiceSettings = () => { reloads++; };
const errors = [];
const _showVoiceProfileError = (message) => errors.push(message);
const drain = async () => { for (let i = 0; i < 3; i++) await new Promise(setImmediate); };

_saveVoiceProfiles();
await drain();
assert.equal(calls.length, 1);
assert.equal(calls[0][0], "/v1/api/admin/settings/audio.tts_profiles");
assert.equal(calls[0][1].method, "PUT");
assert.deepEqual(calls[0][1].headers, {"Content-Type": "application/json"});
assert.deepEqual(JSON.parse(calls[0][1].body), {
  value: JSON.stringify({a: {pause_heading_ms: 300}, b: {code_marker: ""}}),
});
assert.deepEqual(errors, []);
assert.equal(reloads, 1, "a successful save re-reads the setting");
""",
    )


def test_a_refused_profile_save_shows_the_servers_message() -> None:
    _run(
        _source("function _saveVoiceProfiles() {"),
        """
let _voiceProfiles = {a: {pause_heading_ms: 300}};
let _voiceProfilePending = 0;
const authFetch = () =>
  Promise.resolve({ok: false, json: () => Promise.resolve({error: "not valid JSON: boom"})});
let reloads = 0;
const loadVoiceSettings = () => { reloads++; };
const errors = [];
const _showVoiceProfileError = (message) => errors.push(message);
const drain = async () => { for (let i = 0; i < 3; i++) await new Promise(setImmediate); };

_saveVoiceProfiles();
await drain();
assert.deepEqual(errors, ["not valid JSON: boom"]);
assert.equal(reloads, 0, "a refused save keeps the editor as it is");
""",
    )


def test_the_profile_block_renders_the_bar_and_its_sections() -> None:
    _run(
        _source(
            "function _voiceProfileHtml() {",
            "function _voiceProfileRowHtml(field, profile, maxStrChars) {",
            "function _voiceProfileModels(meta) {",
            "function _voiceProfileGroups(fields) {",
            "function _voiceProfileResolveAlias(models, current) {",
            "function _voiceProfileFor(alias) {",
            "function _voiceProfileHasOverrides(profiles, alias) {",
            "function _voiceProfileValue(profile, field) {",
            "function _voiceProfileBadge(profile, field) {",
            "function _voiceProfileMaxStr() {",
            prelude=_PRELUDE + _ESCAPE,
        ),
        _FIELDS
        + """
let _voiceMeta = {
  tts_models: [
    {alias: "tts-qwen", active: true},
    {alias: "tts-other", active: false},
  ],
  profile_fields: fields,
  limits: {max_marker_chars: 60},
};
let _voiceMetaError = "";
let _voiceProfiles = {"tts-qwen": {pause_heading_ms: 300}};
let _voiceProfileAlias = "";

const has = (hay, needle) => {
  assert.ok(hay.indexOf(needle) !== -1, "missing: " + needle);
};
const html = _voiceProfileHtml();
has(html, "Pacing profile for");
has(html, '<option value="tts-qwen" selected>tts-qwen (active TTS model)</option>');
has(html, '<option value="tts-other">tts-other</option>');
has(html, 'Copy from\\u2026');
has(html, "Each TTS model keeps its own pacing, structure and clip settings.");
has(html, "data-section=\\"voice-profile-pacing\\"");
has(html, "data-section=\\"voice-profile-structure\\"");
has(html, "data-section=\\"voice-profile-clips\\"");
has(html, 'class="settings-section-header" role="button" tabindex="0"');
has(html, 'aria-controls="voice-profile-body-pacing"');
has(html, 'class="settings-section-body" id="voice-profile-body-clips"');
has(html, 'value="300"', "the selected alias's overrides are shown");
has(html, "Reset profile");
assert.equal(_voiceProfileAlias, "tts-qwen", "the bar selection is the active model");

_voiceMeta = null;
assert.equal(_voiceProfileHtml(), "", "nothing renders before meta has loaded");

_voiceMeta = {tts_models: [], profile_fields: fields};
assert.match(
  _voiceProfileHtml(),
  /No model with speech synthesis is configured/,
  "no models -> the one-line explanation, not an empty editor",
);

_voiceMetaError = "boom";
assert.match(_voiceProfileHtml(), /could not be loaded: boom/);
""",
    )
