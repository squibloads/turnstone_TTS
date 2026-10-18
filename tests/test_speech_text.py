"""Tests for turnstone.core.speech_text — the backend-agnostic TTS normaliser."""

from __future__ import annotations

import json
import re
import time

import pytest

from turnstone.core.speech_text import (
    _RULES,
    _sub_with_segments,
    normalize_for_speech,
    parse_pronunciations,
    preview_for_speech,
)
from turnstone.core.voice_config import (
    CLEANUP_GROUPS,
    CleanupOptions,
    DictionaryEntry,
    RewriteRule,
    SpeechPreview,
    expand_rewrite,
    parse_dictionary,
)

RULE_CASES: list[tuple[str, str]] = [
    # symbols
    ("~5 minutes", "about 5 minutes"),
    ("~100 users", "about 100 users"),
    ("a -> b", "a to b"),
    ("a → b", "a to b"),
    ("2×3", "2 by 3"),
    ("2 × 3", "2 by 3"),
    ("1024x1024", "1024 by 1024"),
    ("1024x1024x3", "1024 by 1024 by 3"),
    ("a -> -> b", "a to  to b"),
    ("≈ 5", "approximately 5"),
    ("±3", "plus-or-minus 3"),
    ("± 3", "plus-or-minus 3"),
    ("50%", "50 percent"),
    ("12.5 %", "12.5 percent"),
    # units
    ("120ms", "120 milliseconds"),
    ("120 ms", "120 milliseconds"),
    ("3s", "3 seconds"),
    ("3 s later", "3 seconds later"),
    ("40 s", "40 seconds"),
    # decades are not seconds; an attached "s" after a round number is left alone
    ("In the 1990s.", "In the 1990s."),
    ("the 80s and 2020s", "the 80s and 2020s"),
    # a two-digit round number is a decade only with a cue ("the", "'", early/mid/late,
    # or a list of decades); otherwise, in a technical reply, it is a duration
    ("music from the 80s", "music from the 80s"),
    ("the '80s", "the '80s"),
    ("the early 90s", "the early 90s"),
    ("a mid-80s synth", "a mid-80s synth"),
    ("the 80s and 90s", "the 80s and 90s"),
    ("the 70s, 80s or 90s", "the 70s, 80s or 90s"),
    ("timeout is 60s", "timeout is 60 seconds"),
    ("every 10s", "every 10 seconds"),
    ("after 10s and 20s", "after 10 seconds and 20 seconds"),
    ("retry after 120s", "retry after 120 seconds"),
    ("a TTL of 3600s", "a TTL of 3600 seconds"),
    ("took 13.9s", "took 13.9 seconds"),
    ("the 1800s", "the 1800s"),
    ("run it on k8s", "run it on k8s"),
    ("5min", "5 minutes"),
    ("10 min", "10 minutes"),
    ("13 GB/s", "13 gigabytes per second"),
    ("500MB/s", "500 megabytes per second"),
    ("32GB", "32 gigabytes"),
    ("32 MB", "32 megabytes"),
    ("4GiB", "4 gibibytes"),
    ("4 MiB", "4 mebibytes"),
    ("8KB", "8 kilobytes"),
    ("8 kB", "8 kilobytes"),
    ("2TB", "2 terabytes"),
    ("300W", "300 watts"),
    ("4.5GHz", "4.5 gigahertz"),
    ("800 MHz", "800 megahertz"),
    # ranges
    ("3-5 items", "3 to 5 items"),
    ("pages 10-20", "pages 10 to 20"),
    ("10-20ms", "10 to 20 milliseconds"),
    ("A100-80GB", "A100-80 gigabytes"),
    ("V100-SXM2-32GB", "V100-SXM2-32 gigabytes"),
    # versions
    ("1.8.5", "1 point 8 point 5"),
    ("v1.9.0", "version 1 point 9 point 0"),
    ("version 1.2.3.4", "version 1 point 2 point 3 point 4"),
    ("3.12.1", "3 point 12 point 1"),
    ("1.100.20", "1 point 100 point 20"),
    # dotted thousands (German format) read with literal dots
    ("written 1.000.000 in German format", "written 1 dot 000 dot 000 in German format"),
    ("12.500.000", "12 dot 500 dot 000"),
    ("123.456.789", "123 dot 456 dot 789"),
    ("It was 1.000.000.", "It was 1 dot 000 dot 000."),
    ("About 1.000.000 rows.", "About 1 dot 000 dot 000 rows."),
    ("1.100.200", "1 dot 100 dot 200"),
    ("1.000.000.000", "1 point 000 point 000 point 000"),
    # abbreviations
    ("e.g. this", "for example this"),
    ("i.e. that", "that is that"),
    ("cats, dogs, etc.", "cats, dogs, et cetera."),
    ("cats, etc. Then go", "cats, et cetera. Then go"),
    ("cats, etc., and dogs", "cats, et cetera, and dogs"),
    ("cats, etc. and dogs", "cats, et cetera and dogs"),
    ("a vs. b", "a versus b"),
    ("a vs b", "a versus b"),
    ("We picked A vs.", "We picked A versus."),
    ("a --> b", "a to b"),
    ("a-->b", "a to b"),
    # ISO dates: the digits otherwise run together
    ("on 2026-10-16", "on October 16th, 2026"),
    ("2026-01-01 to 2026-03-22", "January 1st, 2026 to March 22nd, 2026"),
    ("2026-02-03", "February 3rd, 2026"),
    ("2026-11-11", "November 11th, 2026"),
    ("2026-10-31", "October 31st, 2026"),
    ("2024-02-29", "February 29th, 2024"),
    ("2026-02-31", "2026-02-31"),
    # year-months; a pair of years is still a range
    ("Released 2026-10.", "Released October 2026."),
    ("2020-2025", "2020 to 2025"),
    ("2019-20", "2019 to 20"),
    # ten-digit phone numbers read digit by digit, in their groups
    ("call 555-123-4567", "call 5 5 5, 1 2 3, 4 5 6 7"),
    ("call (555) 123-4567", "call 5 5 5, 1 2 3, 4 5 6 7"),
    ("call 555.123.4567", "call 5 5 5, 1 2 3, 4 5 6 7"),
    # a "+1" is left as the digits it is; only the ten-digit body is grouped
    ("call +1 555 123 4567", "call +1 5 5 5, 1 2 3, 4 5 6 7"),
    # numbers with a leading 1 (and a space or hyphen) are left alone
    ("call 1 800 555 1234", "call 1 800 555 1234"),
    ("call 1-800-555-1234", "call 1-800-555-1234"),
    # a sentence-final full stop is punctuation, not part of the token
    ("Released 2026-10-16.", "Released October 16th, 2026."),
    ("Call 555-123-4567.", "Call 5 5 5, 1 2 3, 4 5 6 7."),
    ("It takes 10-20.", "It takes 10 to 20."),
    # file names: bounded segments, case-insensitive extension, leading dashes
    ("run --config.toml", "run --config dot toml"),
    ("open speech-text.py", "open speech-text dot py"),
    ("edit config.YAML now", "edit config dot yammel now"),
    ("edit Config.JSON now", "edit Config dot jay-son now"),
    # W is watts after a number, but "123 W Main Street" is a compass point
    ("300 W and more", "300 W and more"),
    ("1600 W 13th Ave", "1600 W 13th Ave"),
    ("301 W. Main St.", "301 W. Main St."),
    # a four-part-or-longer version still reads as points
    ("on 192.168.100.203", "on 192 point 168 point 100 point 203"),
    ("on 192.168.1.203", "on 192 point 168 point 1 point 203"),
    # file names with a common extension
    ("open speech_text.py now", "open speech text dot py now"),
    ("see README.md and app.config.ts", "see README dot md and app.config dot ts"),
    ("a Node.js app", "a Node dot js app"),
    ("see README.md.sh", "see README dot md dot sh"),
    ("v1.2.md", "v1.2 dot md"),
    # ranges: a fraction after the hyphen is not a range
    ("Mix 1-1/2 cups", "Mix 1-1/2 cups"),
    ("10-15 minutes", "10 to 15 minutes"),
    # dimensions
    ("Use 2x4s", "Use 2 by 4's"),
    ("Ryzen 7950x3d", "Ryzen 7950x3d"),
    ("0x1F", "0x1F"),
    # decade / seconds edges
    ("the 2090s", "the 2090s"),
    ("the 2100s", "the 2100 seconds"),
    ("the 1000s", "the 1000s"),
    ("bathe 80s", "bathe 80 seconds"),
    # common technical terms that voices mangle
    ("the PostgreSQL logs", "the Postgres logs"),
    ("run systemctl and kubectl", "run system control and kube control"),
    ("edit the YAML and JSON", "edit the yammel and jay-son"),
    ("open config.json", "open config dot jay-son"),
]


