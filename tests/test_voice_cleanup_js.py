"""Admin › Voice › Text cleanup: the pure helpers behind the tab.

``turnstone/console/static/voice_cleanup.js`` is a classic script.  The DOM
mount, preview debounce and shelf wiring are browser-verified; everything the
tab derives from data — rule parsing, the disabled-group list, position
options, match labels, the AI prompt and the span renderer — is a pure
function exposed under ``window.TurnstoneVoiceCleanup`` and pinned here.

The index.html contract (tab order, host ids, script/css tags) is asserted in
this file too: the Voice shell finds a section by ``<section>-section`` and
``<section>-container``, so a renamed host silently blanks the tab.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests._js_harness_helpers import node_skip, run_node_source

pytestmark = node_skip

_ROOT = Path(__file__).resolve().parents[1]
_JS = _ROOT / "turnstone/console/static/voice_cleanup.js"
_INDEX = _ROOT / "turnstone/console/static/index.html"

_HARNESS = """
import assert from 'node:assert/strict';
globalThis.window = globalThis;
globalThis.document = {createElement: function () { return {}; }};
globalThis.TS_VOICE = {
  registerSection: function (section, loader) {
    globalThis.__registered = {section: section, loader: loader};
  },
};
// The real escapeHtml (utils.js) round-trips through a DOM node; the node
// harness has none, so use the same five replacements it performs.
function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
%(src)s
const VC = globalThis.TurnstoneVoiceCleanup;
%(body)s
"""


def _run(body: str) -> None:
    # Plain replacement, not % formatting: the JS under test is free to
    # contain a literal percent sign.
    source = _HARNESS.replace("%(src)s", _JS.read_text(encoding="utf-8")).replace("%(body)s", body)
    result = run_node_source(source)
    assert result.returncode == 0, result.stderr


# -- module surface and the shell contract ----------------------------------


def test_module_exposes_the_entry_point_and_pure_helpers() -> None:
    _run(
        """
assert.equal(typeof window.loadVoiceCleanup, "function");
assert.deepEqual(__registered.section, "voice-cleanup");
assert.equal(__registered.loader, window.loadVoiceCleanup);
for (const name of [
  "parseRules",
  "serializeRules",
  "parseDisabled",
  "toggleDisabled",
  "matchCountLabel",
  "changesLabel",
  "highlightSegments",
  "positionOptions",
  "applyRuleAt",
  "buildAiPrompt",
  "previewBody",
  "draftPreviewBody",
  "validateRuleDraft",
  "settingRowHtml",
  "groupsTableHtml",
  "rulesTableHtml",
  "ruleTypeLabel",
  "exampleText",
]) {
  assert.equal(typeof VC[name], "function", name + " must be exposed for the harness");
}
assert.equal(VC.VOICE_DOCS_URL, "https://github.com/turnstonelabs/turnstone/blob/main/docs/voice.md");
assert.deepEqual(VC.DEFAULT_LIMITS, {
  max_rules: 100,
  max_find_chars: 500,
  max_replace_chars: 500,
  max_test_chars: 500,
});
"""
    )


def test_contract_strings_and_shelf_copy_are_present() -> None:
    body = _JS.read_text(encoding="utf-8")
    for needle in [
        "audio.tts_normalize",
        "audio.tts_rewrite_rules",
        "audio.tts_cleanup_disabled",
        "/v1/api/admin/voice/meta",
        "/v1/api/admin/voice/preview",
        "/v1/api/admin/settings/schema",
        '"admin.settings"',
        "RWR-NEW",
        "RWR-EDIT",
        "Type the word or phrase exactly as it appears. Matches whole words and ignores case:",
        "Find is now a regular expression (Python flavor), case-sensitive; start with (?i) to ignore case.",
        "Don't write regex?",
        "after rule groups, before the dictionary",
        "Dictionary entries still win",
        "https://regex101.com/?flavor=python",
        "#text-cleanup",
        "#rule-groups",
        "#custom-rewrite-rules",
        "Runs in order, left to right like the tabs",
        "What the TTS model receives",
        "Built in; turn off any your model already reads well",
        "Custom rewrite rules",
        "for anything the groups don't cover",
        "text = the words you typed, matched whole-word and ignoring case",
        "Read-aloud only: dictation uses the dictionary",
    ]:
        assert needle in body, f"voice_cleanup.js lost {needle!r}"


def test_the_voice_shell_declares_the_text_cleanup_tab() -> None:
    html = _INDEX.read_text(encoding="utf-8")
    settings_btn = html.index('id="voice-tab-settings"')
    cleanup_btn = html.index('id="voice-tab-cleanup"')
    dictionary_btn = html.index('id="voice-tab-dictionary"')
    assert settings_btn < cleanup_btn < dictionary_btn, (
        "Text cleanup sits between Settings and Dictionary"
    )
    assert 'data-section="voice-cleanup"' in html
    assert 'aria-controls="voice-cleanup-section"' in html
    assert "Text cleanup" in html[cleanup_btn : cleanup_btn + 400]

    # _loadVoiceSection resolves both hosts as "<section>-section" and
    # "<section>-container"; anything else silently blanks the tab.
    assert 'id="voice-cleanup-section"' in html
    assert 'class="voice-section"' in html
    assert 'id="voice-cleanup-container"' in html
    assert 'id="voice-cleanup-section"' in html and html.index(
        'id="voice-cleanup-section"'
    ) < html.index('id="voice-dictionary-section"')

    assert '<link rel="stylesheet" href="/static/voice_cleanup.css" />' in html
    assert '<script src="/static/voice_cleanup.js"></script>' in html


def test_voice_cleanup_js_parses() -> None:
    """A syntax error in the IIFE would blank the tab; the sliced unit tests
    would still pass."""
    proc = subprocess.run(
        ["node", "--check", str(_JS)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr


# -- stored rules -----------------------------------------------------------


def test_parse_rules_is_tolerant_and_canonicalises_fields() -> None:
    _run(
        """
