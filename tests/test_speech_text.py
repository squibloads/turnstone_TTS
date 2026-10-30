"""Tests for turnstone.core.speech_text — the backend-agnostic TTS normaliser."""

from __future__ import annotations

import json
import time

import pytest

from turnstone.core.speech_text import normalize_for_speech, parse_pronunciations
from turnstone.core.voice_config import DictionaryEntry, parse_dictionary

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
    ("open speech_text.py now", "open speech_text dot py now"),
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


def _elapsed(text: str) -> float:
    start = time.perf_counter()
    normalize_for_speech(text)
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