@pytest.mark.parametrize(("src", "expected"), RULE_CASES)
def test_rules(src: str, expected: str) -> None:
    assert normalize_for_speech(src) == expected


@pytest.mark.parametrize(("src", "_expected"), RULE_CASES)
def test_idempotent(src: str, _expected: str) -> None:
    once = normalize_for_speech(src)
    assert normalize_for_speech(once) == once


@pytest.mark.parametrize(
    "text",
    [
        "Hello there, how are you today?",
        "The items in these rooms are fine, as it was.",
        "Run it for 5 hours, then stop.",
        "Use items, this, rooms, ms, s and min as words.",
        "It happened on 2026-13-45 at noon.",
        "Call 555-12-34 today.",
        "The file named notes is in the docs folder.",
        "Pi is 3.14 and the load was 13.9 yesterday.",
        "Version two is out.",
        "The x axis and y axis; box and tax.",
        "Memory at 0x1F is fine.",
        "Qwen3-8B and qwen3-32b run.",
        "The .30-06 rifle.",
        "30-06",
        "Dated 16.10.2026 here.",
        "About 1,000,000 rows.",
        "Call 1-800-555-1234 now",
        "Moved to 1600 W 13th Ave",
        "2026-02-31",
        "2026-10-16.5",
        "555-123-4567.5",
        "pages 10-20.5 ok",
        "logged at 2026-10-16T12:00:00Z",
        "Read 2026.10.16 in the log.",
        "openssl 3.0.2-1 installed",
        "123 W Main Street",
        "the 80s-90s",
        "the 80s–90s",
    ],
)
def test_plain_prose_unchanged(text: str) -> None:
    assert normalize_for_speech(text) == text


