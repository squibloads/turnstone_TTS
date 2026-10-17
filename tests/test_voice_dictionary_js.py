"""Behavioral tests for the Admin › Voice › Dictionary slice
(``turnstone/console/static/voice_dictionary.js``): the tolerant reader, the
legacy ``audio.tts_pronunciations`` import, the list/notice markup, form
validation and the settings PUT body, driven through ``node``.

The DOM instance (shelf fields, click wiring) is browser-verified only: the
node harness has no DOMParser.  Everything the list derives from data is a
pure function and pinned here.
"""

from __future__ import annotations

from pathlib import Path

from tests._js_harness_helpers import node_skip, run_node_source

pytestmark = node_skip

_ROOT = Path(__file__).resolve().parents[1]
_DICTIONARY_JS = _ROOT / "turnstone/console/static/voice_dictionary.js"

_HARNESS = """
import assert from 'node:assert/strict';
globalThis.window = globalThis;
globalThis.document = {createElement: function () { return {}; }};
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
const VD = globalThis.TurnstoneVoiceDictionary;
%(body)s
"""


def _run(body: str) -> None:
    source = _HARNESS % {
        "src": _DICTIONARY_JS.read_text(encoding="utf-8"),
        "body": body,
    }
    result = run_node_source(source)
    assert result.returncode == 0, result.stderr


# -- module surface ---------------------------------------------------------


def test_module_exposes_the_entry_point_and_pure_helpers() -> None:
    _run(
        """
assert.equal(typeof window.loadVoiceDictionary, "function");
for (const name of [
  "parseEntries",
  "canonicalJson",
  "parseLegacyPronunciations",
  "importLegacyEntries",
  "importEntries",
  "sayAs",
  "rowsHtml",
  "renderHtml",
  "splitAlsoHears",
  "validateEntry",
  "putSetting",
]) {
  assert.equal(typeof VD[name], "function", name + " must be exposed for the harness");
}
assert.equal(VD.MAX_ENTRIES, 200);
"""
    )


def test_contract_strings_and_shelf_copy_are_present() -> None:
    """The settings keys, permission scope, shelf tags, hint copy and the
    hatch/confirm entry points the Voice shell must find."""
    body = _DICTIONARY_JS.read_text(encoding="utf-8")
    for needle in [
        "audio.voice_dictionary",
        "audio.tts_pronunciations",
        '"admin.settings"',
        "used by every model",
        "only for models that accept IPA; others use the respelling",
        "When a transcript contains one of these as a whole phrase, it becomes",
        "DCT-NEW",
        "DCT-EDIT",
        "window.TurnstoneHatch.openShelf",
        "window.TurnstoneHatch.setBusy",
        "showConfirmModal",
        "data-vd-import",
    ]:
        assert needle in body, f"voice_dictionary.js lost {needle!r}"


# -- dictionary: tolerant parse --------------------------------------------


def test_parse_entries_degrades_bad_json_to_empty() -> None:
    _run(
        """
for (const raw of ["", "   ", "not json", "{}", "42", "null", '"x"']) {
  assert.deepEqual(VD.parseEntries(raw), [], JSON.stringify(raw));
}
assert.deepEqual(VD.parseEntries(undefined), []);
"""
    )


def test_parse_entries_defaults_missing_fields() -> None:
    _run(
        """
assert.deepEqual(
  VD.parseEntries(JSON.stringify([{term: " SQL ", respelling: " sequel "}])),
  [{
    term: "SQL",
    kind: "respelling",
    respelling: "sequel",
    ipa: "",
    match_case: false,
    also_hears: [],
  }],
);
"""
    )


