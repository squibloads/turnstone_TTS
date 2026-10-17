"""Tests for turnstone.core.dictation_text — dictionary hints and corrections."""

from __future__ import annotations

import pytest

from turnstone.core.dictation_text import (
    MAX_DICTIONARY_HINT_CHARS,
    correct_transcript,
    dictionary_prompt,
)
from turnstone.core.voice_config import DictionaryEntry


def _entry(term: str = "BAA", *, hears: tuple[str, ...] = ("Bay A",)) -> DictionaryEntry:
    return DictionaryEntry(term, "respelling", "B A A", "", False, hears)


# -- dictionary_prompt -------------------------------------------------------


@pytest.mark.parametrize("base", ["", "GRT, Hodgdon", "  keep  me  "])
def test_dictionary_prompt_is_identity_without_entries(base: str) -> None:
    assert dictionary_prompt(base, []) == base


def test_dictionary_prompt_appends_terms_after_the_operator_prompt() -> None:
    entries = [
        _entry("BAA"),
        _entry("nginx", hears=("engine x",)),
        _entry("Qwen", hears=()),
    ]
    assert dictionary_prompt("GRT, Hodgdon", entries) == (
        "GRT, Hodgdon Vocabulary: BAA, nginx, Qwen."
    )


def test_dictionary_prompt_without_a_base_is_the_hint_alone() -> None:
    assert dictionary_prompt("", [_entry()]) == "Vocabulary: BAA."


def test_dictionary_prompt_dedupes_terms_case_insensitively() -> None:
    entries = [_entry("BAA"), _entry("baa"), _entry("Baa"), _entry("nginx")]
    assert dictionary_prompt("", entries) == "Vocabulary: BAA, nginx."


def test_dictionary_prompt_drops_terms_that_do_not_fit_whole() -> None:
    # 100-character terms: three fit the cap, the rest are dropped whole rather
    # than truncated (a cut-off term would bias the decoder towards a non-word).
    entries = [_entry(f"{i:02d}" + "x" * 98) for i in range(6)]
    got = dictionary_prompt("", entries)
    assert len(got) <= MAX_DICTIONARY_HINT_CHARS
    kept = got.removeprefix("Vocabulary: ").removesuffix(".").split(", ")
    assert 0 < len(kept) < len(entries)
    for entry in entries[: len(kept)]:
        assert entry.term in got
    for entry in entries[len(kept) :]:
        assert entry.term not in got


def test_dictionary_prompt_leaves_the_base_when_no_term_fits() -> None:
    assert dictionary_prompt("GRT", [_entry("x" * (MAX_DICTIONARY_HINT_CHARS + 1))]) == "GRT"


# -- correct_transcript ------------------------------------------------------


def test_correct_transcript_replaces_a_heard_phrase() -> None:
    entries = [_entry("BAA", hears=("bay a",))]
    assert correct_transcript("the bay a was signed", entries) == "the BAA was signed"


def test_correct_transcript_matches_case_insensitively() -> None:
    entries = [_entry("BAA", hears=("bay a",))]
    assert correct_transcript("The BAY A was signed", entries) == "The BAA was signed"


def test_correct_transcript_matches_whitespace_flexibly() -> None:
    entries = [_entry("BAA", hears=("bay a",))]
    assert correct_transcript("the bay \t  a was signed", entries) == "the BAA was signed"


def test_correct_transcript_leaves_adjacent_punctuation_in_place() -> None:
    entries = [_entry("BAA", hears=("bay a",))]
    assert correct_transcript('"the bay a," she wrote.', entries) == '"the BAA," she wrote.'
    assert correct_transcript("signed by bay a.", entries) == "signed by BAA."


def test_correct_transcript_matches_whole_phrases_only() -> None:
    entries = [_entry("BAA", hears=("bay a",))]
    assert correct_transcript("the bay area was signed", entries) == "the bay area was signed"


def test_correct_transcript_prefers_the_longest_phrase() -> None:
    # The shorter entry is first, so only the longest-first ordering lets
    # "kube control" win over "control".
    entries = [
        _entry("control", hears=("control",)),
        _entry("Kubernetes", hears=("kube control",)),
    ]
    assert correct_transcript("kube control plane", entries) == "Kubernetes plane"


def test_correct_transcript_replaces_every_occurrence() -> None:
    entries = [_entry("BAA", hears=("bay a",))]
    assert correct_transcript("bay a, then BAY A again", entries) == "BAA, then BAA again"


def test_correct_transcript_never_rescans_its_output() -> None:
    entries = [_entry("BAA", hears=("bay a",)), _entry("gamma", hears=("BAA",))]
    assert correct_transcript("the bay a was signed", entries) == "the BAA was signed"


def test_correct_transcript_later_entry_wins_a_shared_phrase() -> None:
    entries = [_entry("BAA", hears=("bay a",)), _entry("B A A", hears=("bay a",))]
    assert correct_transcript("the bay a", entries) == "the B A A"


def test_correct_transcript_treats_the_phrase_literally() -> None:
    entries = [
        _entry("C++", hears=("c plus plus",)),
        _entry("A.B", hears=("a.b",)),
        _entry(r"C:\temp", hears=("see slash temp",)),
    ]
    assert correct_transcript("use c plus plus, a.b and see slash temp", entries) == (
        r"use C++, A.B and C:\temp"
    )


def test_correct_transcript_ignores_entries_without_phrases() -> None:
    text = "the bay a was signed"
    assert correct_transcript(text, []) == text
    assert correct_transcript(text, [_entry(hears=())]) == text


def test_correct_transcript_is_identity_for_unmatched_text() -> None:
    entries = [_entry("BAA", hears=("bay a",))]
    assert correct_transcript("nothing to see here", entries) == "nothing to see here"