def test_s_unit_only_after_number() -> None:
    assert normalize_for_speech("5s of delay") == "5 seconds of delay"
    assert normalize_for_speech("5sec") == "5sec"
    assert normalize_for_speech("rooms") == "rooms"
    assert normalize_for_speech("10 items") == "10 items"
    assert normalize_for_speech("100 ms") == "100 milliseconds"


@pytest.mark.parametrize("key", ["yaml", "YAML", "Yaml"])
def test_operator_pronunciation_overrides_built_in_term(key: str) -> None:
    assert normalize_for_speech("the YAML file", {key: "yah-mul"}) == "the yah-mul file"


def test_two_part_decimal_untouched() -> None:
    assert normalize_for_speech("score 13.9") == "score 13.9"


def test_arrow_whitespace_variants() -> None:
    assert normalize_for_speech("a->b") == "a to b"
    assert normalize_for_speech("a  ->  b") == "a to b"
    assert normalize_for_speech("a ->") == "a to "
    assert normalize_for_speech("a -> → b") == "a to  to b"
    assert normalize_for_speech("->b") == " to b"


def test_dimension_chain_single_pass() -> None:
    once = normalize_for_speech("1024x1024x3")
    assert once == "1024 by 1024 by 3"
    assert normalize_for_speech(once) == once


def test_empty_string() -> None:
    assert normalize_for_speech("") == ""


def test_cued_eight_item_decade_list_stays_decades() -> None:
    text = "the 10s, 20s, 30s, 40s, 50s, 60s, 70s, 80s"
    assert normalize_for_speech(text) == text


def test_decade_list_past_the_walk_bound_reads_the_tail_as_seconds() -> None:
    # The cue is more than eight decades back, so the ninth is given up on
    # rather than searched for: documented, bounded behaviour.
    text = "the 10s, 20s, 30s, 40s, 50s, 60s, 70s, 80s, 90s"
    assert normalize_for_speech(text) == text.removesuffix("90s") + "90 seconds"