def test_parse_entries_skips_bad_entries_and_keeps_good_ones() -> None:
    _run(
        """
const items = [
  "nope",
  {term: ""},
  {term: "x".repeat(101), respelling: "y"},
  {term: "lonely"},
  {term: "A", kind: "ssml", respelling: "x"},
  {term: "B", kind: "ipa", respelling: "engine ex"},  // ipa is required for this kind
  {term: "C", respelling: "x", match_case: "yes"},
  {term: "D", respelling: "x", also_hears: "Bay A"},
  {term: "E", respelling: "x", also_hears: ["ok", 3]},
  {term: "SQL", respelling: "sequel", also_hears: []},
];
assert.deepEqual(
  VD.parseEntries(JSON.stringify(items)).map((entry) => entry.term),
  ["SQL"],
);
"""
    )


def test_parse_entries_drops_ipa_from_respelling_entries() -> None:
    _run(
        """
const got = VD.parseEntries(JSON.stringify([{
  term: "BAA",
  kind: "respelling",
  respelling: "B A A",
  ipa: "/x/",
  match_case: true,
  also_hears: ["Bay A"],
}]));
assert.equal(got[0].ipa, "");
assert.equal(got[0].match_case, true);
"""
    )


def test_parse_entries_later_duplicate_wins_and_takes_the_later_position() -> None:
    _run(
        """
const got = VD.parseEntries(JSON.stringify([
  {term: "sql", respelling: "S Q L", match_case: false},
  {term: "SQL", respelling: "sequel", match_case: false},
  {term: "SQL", respelling: "ess cue ell", match_case: true},
]));
// case-insensitive "sql"/"SQL" collide; the case-sensitive one is distinct
assert.deepEqual(
  got.map((entry) => [entry.term, entry.respelling, entry.match_case]),
  [["SQL", "sequel", false], ["SQL", "ess cue ell", true]],
);

// Python's tolerant parser re-inserts, so a replaced entry moves to the end.
const moved = VD.parseEntries(JSON.stringify([
  {term: "a", respelling: "one"},
  {term: "b", respelling: "two"},
  {term: "A", respelling: "three"},
]));
assert.deepEqual(moved.map((entry) => entry.term), ["b", "A"]);
"""
    )


def test_parse_entries_caps_entries_and_also_hears() -> None:
    _run(
        """
const many = Array.from({length: 210}, (_, i) => ({term: "t" + i, respelling: "r"}));
assert.equal(VD.parseEntries(JSON.stringify(many)).length, 200);

const manyHears = VD.parseEntries(JSON.stringify([{
  term: "x",
  respelling: "y",
  also_hears: Array.from({length: 15}, (_, i) => "h" + i),
}]));
assert.equal(manyHears[0].also_hears.length, 10);
"""
    )


# -- legacy pronunciations --------------------------------------------------


def test_parse_legacy_pronunciations_ignores_comments_blanks_and_bad_lines() -> None:
    _run(
        """
const raw = [
  "# SQL is the old spelling",
  "",
  "SQL = sequel",
  "  nginx = engine ex  ",
  "broken line",
  "empty =",
  "= nothing",
  "too-long = " + "x".repeat(101),
].join("\\n");
assert.deepEqual(VD.parseLegacyPronunciations(raw), [
  {term: "SQL", respelling: "sequel"},
  {term: "nginx", respelling: "engine ex"},
]);
assert.deepEqual(VD.parseLegacyPronunciations(""), []);
"""
    )


def test_parse_legacy_pronunciations_later_line_wins_and_keys_stay_distinct() -> None:
    _run(
        """
assert.deepEqual(VD.parseLegacyPronunciations("SQL = sequel\\nSQL = ess cue ell"), [
  {term: "SQL", respelling: "ess cue ell"},
]);
// The server's parser keys this setting case-sensitively, so `SQL` and `sql`
// are two lines there; the import folds them case-insensitively.
assert.equal(VD.parseLegacyPronunciations("SQL = a\\nsql = b").length, 2);
"""
    )