const L = VC.DEFAULT_LIMITS;
for (const raw of ["", "   ", "not json", "{}", "42", "null", '"x"']) {
  assert.deepEqual(VC.parseRules(raw, L), [], JSON.stringify(raw));
}
assert.deepEqual(VC.parseRules(undefined, L), []);

const items = [
  "nope",
  {find: ""},
  {find: "x".repeat(501)},
  {find: "ok", regex: "yes"},
  {find: "ok", replace: 7},
  {find: "ok", replace: "y".repeat(501)},
  {find: "ok", test: "z".repeat(501)},
  {find: "ok", enabled: "no"},
  {find: " k8s ", replace: "kubernetes"},
  {find: "a", replace: null, test: " a ", enabled: false},
];
assert.deepEqual(VC.parseRules(JSON.stringify(items), L), [
  {find: "k8s", regex: false, replace: "kubernetes", test: "", enabled: true},
  {find: "a", regex: false, replace: "", test: "a", enabled: false},
]);

const many = Array.from({length: 150}, (_, i) => ({find: "f" + i}));
assert.equal(VC.parseRules(JSON.stringify(many), L).length, 100);
assert.equal(
  VC.parseRules(JSON.stringify(many), {max_rules: 2, max_find_chars: 500, max_replace_chars: 500, max_test_chars: 500}).length,
  2,
);
"""
    )


def test_serialize_rules_uses_the_contract_field_order() -> None:
    _run(
        """
assert.equal(
  VC.serializeRules([{
    find: "k8s",
    regex: false,
    replace: "kubernetes",
    test: "to k8s",
    enabled: true,
  }]),
  '[{"find":"k8s","regex":false,"replace":"kubernetes","test":"to k8s","enabled":true}]',
);
assert.equal(VC.serializeRules([]), "[]");
assert.equal(
  VC.serializeRules([{find: "x", regex: true}]),
  '[{"find":"x","regex":true,"replace":"","test":"","enabled":true}]',
);
"""
    )


def test_validate_rule_draft_matches_the_server_caps() -> None:
    _run(
        """
const L = VC.DEFAULT_LIMITS;
assert.equal(VC.validateRuleDraft({find: "k8s", replace: "", test: ""}, L), "");
assert.match(VC.validateRuleDraft({find: "  "}, L), /Find is required/);
assert.match(VC.validateRuleDraft({find: "x".repeat(501)}, L), /500/);
assert.match(VC.validateRuleDraft({find: "x", replace: "y".repeat(501)}, L), /500/);
assert.match(VC.validateRuleDraft({find: "x", test: "z".repeat(501)}, L), /500/);
assert.match(
  VC.validateRuleDraft({find: "x".repeat(13)}, {max_rules: 100, max_find_chars: 12, max_replace_chars: 500, max_test_chars: 500}),
  /12/,
);
"""
    )


# -- disabled groups --------------------------------------------------------


def test_parse_disabled_keeps_known_ids_only_and_dedupes() -> None:
    _run(
        """