class TestPronunciations:
    def test_case_insensitive_whole_word(self) -> None:
        out = normalize_for_speech("Turnstone and turnstones", {"turnstone": "turn stone"})
        assert out == "turn stone and turnstones"

    def test_applied_last(self) -> None:
        out = normalize_for_speech("v1.2.3 is ready", {"point": "dot"})
        assert out == "version 1 dot 2 dot 3 is ready"

    def test_idempotent(self) -> None:
        pron = {"Kokoro": "ko ko ro"}
        once = normalize_for_speech("Kokoro rocks", pron)
        assert normalize_for_speech(once, pron) == once

    def test_regex_chars_in_key_are_literal(self) -> None:
        assert normalize_for_speech("a.b ok axb", {"a.b": "ay bee"}) == "ay bee ok axb"

    def test_parse(self) -> None:
        raw = "\n# comment\nGPU = gee pee you\n\nnoequals\n = empty\nkey =\nSQL=sequel\n"
        assert parse_pronunciations(raw) == {"GPU": "gee pee you", "SQL": "sequel"}

    def test_parse_keeps_first_200_entries(self) -> None:
        raw = "\n".join(f"w{i} = r{i}" for i in range(250))
        parsed = parse_pronunciations(raw)
        assert len(parsed) == 200
        assert "w199" in parsed
        assert "w200" not in parsed

    def test_parse_skips_overlong_respelling(self) -> None:
        parsed = parse_pronunciations(f"long = {'x' * 101}\nok = {'y' * 100}")
        assert parsed == {"ok": "y" * 100}

    def test_parse_skips_overlong_key(self) -> None:
        parsed = parse_pronunciations(f"{'k' * 101} = x\n{'k' * 100} = y")
        assert parsed == {"k" * 100: "y"}

    def test_later_duplicate_line_wins(self) -> None:
        assert parse_pronunciations("a = one\na = two") == {"a": "two"}

    def test_parse_empty(self) -> None:
        assert parse_pronunciations("") == {}

    def test_parse_value_with_equals(self) -> None:
        assert parse_pronunciations("a = b = c") == {"a": "b = c"}


def _entry(
    term: str,
    respelling: str,
    *,
    match_case: bool = False,
    kind: str = "respelling",
    ipa: str = "",
    also_hears: tuple[str, ...] = (),
) -> DictionaryEntry:
    return DictionaryEntry(term, kind, respelling, ipa, match_case, also_hears)