def test_import_skips_terms_already_in_the_dictionary() -> None:
    _run(
        """
const existing = [
  {term: "SQL", kind: "respelling", respelling: "sequel", ipa: "", match_case: false, also_hears: []},
];
const legacy = VD.parseLegacyPronunciations(
  "# comment\\nSQL = ess cue ell\\nsql = ess cue ell\\nnginx = engine ex\\nBAA = B A A",
);
const result = VD.importLegacyEntries(existing, legacy);
assert.equal(result.overflow, false);
assert.equal(result.added, 2);
assert.deepEqual(result.entries.map((entry) => entry.term), ["SQL", "nginx", "BAA"]);
assert.deepEqual(result.entries[1], {
  term: "nginx",
  kind: "respelling",
  respelling: "engine ex",
  ipa: "",
  match_case: false,
  also_hears: [],
});
assert.equal(existing.length, 1, "the input list is not mutated");
"""
    )


def test_import_refuses_to_overflow_the_200_entry_cap() -> None:
    _run(
        """
const entry = (term) => ({
  term,
  kind: "respelling",
  respelling: "r",
  ipa: "",
  match_case: false,
  also_hears: [],
});
const legacy = VD.parseLegacyPronunciations("a = one\\nb = two");

const full = Array.from({length: 199}, (_, i) => entry("t" + i));
const over = VD.importLegacyEntries(full, legacy);
assert.equal(over.overflow, true);
assert.equal(over.added, 0);
assert.equal(over.entries.length, 199, "an overflowing import leaves the list alone");

const room = Array.from({length: 198}, (_, i) => entry("t" + i));
const fits = VD.importLegacyEntries(room, legacy);
assert.equal(fits.overflow, false);
assert.equal(fits.added, 2);
assert.equal(fits.entries.length, 200);
"""
    )


def test_import_folds_legacy_case_variants_with_the_later_line_winning() -> None:
    _run(
        """
// Read-aloud lower-cases legacy keys before applying them, so `sql` and
// `SQL` were always one term whose last line won.
const legacy = VD.parseLegacyPronunciations("sql = ess cue ell\\nSQL = sequel");
const result = VD.importLegacyEntries([], legacy);
assert.equal(result.added, 1);
assert.equal(result.entries.length, 1);
assert.equal(result.entries[0].term, "sql");
assert.equal(result.entries[0].respelling, "sequel");
"""
    )


def test_canonical_json_uses_the_contract_field_order() -> None:
    _run(
        """
assert.equal(
  VD.canonicalJson([{
    term: "SQL",
    kind: "respelling",
    respelling: "sequel",
    ipa: "",
    match_case: false,
    also_hears: ["S Q L"],
  }]),
  '[{"term":"SQL","kind":"respelling","respelling":"sequel","ipa":"","match_case":false,"also_hears":["S Q L"]}]',
);
"""
    )


# -- form state -------------------------------------------------------------


def test_split_also_hears_trims_and_drops_blanks() -> None:
    _run(
        """
assert.deepEqual(VD.splitAlsoHears(" Bay A, ,bay ah ,"), ["Bay A", "bay ah"]);
assert.deepEqual(VD.splitAlsoHears(""), []);
assert.deepEqual(VD.splitAlsoHears(undefined), []);
"""
    )


def test_entry_from_fields_trims_and_drops_ipa_outside_the_ipa_kind() -> None:
    _run(
        """
assert.deepEqual(
  VD.entryFromFields({
    term: "  SQL ",
    kind: "respelling",
    respelling: " sequel ",
    ipa: "/x/",
    match_case: true,
    also_hears: ["S Q L"],
  }),
  {
    term: "SQL",
    kind: "respelling",
    respelling: "sequel",
    ipa: "",
    match_case: true,
    also_hears: ["S Q L"],
  },
);
assert.equal(
  VD.entryFromFields({term: "nginx", kind: "ipa", respelling: "engine ex", ipa: " /x/ "}).ipa,
  "/x/",
);
// An unknown/missing kind falls back to the universal respelling form.
assert.equal(VD.entryFromFields({term: "x", kind: "ssml"}).kind, "respelling");
"""
    )