assert.deepEqual(
  VC.parseDisabled('["units", "versions", "units", "colours"]', ["units", "versions", "identifiers"]),
  ["units", "versions"],
);
assert.deepEqual(VC.parseDisabled('["units", 3, null]'), ["units"]);
for (const raw of ["", "not json", "{}", "42", "null"]) {
  assert.deepEqual(VC.parseDisabled(raw, ["units"]), []);
}
assert.deepEqual(VC.parseDisabled(undefined), []);
"""
    )


def test_toggle_disabled_adds_and_removes_without_mutating() -> None:
    _run(
        """
const base = ["units", "versions"];
assert.deepEqual(VC.toggleDisabled(base, "versions", true), ["units"]);
assert.deepEqual(VC.toggleDisabled(base, "dates", false), ["units", "versions", "dates"]);
assert.deepEqual(VC.toggleDisabled(base, "dates", true), base);
assert.deepEqual(VC.toggleDisabled(undefined, "units", false), ["units"]);
assert.deepEqual(VC.toggleDisabled(base, "units", false), base, "already off is unchanged");
assert.deepEqual(base, ["units", "versions"], "the input list is not mutated");
"""
    )


# -- preview labels and highlighting ---------------------------------------


def test_match_and_change_labels() -> None:
    _run(
        """
assert.equal(VC.matchCountLabel(0, false), "No match in the test line");
assert.equal(VC.matchCountLabel(0, true), "No match in the test line");
assert.equal(VC.matchCountLabel(1, false), "1 match");
assert.equal(VC.matchCountLabel(2, false), "2 matches");
assert.equal(VC.matchCountLabel(2, true), "2 matches \\u00b7 removed");
assert.equal(VC.changesLabel(0), "0 changes");
assert.equal(VC.changesLabel(1), "1 change");
assert.equal(VC.changesLabel(3), "3 changes");
"""
    )


def test_highlight_segments_escapes_every_piece_of_the_reply() -> None:
    _run(
        """
const text = "a <b> & c";
const html = VC.highlightSegments(
  text,
  [{start: 2, end: 5, source: "custom", label: "custom 1"}],
  function (span) { return span.label; },
);
assert.ok(html.startsWith("a "));
assert.ok(html.includes("&lt;b&gt;"));
assert.ok(!html.includes("<b>"), "reply markup is never injected");
assert.ok(html.includes("&amp;"));
assert.ok(html.includes('class="voice-cleanup-hl voice-cleanup-hl--custom"'));
assert.ok(html.includes('title="custom 1"'));

const grouped = VC.highlightSegments(
  "120ms and YAML",
  [
    {start: 0, end: 5, source: "group", label: "units"},
    {start: 10, end: 14, source: "dictionary", label: "dictionary"},
  ],
  function (span) { return span.source === "group" ? "units (built in)" : span.label; },
);
assert.ok(grouped.includes("voice-cleanup-hl--group"));
assert.ok(grouped.includes('title="units (built in)"'));
assert.ok(grouped.includes("voice-cleanup-hl--dictionary"));

// Out-of-range and overlapping spans are clipped/skipped, never allowed to
// duplicate or reorder the text.
const messy = VC.highlightSegments("abcdef", [
  {start: -2, end: 3, source: "group", label: "a"},
  {start: 2, end: 4, source: "custom", label: "b"},
  {start: 20, end: 30, source: "group", label: "c"},
]);
assert.ok(messy.includes(">abc</mark>"));
assert.ok(messy.includes("def"));
assert.equal((messy.match(/<mark/g) || []).length, 1);
assert.equal(VC.highlightSegments("plain", []), "plain");
"""
    )


# -- placement --------------------------------------------------------------


def test_position_options_describe_first_between_and_last() -> None:
    _run(
        """
const rules = [{find: "k8s"}, {find: "PR"}];
assert.deepEqual(VC.positionOptions(rules, null), [
  {value: 0, label: "First, before rule 1"},
  {value: 1, label: "After 1 \\u00b7 k8s"},
  {value: 2, label: "After 2 \\u00b7 PR (last)"},
]);
assert.deepEqual(VC.positionOptions([], null), [{value: 0, label: "First"}]);

// Editing excludes the rule itself: its own position is not "after itself".
assert.deepEqual(VC.positionOptions(rules, 0), [
  {value: 0, label: "First, before rule 1"},
  {value: 1, label: "After 1 \\u00b7 PR (last)"},
]);
assert.deepEqual(VC.positionOptions(rules, 1), [
  {value: 0, label: "First, before rule 1"},
  {value: 1, label: "After 1 \\u00b7 k8s (last)"},
]);
"""
    )


def test_apply_rule_at_inserts_and_moves() -> None:
    _run(
        """