class TestVoiceDictionary:
    def test_entry_is_applied(self) -> None:
        assert normalize_for_speech("Use SQL", None, [_entry("SQL", "sequel")]) == "Use sequel"

    def test_dictionary_beats_legacy_pronunciations(self) -> None:
        entries = [_entry("SQL", "sequel")]
        assert normalize_for_speech("Use SQL", {"SQL": "ess cue ell"}, entries) == "Use sequel"

    def test_dictionary_beats_built_in_terms(self) -> None:
        assert normalize_for_speech("the YAML file", None, []) == "the yammel file"
        entries = [_entry("YAML", "why-ammel")]
        assert normalize_for_speech("the YAML file", None, entries) == "the why-ammel file"

    def test_match_case_false_is_case_insensitive(self) -> None:
        entries = [_entry("Turnstone", "turn stone")]
        assert (
            normalize_for_speech("Turnstone and turnstones", None, entries)
            == "turn stone and turnstones"
        )
        assert normalize_for_speech("turnstone", None, entries) == "turn stone"

    def test_match_case_true_is_exact_case(self) -> None:
        entries = [_entry("BAA", "B A A", match_case=True)]
        assert normalize_for_speech("the sheep said baa", None, entries) == "the sheep said baa"
        assert normalize_for_speech("BAA is the code", None, entries) == "B A A is the code"
        assert normalize_for_speech("Baa is not BAA", None, entries) == "Baa is not B A A"

    def test_case_sensitive_entry_wins_over_case_insensitive(self) -> None:
        # Input order deliberately puts the broader entry first.
        entries = [_entry("baa", "bah"), _entry("BAA", "B A A", match_case=True)]
        assert normalize_for_speech("BAA", None, entries) == "B A A"
        assert normalize_for_speech("baa", None, entries) == "bah"
        assert normalize_for_speech("Baa", None, entries) == "bah"

    def test_longest_key_first(self) -> None:
        entries = [_entry("C", "see"), _entry("C++", "see plus plus")]
        assert normalize_for_speech("C++ and C", None, entries) == "see plus plus and see"

    def test_longer_key_beats_shorter_case_sensitive_key(self) -> None:
        # Length ordering applies before the case-sensitive tie-break: the
        # longer, case-insensitive entry must own the whole token.
        entries = [_entry("C", "see", match_case=True), _entry("c++", "see plus plus")]
        assert normalize_for_speech("C++", None, entries) == "see plus plus"

    def test_replacement_is_not_rescanned(self) -> None:
        entries = [_entry("foo", "bar"), _entry("bar", "baz")]
        assert normalize_for_speech("foo bar", None, entries) == "bar baz"

    def test_empty_respelling_is_dictation_only(self) -> None:
        entries = [_entry("nginx", "", also_hears=("engine x",))]
        assert normalize_for_speech("run nginx", None, entries) == "run nginx"
        # The entry is skipped, so a legacy pronunciation for the same word stands.
        assert normalize_for_speech("run nginx", {"nginx": "engine ex"}, entries) == "run engine ex"

    def test_ipa_entry_reads_aloud_from_respelling(self) -> None:
        entries = [
            _entry(
                "nginx", "engine ex", kind="ipa", ipa="/\u02c8\u025bnd\u0292\u026an \u02c8\u025bks/"
            )
        ]
        assert normalize_for_speech("run nginx", None, entries) == "run engine ex"

    def test_ipa_entry_without_respelling_is_dictation_only(self) -> None:
        entries = [
            _entry("nginx", "", kind="ipa", ipa="/\u02c8\u025bnd\u0292\u026an \u02c8\u025bks/")
        ]
        assert normalize_for_speech("run nginx", None, entries) == "run nginx"

    def test_key_is_literal_and_whole_word(self) -> None:
        entries = [_entry("a.b", "ay bee")]
        assert normalize_for_speech("a.b ok axb", None, entries) == "ay bee ok axb"

    def test_applied_after_the_rules(self) -> None:
        assert (
            normalize_for_speech("v1.2.3 is ready", None, [_entry("point", "dot")])
            == "version 1 dot 2 dot 3 is ready"
        )

    def test_parsed_dictionary_entries_work(self) -> None:
        entries = parse_dictionary(
            json.dumps([{"term": "SQL", "respelling": "sequel", "match_case": False}])
        )
        assert normalize_for_speech("Use SQL", None, entries) == "Use sequel"

    def test_none_and_empty_dictionary_are_noops(self) -> None:
        assert normalize_for_speech("the YAML file", None, None) == "the yammel file"
        assert normalize_for_speech("the YAML file", None, []) == "the yammel file"


class TestUnicodeCaseFolds:
    def test_long_s_matches_key(self) -> None:
        assert normalize_for_speech("ſql rocks", {"sql": "sequel"}) == "sequel rocks"

    def test_dotless_i_does_not_raise(self) -> None:
        normalize_for_speech("an ı here", {"i": "eye"})

    def test_dotted_capital_i_does_not_raise(self) -> None:
        normalize_for_speech("İs it", {"is": "X"})


def _elapsed(text: str, options: CleanupOptions | None = None) -> float:
    start = time.perf_counter()
    normalize_for_speech(text, options=options)
    return time.perf_counter() - start


def test_timing_long_whitespace_run() -> None:
    assert _elapsed("a" + " " * 49_998 + "b") < 1.0


def test_timing_repeated_dotted_thousands() -> None:
    assert _elapsed("1.000." * 8_334) < 0.1


def test_timing_long_dotted_chain() -> None:
    assert _elapsed("1." * 25_000 + "1x1") < 1.0


def test_timing_long_hyphen_runs() -> None:
    assert _elapsed("a-" * 25_000 + ".py") < 0.2
    assert _elapsed("-a" * 25_000) < 0.2