def test_validate_entry_matches_the_contract() -> None:
    _run(
        """
const base = {term: "x", kind: "respelling", respelling: "y", ipa: "", match_case: false, also_hears: []};
assert.equal(VD.validateEntry(base), "");
assert.equal(VD.validateEntry({...base, respelling: "", also_hears: ["ex"]}), "");
assert.equal(VD.validateEntry({...base, kind: "ipa", respelling: "", ipa: "/x/"}), "");

assert.match(VD.validateEntry({...base, term: "  "}), /Term/);
assert.match(VD.validateEntry({...base, kind: "ipa", respelling: "", ipa: ""}), /IPA/);
assert.match(VD.validateEntry({...base, respelling: ""}), /respelling|also hears/i);
assert.match(VD.validateEntry({...base, also_hears: Array(11).fill("h")}), /10/);
"""
    )


def test_say_as_labels_ipa_entries_and_dictation_only_entries() -> None:
    _run(
        """
const entry = (kw) => Object.assign({
  term: "x", kind: "respelling", respelling: "y", ipa: "", match_case: false, also_hears: [],
}, kw);
assert.equal(VD.sayAs(entry({})), "y");
assert.equal(VD.sayAs(entry({kind: "ipa", respelling: "engine ex", ipa: "/x/"})), "/x/ else \\u201cengine ex\\u201d");
assert.equal(VD.sayAs(entry({kind: "ipa", respelling: "", ipa: "/x/"})), "/x/");
assert.equal(VD.sayAs(entry({respelling: "", also_hears: ["cube"]})), "\\u2014");
"""
    )


# -- markup -----------------------------------------------------------------


def test_rows_render_every_column_and_respect_write_permission() -> None:
    _run(
        """
const entries = [
  {term: "BAA", kind: "respelling", respelling: "B A A", ipa: "", match_case: true, also_hears: ["Bay A", "bay ah"]},
  {term: "nginx", kind: "ipa", respelling: "engine ex", ipa: "/x/", match_case: false, also_hears: []},
  {term: "kubectl", kind: "respelling", respelling: "", ipa: "", match_case: false, also_hears: ["cube control"]},
];
const html = VD.rowsHtml(entries, true);
assert.ok(html.includes("<code>BAA</code>"), "term is monospace");
assert.ok(html.includes("Bay A, bay ah"));
assert.ok(html.includes(">Aa<"), "match_case reads as Aa");
assert.ok(html.includes("/x/ else \\u201cengine ex\\u201d"));
assert.ok(html.includes("data-vd-edit=\\"0\\""));
assert.ok(html.includes("data-vd-delete=\\"2\\""));
assert.ok(html.includes("\\u2014"), "empty also_hears and case-off read as a dash");

const readonly = VD.rowsHtml(entries, false);
assert.ok(!readonly.includes("data-vd-edit"));
assert.ok(!readonly.includes("data-vd-delete"));
assert.ok(!readonly.includes(">Edit<"));

const escaped = VD.rowsHtml([
  {term: "<b>&\\"x\\"</b>", kind: "respelling", respelling: "y", ipa: "", match_case: false, also_hears: []},
], true);
assert.ok(!escaped.includes("<b>"), "term markup is escaped, not injected");
assert.ok(escaped.includes("&lt;b&gt;"));
"""
    )


def test_render_shows_the_cap_count_empty_state_and_legacy_notice() -> None:
    _run(
        """
const empty = VD.renderHtml([], 0, true);
assert.ok(empty.includes("0 of 200 entries"));
assert.ok(empty.includes("No dictionary entries yet."));
assert.ok(empty.includes("data-vd-new"));
assert.ok(!empty.includes("legacy pronunciation"));

const two = VD.renderHtml([], 2, true);
assert.ok(two.includes("2 legacy pronunciation lines still apply"));
assert.ok(two.includes("data-vd-import"));
assert.ok(
  two.indexOf("legacy pronunciation") < two.indexOf("voice-dict-grid"),
  "the notice sits above the column headers and the list",
);

const one = VD.renderHtml([], 1, true);
assert.ok(one.includes("1 legacy pronunciation line still applies"));

const readonly = VD.renderHtml([], 2, false);
assert.ok(!readonly.includes("data-vd-new"), "read-only users get no New entry control");
assert.ok(!readonly.includes("data-vd-import"), "read-only users get no Import control");
assert.ok(readonly.includes("legacy pronunciation"), "the notice itself still informs them");
"""
    )