const rules = [{find: "a"}, {find: "b"}, {find: "c"}];
const rule = {find: "x"};
const finds = (list) => list.map((item) => item.find);
assert.deepEqual(finds(VC.applyRuleAt(rules, null, 1, rule)), ["a", "x", "b", "c"]);
assert.deepEqual(finds(VC.applyRuleAt(rules, null, 3, rule)), ["a", "b", "c", "x"]);
assert.deepEqual(finds(VC.applyRuleAt(rules, null, 99, rule)), ["a", "b", "c", "x"]);
assert.deepEqual(finds(VC.applyRuleAt(rules, 0, 2, rule)), ["b", "c", "x"]);
assert.deepEqual(finds(VC.applyRuleAt(rules, 2, 0, rule)), ["x", "a", "b"]);
assert.deepEqual(finds(VC.applyRuleAt(rules, 1, 1, rule)), ["a", "x", "c"]);
assert.deepEqual(finds(rules), ["a", "b", "c"], "the input list is not mutated");
"""
    )


# -- AI prompt --------------------------------------------------------------


def test_build_ai_prompt_includes_the_engine_contract_and_draft() -> None:
    _run(
        """
const prompt = VC.buildAiPrompt({
  find: "DT-(\\\\d+)",
  regex: true,
  replace: "D T \\\\1",
  test: "Ticket DT-1412 is open",
});
for (const needle of [
  "Write one custom rewrite rule for Turnstone read-aloud",
  "Engine: Python `re`, applied with re.sub.",
  "Case-sensitive. Start the pattern with (?i) to ignore case.",
  "It sees the reply as displayed, not the markdown source",
  "It runs on one clip at a time",
  "Dictionary respellings run after all rules and override them.",
  "The replacement is what the voice should SAY",
  "must not be able to match empty text, and is at most 500 characters.",
  "What I want: <describe it, e.g. \\"read ticket ids like DT-1412 as D T 1412\\">",
  "Example line: Ticket DT-1412 is open",
  "It should be read as: <how it should sound>",
  "My attempt so far: DT-(\\\\d+)  ->  D T \\\\1",
  "Reply with the pattern and the replacement on separate lines",
]) {
  assert.ok(prompt.includes(needle), "prompt lost: " + needle);
}
assert.ok(!prompt.endsWith("\\n"));
"""
    )


def test_build_ai_prompt_handles_empty_fields_and_the_delete_case() -> None:
    _run(
        """
const empty = VC.buildAiPrompt({find: "", regex: false, replace: "", test: ""});
assert.ok(empty.includes("Example line: <paste a line from a reply>"));
assert.ok(!empty.includes("My attempt so far"));

const deleting = VC.buildAiPrompt({find: "x", replace: "", test: "a x b"});
assert.ok(deleting.includes('My attempt so far: x  ->  (delete)'));

const custom = VC.buildAiPrompt(
  {find: "x", replace: "y", test: "a x"},
  {max_rules: 100, max_find_chars: 120, max_replace_chars: 500, max_test_chars: 500},
);
assert.ok(custom.includes("at most 120 characters"));
"""
    )


# -- preview request bodies -------------------------------------------------


def test_preview_bodies_follow_the_http_contract() -> None:
    _run(
        """
assert.deepEqual(VC.previewBody("hello"), {text: "hello"});
assert.deepEqual(VC.previewBody("hello", [], true), {
  text: "hello",
  rules: [],
  only_rules: true,
});

const draft = {find: "x", regex: false, replace: "y", test: "a x", enabled: false};
assert.deepEqual(VC.draftPreviewBody(draft), {
  text: "a x",
  rules: [{find: "x", regex: false, replace: "y", test: "a x", enabled: true}],
  only_rules: true,
});
assert.equal(VC.draftPreviewBody({find: "x", test: ""}).text, "sample");
"""
    )


# -- markup -----------------------------------------------------------------


def test_setting_row_reuses_the_settings_component_and_docs_link() -> None:
    _run(
        """
const html = VC.settingRowHtml({
  key: "audio.tts_normalize",
  description: "Rewrite symbols",
  help: "Long help text",
  reference_url: VC.VOICE_DOCS_URL + "#text-cleanup",
  value: false,
  type: "bool",
});
assert.ok(html.includes('class="settings-row" data-row-key="audio.tts_normalize"'));
assert.ok(html.includes('data-setting-key="audio.tts_normalize"'));
assert.ok(html.includes('class="settings-help-btn"'));
assert.ok(html.includes('class="settings-help-popover"'));
assert.ok(html.includes("Long help text"));
assert.ok(html.includes("#text-cleanup"));
assert.ok(!html.includes(" checked"));

