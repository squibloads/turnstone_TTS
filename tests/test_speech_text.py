"""Tests for turnstone.core.speech_text — the backend-agnostic TTS normaliser."""

from __future__ import annotations

import time

import pytest

from turnstone.core.speech_text import normalize_for_speech, parse_pronunciations

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
    # abbreviations
    ("e.g. this", "for example this"),
    ("i.e. that", "that is that"),
    ("cats, dogs, etc.", "cats, dogs, et cetera."),
    ("cats, etc. Then go", "cats, et cetera. Then go"),
    ("cats, etc., and dogs", "cats, et cetera, and dogs"),
    ("cats, etc. and dogs", "cats, et cetera and dogs"),
    ("a vs. b", "a versus b"),
    ("a vs b", "a versus b"),
    # ISO dates: the digits otherwise run together
    ("on 2026-10-16", "on October 16th, 2026"),
    ("2026-01-01 to 2026-03-22", "January 1st, 2026 to March 22nd, 2026"),
    ("2026-02-03", "February 3rd, 2026"),
    ("2026-11-11", "November 11th, 2026"),
    # ten-digit phone numbers read digit by digit, in their groups
    ("call 555-123-4567", "call 5 5 5, 1 2 3, 4 5 6 7"),
    ("call (555) 123-4567", "call 5 5 5, 1 2 3, 4 5 6 7"),
    ("call 555.123.4567", "call 5 5 5, 1 2 3, 4 5 6 7"),
    # file names with a common extension
    ("open speech_text.py now", "open speech_text dot py now"),
    ("see README.md and app.config.ts", "see README dot md and app.config dot ts"),
    ("a Node.js app", "a Node dot js app"),
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
        "About 1.000.000 rows.",
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


def test_operator_pronunciation_overrides_built_in_term() -> None:
    assert normalize_for_speech("the YAML file", {"yaml": "yah-mul"}) == "the yah-mul file"


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

    def test_parse_empty(self) -> None:
        assert parse_pronunciations("") == {}

    def test_parse_value_with_equals(self) -> None:
        assert parse_pronunciations("a = b = c") == {"a": "b = c"}


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


def test_timing_long_dotted_chain() -> None:
    assert _elapsed("1." * 25_000 + "1x1") < 1.0