def test_timing_file_name_rule() -> None:
    # Not parametrized: pytest puts the id in an environment variable.
    for text in ("a-a." * 12_500, "a_b.c-d." * 6_250, "a.x-" * 12_500):
        assert _elapsed(text) < 0.2


def test_timing_arrow_and_decade_runs() -> None:
    assert _elapsed("a " + "-" * 50_000 + "> b") < 0.2
    assert _elapsed("10s, " * 10_000) < 1.0  # measured ~0.17 s: decade walk


# -- cleanup groups -----------------------------------------------------------


def test_every_rule_row_has_a_known_group() -> None:
    known = {group.id for group in CLEANUP_GROUPS}
    tagged = {group for group, _pattern, _repl in _RULES}
    assert tagged <= known
    # identifiers runs as its own stage, so it need not own a _RULES row.
    assert known - {"identifiers"} <= tagged


@pytest.mark.parametrize(
    ("group", "text"),
    [
        ("phone_numbers", "call 555-123-4567"),
        ("dates", "on 2026-10-16"),
        ("file_names", "open notes.txt"),
        ("versions", "upgrade to v1.8.5"),
        ("symbols", "~5 users"),
        ("dimensions", "a 1024x768 screen"),
        ("units", "120ms and 32GB"),
        ("seconds_decades", "timeout is 60s"),
        ("ranges", "takes 10-20 minutes"),
        ("abbreviations", "e.g. this"),
        ("identifiers", "user_id"),
    ],
)
def test_disabling_a_group_skips_its_rules(group: str, text: str) -> None:
    options = CleanupOptions(disabled_groups=frozenset({group}))
    assert normalize_for_speech(text, options=options) == text


def test_disabling_one_group_leaves_the_others_running() -> None:
    options = CleanupOptions(disabled_groups=frozenset({"units"}))
    assert normalize_for_speech("~5 files at 32GB", options=options) == "about 5 files at 32GB"


def test_builtin_terms_off_leaves_those_terms_alone() -> None:
    options = CleanupOptions(builtin_terms_off=frozenset({"YAML", "JSON"}))
    assert normalize_for_speech("edit YAML and JSON", options=options) == "edit YAML and JSON"
    assert normalize_for_speech("run systemctl", options=options) == "run system control"


def test_builtin_term_off_does_not_block_an_operator_respelling() -> None:
    options = CleanupOptions(builtin_terms_off=frozenset({"YAML"}))
    assert normalize_for_speech("edit YAML", {"YAML": "yah-mul"}, options=options) == "edit yah-mul"
    entries = [_entry("YAML", "why-ammel")]
    assert normalize_for_speech("edit YAML", None, entries, options) == "edit why-ammel"


# -- identifiers --------------------------------------------------------------

IDENTIFIER_ACRONYMS = (
    "ai",
    "api",
    "id",
    "db",
    "ui",
    "url",
    "uri",
    "io",
    "os",
    "cpu",
    "gpu",
    "llm",
    "tts",
    "stt",
    "json",
    "http",
    "sql",
    "ssh",
    "vm",
    "ip",
    "phi",
    "pii",
    "aws",
    "ssl",
    "tls",
    "cli",
    "sdk",
    "jwt",
    "mcp",
    "ado",
    "pr",
    "ci",
)


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("user_id", "user ID"),
        ("aid_kit", "aid kit"),
        ("myplace_health_ai_use_context", "myplace health AI use context"),
        ("__init__", "init"),
        ("_private_var", "private var"),
        ("a__b", "a b"),
        ("trace_2026_10_18", "trace 2026 10 18"),
        ("api_url and db_host", "API URL and DB host"),
        ("gpu_tts_mcp", "GPU TTS MCP"),
        ("speech_text.py", "speech text dot py"),
        ("voice_dictionary.js", "voice dictionary dot js"),
        ("café_a", "café a"),
        # no ASCII letter or digit beside an underscore: left alone, like qwen-tts
        ("café_", "café_"),
        ("k8s", "k8s"),
        ("x _ y", "x _ y"),
    ],
)
def test_identifiers_read_snake_case_as_words(src: str, expected: str) -> None:
    assert normalize_for_speech(src) == expected