const on = VC.settingRowHtml({
  key: "audio.tts_normalize",
  description: "d",
  help: "h",
  reference_url: VC.VOICE_DOCS_URL + "#text-cleanup",
  value: "true",
  type: "bool",
});
assert.ok(on.includes(" checked"));

const escaped = VC.settingRowHtml({
  key: "audio.tts_normalize",
  description: "<b>bold</b>",
  help: "<i>help</i>",
  reference_url: "javascript:alert(1)",
  value: true,
  type: "bool",
});
assert.ok(!escaped.includes("<b>"));
assert.ok(!escaped.includes("<i>"));
assert.ok(!escaped.includes("javascript:"), "only http(s) reference links are rendered");
"""
    )


def test_groups_table_renders_examples_and_switches() -> None:
    _run(
        """
const groups = [
  {id: "units", label: "units", description: "Size units", example: "120ms", result: "120 milliseconds"},
  {id: "dates", label: "dates", description: "ISO dates", example: "2026-10-16", result: "October 16th, 2026"},
];
const html = VC.groupsTableHtml(groups, ["dates"], true);
assert.ok(html.includes('data-vc-group="units"'));
assert.ok(html.includes('data-vc-group="dates"'));
assert.ok(html.includes("120ms"));
assert.ok(html.includes("120 milliseconds"));
assert.ok(html.includes("\\u2192"));
assert.equal((html.match(/ checked/g) || []).length, 1, "the disabled group is the only unchecked one");
assert.ok(html.includes("Size units"));

const readonly = VC.groupsTableHtml(groups, [], false);
assert.ok(readonly.includes(" disabled"));
assert.ok(!readonly.includes('data-vc-group="units" checked'));

const empty = VC.groupsTableHtml([], [], true);
assert.ok(empty.includes("No cleanup groups"));
"""
    )


def test_rules_table_renders_every_column_and_the_just_added_pill() -> None:
    _run(
        """
const limits = VC.DEFAULT_LIMITS;
const rules = [
  {find: "k8s", regex: false, replace: "kubernetes", test: "to k8s", enabled: true},
  {find: "<b>", regex: true, replace: "", test: "", enabled: false},
];
const html = VC.rulesTableHtml(rules, true, 0, limits);
assert.ok(html.includes(">text<"));
assert.ok(html.includes(">regex<"));
assert.ok(html.includes("just added"));
assert.ok(html.includes('data-vc-rule-edit="0"'));
assert.ok(html.includes('data-vc-rule-delete="1"'));
assert.ok(html.includes('data-vc-rule-toggle="0"'));
assert.ok(html.includes('data-vc-example="0"'));
assert.ok(html.includes("to k8s"));
assert.ok(!html.includes("<b>"), "a rule's find is escaped, not injected");
assert.ok(html.includes("&lt;b&gt;"));

const readonly = VC.rulesTableHtml(rules, false, -1, limits);
assert.ok(!readonly.includes("data-vc-rule-edit"));
assert.ok(!readonly.includes("data-vc-rule-delete"));
assert.ok(!readonly.includes("just added"));
assert.ok(readonly.includes(" disabled"));

const empty = VC.rulesTableHtml([], true, -1, limits);
assert.ok(empty.includes("No custom rewrite rules yet."));
"""
    )


def test_rule_type_and_example_labels() -> None:
    _run(
        """
assert.equal(VC.ruleTypeLabel({regex: false}), "text");
assert.equal(VC.ruleTypeLabel({regex: true}), "regex");
assert.equal(VC.exampleText({test: "to k8s"}, "to kubernetes"), "to k8s \\u2192 to kubernetes");
assert.equal(VC.exampleText({test: ""}, "ignored"), "\\u2014");
assert.equal(VC.exampleText({test: "x"}, null), "x \\u2192 \\u2026");
"""
    )


def test_sample_for_appends_one_enabled_rule_test_line() -> None:
    _run(
        """
assert.equal(VC.sampleFor([]), VC.SAMPLE_TEXT);
assert.equal(
  VC.sampleFor([{find: "x", test: "Ticket DT-1412", enabled: false}]),
  VC.SAMPLE_TEXT,
  "a switched-off rule is not exercised by the sample",
);
assert.equal(
  VC.sampleFor([{find: "x", test: "Please reboot the node", enabled: true}]),
  VC.SAMPLE_TEXT + "\\n\\nPlease reboot the node",
);
// A test line already present is not appended twice.
assert.equal(
  VC.sampleFor([{find: "x", test: "Deploy v1.8.5 to the 32GB box", enabled: true}]),
  VC.SAMPLE_TEXT,
);
"""
    )
