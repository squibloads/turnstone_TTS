"""Tests for turnstone.core.speech_text — the backend-agnostic TTS normaliser."""

from __future__ import annotations

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
    ("≈ 5", "approximately 5"),
    ("±3", "plus or minus 3"),
    ("± 3", "plus or minus 3"),
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
    # versions
    ("1.8.5", "1 point 8 point 5"),
    ("v1.9.0", "version 1 point 9 point 0"),
    ("version 1.2.3.4", "version 1 point 2 point 3 point 4"),
    # abbreviations
    ("e.g. this", "for example this"),
    ("i.e. that", "that is that"),
    ("cats, dogs, etc.", "cats, dogs, et cetera"),
    ("cats, etc. Then go", "cats, et cetera. Then go"),
    ("a vs. b", "a versus b"),
    ("a vs b", "a versus b"),
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
        "It happened on 2026-10-16 at noon.",
        "Pi is 3.14 and the load was 13.9 yesterday.",
        "Version two is out.",
        "The x axis and y axis; box and tax.",
        "Memory at 0x1F is fine.",
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


def test_dates_untouched() -> None:
    assert normalize_for_speech("on 2026-10-16") == "on 2026-10-16"


def test_two_part_decimal_untouched() -> None:
    assert normalize_for_speech("score 13.9") == "score 13.9"


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