@pytest.mark.parametrize("acronym", IDENTIFIER_ACRONYMS)
def test_identifiers_spell_every_known_acronym_in_capitals(acronym: str) -> None:
    # "json" is also a built-in term, so switch that layer off to see the
    # group's own capitalisation.
    options = CleanupOptions(builtin_terms_off=frozenset({"JSON"}))
    assert normalize_for_speech(f"x_{acronym}_y", options=options) == f"x {acronym.upper()} y"


def test_identifiers_run_before_the_builtin_term_layer() -> None:
    assert normalize_for_speech("x_json_y") == "x jay-son y"


def test_timing_long_word_run_with_identifiers() -> None:
    # The qwen-tts pattern backtracks quadratically on a long word run; the
    # boundary form used here must stay linear.
    assert _elapsed("a" * 50_000) < 0.2


# -- custom rewrite rules -----------------------------------------------------


def _rule(
    find: str, replace: str = "", *, regex: bool = False, enabled: bool = True
) -> RewriteRule:
    return RewriteRule(find=find, regex=regex, replace=replace, test="", enabled=enabled)


def test_custom_rules_run_after_the_cleanup_groups() -> None:
    options = CleanupOptions(rules=(_rule("gigabytes", "GBs"),))
    assert normalize_for_speech("needs 32GB", options=options) == "needs 32 GBs"


def test_custom_rules_run_before_the_pronunciation_layer() -> None:
    options = CleanupOptions(rules=(_rule("why ammel", "YAML"),))
    assert normalize_for_speech("say why ammel", options=options) == "say yammel"


def test_custom_rules_run_in_order_and_see_the_previous_output() -> None:
    options = CleanupOptions(rules=(_rule("foo", "bar"), _rule("bar", "baz")))
    assert normalize_for_speech("foo", options=options) == "baz"


def test_only_enabled_custom_rules_run() -> None:
    options = CleanupOptions(rules=(_rule("foo", "bar", enabled=False), _rule("bar", "baz")))
    assert normalize_for_speech("foo bar", options=options) == "foo baz"


def test_plain_rule_is_whole_word_and_ignores_case() -> None:
    options = CleanupOptions(rules=(_rule("K8s", "kubernetes"),))
    assert normalize_for_speech("K8S and k8sx", options=options) == "kubernetes and k8sx"


def test_regex_rule_expands_groups() -> None:
    options = CleanupOptions(rules=(_rule(r"(\d+)px", r"\1 pixels", regex=True),))
    assert normalize_for_speech("at 10px and 2px", options=options) == "at 10 pixels and 2 pixels"


def test_a_rule_that_no_longer_compiles_is_skipped() -> None:
    bad = RewriteRule(find="(bad", regex=True, replace="x", test="", enabled=True)
    options = CleanupOptions(rules=(bad, _rule("foo", "bar")))
    assert normalize_for_speech("foo", options=options) == "bar"


def test_zero_length_matches_are_skipped() -> None:
    # The public write path rejects a pattern that matches empty text; a
    # directly-built CleanupOptions can still carry one, so the engine guards.
    rule = RewriteRule(find="x*", regex=True, replace="Y", test="", enabled=True)
    pattern = re.compile("x*")
    text, _segments = _sub_with_segments(pattern, lambda match: expand_rewrite(rule, match), "axxb")
    assert text == "aYb"


def test_timing_one_hundred_custom_rules_on_a_long_input() -> None:
    rules = tuple(_rule(f"word{i}", f"term{i}") for i in range(100))
    text = " ".join(f"word{i}" for i in range(100)) * 20
    assert _elapsed(text, CleanupOptions(rules=rules)) < 1.0


# -- preview ------------------------------------------------------------------


def _assert_spans(preview: SpeechPreview) -> None:
    last = 0
    for span in preview.spans:
        assert 0 <= span.start < span.end <= len(preview.text)
        assert span.start >= last
        assert span.source in {"group", "custom", "dictionary"}
        assert span.label
        last = span.end


PREVIEW_CORPUS = [src for src, _expected in RULE_CASES] + [
    "user_id and aid_kit at 32GB",
    "v1.8.5 needs 1.000.000 rows, e.g. on 2026-10-16",
    "__init__ calls myplace_health_ai_use_context",
    "5-10ms, 300W, ~50%, ±3",
    "Use SQL and the YAML file",
    "No changes here at all.",
    "",
]