# -- settings writes --------------------------------------------------------


def test_put_setting_sends_the_settings_envelope() -> None:
    _run(
        """
const calls = [];
globalThis.authFetch = (url, init) => {
  calls.push({url, init});
  return Promise.resolve({ok: true, json: async () => ({})});
};
const value = VD.canonicalJson([{
  term: "SQL", kind: "respelling", respelling: "sequel", ipa: "", match_case: false, also_hears: [],
}]);
await VD.putSetting("audio.voice_dictionary", value);
assert.equal(calls.length, 1);
assert.equal(calls[0].url, "/v1/api/admin/settings/audio.voice_dictionary");
assert.equal(calls[0].init.method, "PUT");
assert.deepEqual(calls[0].init.headers, {"Content-Type": "application/json"});
assert.deepEqual(JSON.parse(calls[0].init.body), {value});
"""
    )


def test_put_setting_surfaces_the_servers_error_text() -> None:
    _run(
        """
globalThis.authFetch = () => Promise.resolve({
  ok: false,
  json: async () => ({error: "audio.voice_dictionary: entry 1: duplicate term 'SQL'"}),
});
await assert.rejects(
  () => VD.putSetting("audio.voice_dictionary", "[]"),
  /entry 1: duplicate term 'SQL'/,
);
"""
    )


def test_import_writes_the_dictionary_then_clears_the_legacy_key() -> None:
    _run(
        """
const calls = [];
globalThis.authFetch = (url, init) => {
  calls.push({url, body: JSON.parse(init.body)});
  return Promise.resolve({ok: true, json: async () => ({})});
};
const existing = [];
const result = await VD.importEntries(
  existing,
  "# old\\nSQL = sequel\\nnginx = engine ex",
);
assert.deepEqual(calls.map((call) => call.url), [
  "/v1/api/admin/settings/audio.voice_dictionary",
  "/v1/api/admin/settings/audio.tts_pronunciations",
]);
assert.deepEqual(JSON.parse(calls[0].body.value).map((entry) => entry.term), ["SQL", "nginx"]);
assert.equal(calls[1].body.value, "");
assert.equal(result.imported, 2);
assert.equal(result.skipped, 0);
"""
    )


def test_import_stops_after_a_refused_dictionary_write() -> None:
    _run(
        """
const calls = [];
globalThis.authFetch = (url, init) => {
  calls.push(url);
  return Promise.resolve({
    ok: false,
    json: async () => ({error: "audio.voice_dictionary: entry 2: duplicate term 'SQL'"}),
  });
};
await assert.rejects(
  () => VD.importEntries([], "SQL = sequel\\nSQL = ess cue ell"),
  /duplicate term/,
);
assert.deepEqual(calls, ["/v1/api/admin/settings/audio.voice_dictionary"]);
"""
    )


def test_import_refuses_to_start_when_it_would_overflow_the_cap() -> None:
    _run(
        """
const calls = [];
globalThis.authFetch = (url) => {
  calls.push(url);
  return Promise.resolve({ok: true, json: async () => ({})});
};
const existing = Array.from({length: 200}, (_, i) => ({
  term: "t" + i, kind: "respelling", respelling: "r", ipa: "", match_case: false, also_hears: [],
}));
await assert.rejects(() => VD.importEntries(existing, "a = one"), /200/);
assert.deepEqual(calls, [], "nothing is written when the import cannot fit");
"""
    )