@pytest.mark.parametrize("src", PREVIEW_CORPUS)
def test_preview_text_matches_normalize_for_speech(src: str) -> None:
    assert preview_for_speech(src).text == normalize_for_speech(src)


@pytest.mark.parametrize("src", PREVIEW_CORPUS)
def test_preview_spans_are_sorted_disjoint_and_inside_the_text(src: str) -> None:
    _assert_spans(preview_for_speech(src))


def test_preview_text_matches_normalize_with_options() -> None:
    options = CleanupOptions(
        disabled_groups=frozenset({"units"}),
        rules=(_rule("Postgres", "postgres"), _rule("SQL", "sequel", enabled=False)),
        builtin_terms_off=frozenset({"YAML"}),
    )
    entries = [_entry("nginx", "engine ex")]
    text = "Postgres nginx 32GB YAML SQL"
    preview = preview_for_speech(text, None, entries, options)
    assert preview.text == normalize_for_speech(text, None, entries, options)
    _assert_spans(preview)


def test_preview_labels_a_group_change() -> None:
    preview = preview_for_speech("Needs 32GB")
    assert preview.text == "Needs 32 gigabytes"
    assert [(span.source, span.label) for span in preview.spans] == [("group", "units")]
    span = preview.spans[0]
    assert preview.text[span.start : span.end] == " gigabytes"


def test_preview_labels_identifiers() -> None:
    preview = preview_for_speech("user_id")
    assert [(span.source, span.label) for span in preview.spans] == [("group", "identifiers")]
    assert preview.text == "user ID"


def test_preview_labels_a_custom_rule_by_its_stored_position() -> None:
    options = CleanupOptions(rules=(_rule("first", "1st"), _rule("Postgres", "postgres")))
    preview = preview_for_speech("Postgres", options=options)
    assert [(span.source, span.label) for span in preview.spans] == [("custom", "custom 2")]


def test_preview_numbers_rules_across_disabled_ones() -> None:
    options = CleanupOptions(
        rules=(_rule("first", "1st", enabled=False), _rule("Postgres", "postgres"))
    )
    preview = preview_for_speech("Postgres", options=options)
    assert [(span.source, span.label) for span in preview.spans] == [("custom", "custom 2")]


def test_preview_labels_the_pronunciation_layer_as_dictionary() -> None:
    built_in = preview_for_speech("the YAML file")
    assert built_in.text == "the yammel file"
    assert [(span.source, span.label) for span in built_in.spans] == [("dictionary", "dictionary")]
    entry = preview_for_speech("Use SQL", None, [_entry("SQL", "sequel")])
    assert [(span.source, span.label) for span in entry.spans] == [("dictionary", "dictionary")]


def test_preview_keeps_separate_spans_for_separate_changes() -> None:
    preview = preview_for_speech("32GB at 120ms")
    assert [(span.source, span.label) for span in preview.spans] == [
        ("group", "units"),
        ("group", "units"),
    ]
    covered = "".join(preview.text[span.start : span.end] for span in preview.spans)
    assert covered == " gigabytes milliseconds"


def test_preview_covers_every_changed_stretch_of_text() -> None:
    preview = preview_for_speech("user_id needs 32GB")
    covered = "".join(preview.text[span.start : span.end] for span in preview.spans)
    assert covered == "user ID gigabytes"


def test_preview_reports_no_spans_for_unchanged_text() -> None:
    preview = preview_for_speech("Hello there.")
    assert preview.text == "Hello there."
    assert preview.spans == ()


def test_preview_does_not_label_a_no_op_rule() -> None:
    options = CleanupOptions(rules=(_rule("SQL", "SQL"),))
    assert preview_for_speech("Use SQL", options=options).spans == ()


def test_preview_carries_group_spans_through_a_later_rule() -> None:
    options = CleanupOptions(rules=(_rule("point", "dot"),))
    preview = preview_for_speech("v1.2.3", options=options)
    assert preview.text == "version 1 dot 2 dot 3"
    _assert_spans(preview)
    labels = {span.label for span in preview.spans}
    assert "versions" in labels
    assert "custom 1" in labels
