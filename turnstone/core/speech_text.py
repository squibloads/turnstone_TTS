"""Backend-agnostic text normalisation for text-to-speech.

TTS models (Kokoro, OpenAI, vLLM-Omni, ...) read technical replies badly:
``~5`` becomes "tilde five", ``32GB`` is spelled out, ``1.8.5`` is read as a
decimal.  Rewriting those into plain English words before synthesis helps every
backend at once, which is why this is a small ordered rule table rather than
anything model-specific.

Design rules: cover the common 95% and leave text alone when unsure; every rule
is idempotent so normalising twice equals normalising once; plain prose without
symbols passes through unchanged.

Deliberately skipped (do not add without a measured need):

- non-English text
- IPA / SSML or any other model-specific pronunciation syntax (operators use
  plain respellings via ``parse_pronunciations`` instead)
- math expressions
- dates other than ISO: ``16.10.2026`` is left alone (day/month order is
  ambiguous); ISO ``2026-10-16`` is read as "October 16th, 2026"
- currency beyond what models already do
- phone numbers other than ten-digit ones: ``555-1234`` still reads as a range
- ambiguous tokens such as ``8k``
- LLM-based rewriting
- pluralisation ("1 seconds" is accepted)
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


def _speak_version(match: re.Match[str]) -> str:
    text = match.group(0)
    # 1.000.000 is a number with thousands separators, not a version.
    if all(len(p) == 3 and p.isdigit() for p in text.split(".")[1:]):
        return text
    return text.replace(".", " point ")


_DECADE_CUE = re.compile(r"(?:\b(?:the|early|mid|late)[\s-]+|')$", re.IGNORECASE)
_DECADE_BEFORE = re.compile(
    r"(?<![\w.])(\d0|\d{3}0)s(?:\s*,\s*|\s+(?:and|or|to|through)\s+)$", re.IGNORECASE
)


def _is_decade(text: str, start: int, number: str) -> bool:
    """Whether ``<number>s`` at *start* is a decade rather than a duration.

    TTS models read "60s" as "sixties" whatever the context, so this decides.
    Round four-digit years (1000s-2090s) always are decades; 3600s is not.  A
    round two-digit number is one only with a cue just before it: "the", an
    apostrophe, early/mid/late, or an earlier decade in a list ("the 80s and
    90s").  A bare "60s" in a technical reply is a timeout far more often than
    an era.
    """
    for _ in range(8):  # decade lists are short; this bounds the walk back
        if len(number) == 4:
            return re.fullmatch(r"1\d\d0|20\d0", number) is not None
        if len(number) != 2 or not number.endswith("0"):
            return False
        before = text[max(0, start - 24) : start]
        if _DECADE_CUE.search(before):
            return True
        previous = _DECADE_BEFORE.search(before)
        if previous is None:
            return False
        start, number = start - len(before) + previous.start(1), previous.group(1)
    return False


def _seconds_unless_decade(match: re.Match[str]) -> str:
    number = match.group(1)
    if "." not in number and _is_decade(match.string, match.start(), number):
        return match.group(0)
    return f"{number} seconds"


_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}" + {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _speak_iso_date(match: re.Match[str]) -> str:
    # Read as digits, voices rush "2026-10-16" into one blur of numbers.
    year, month, day = match.group(1), int(match.group(2)), int(match.group(3))
    return f"{_MONTHS[month - 1]} {_ordinal(day)}, {year}"


def _speak_phone(match: re.Match[str]) -> str:
    # Digit by digit in the number's own groups, the way people read them out.
    return ", ".join(" ".join(group) for group in match.groups())


# Technical terms most voices mangle, as plain respellings that work on every
# backend.  Universal terms only; an operator's audio.tts_pronunciations entry
# for the same word wins.
_TECH_TERMS = {
    "PostgreSQL": "Postgres",
    "systemctl": "system control",
    "kubectl": "kube control",
    "YAML": "yammel",
    "JSON": "jay-son",
}


# Ordered (pattern, replacement); a replacement is a string or a callback.
# Order matters: ``GB/s`` before ``GB`` and ``s``.  The two version rows stay at
# the head so no later rule (ranges, units, "x") can eat their digits.
_RULES: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = tuple(
    (re.compile(pattern), repl)
    for pattern, repl in (
        # ten-digit phone numbers and ISO dates first: later rules would read
        # their parts as ranges or versions
        (
            r"(?<![\w.(-])(?:\+?1[\s.-])?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-](\d{4})(?![\w.-])",
            _speak_phone,
        ),
        (
            r"(?<![\w.-])(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(?![\w.-])",
            _speak_iso_date,
        ),
        # file names with a common extension: "speech_text.py" is "speech_text dot py"
        (
            r"(?<![\w.])([A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*)"
            r"\.(py|js|mjs|ts|tsx|jsx|md|json|ya?ml|toml|txt|sh|html|css|sql|csv|log|ini|cfg"
            r"|rs|go|java|rb|php)\b",
            "\\1 dot \\2",
        ),
        # versions: 3+ dotted parts only (two-part decimals like "13.9" read fine).
        # The possessive group keeps long dotted chains linear and the date guard
        # leaves 16.10.2026 alone.
        (r"\bv(?=\d+(?:\.\d+){2,})", "version "),
        (r"(?<![\w.])\d+(?:\.\d+){2,}+(?<!\.\d{4})(?![\w.]*\d)", _speak_version),
        # symbols
        (r"~\s*(?=\d)", "about "),
        # lookbehind + possessive tail stop quadratic backtracking on long blanks
        (r"(?<!\s)\s*(?:->|→)(?:\s*+(?!->|→))?", " to "),
        (r"(?<=\d)\s*×\s*(?=\d)", " by "),
        (r"(?<![\w.])(?!0x)\d+(?:x\d+)+", lambda m: m.group(0).replace("x", " by ")),
        (r"≈\s*", "approximately "),
        (r"±\s*", "plus-or-minus "),  # hyphenated: Kokoro paused after "plus"
        (r"(?<=\d)\s*%", " percent"),
        # units (case-sensitive: "MB" is not "mb"); rates before their parts
        (r"(?<=\d)\s*GB/s\b", " gigabytes per second"),
        (r"(?<=\d)\s*MB/s\b", " megabytes per second"),
        (r"(?<=\d)\s*GiB\b", " gibibytes"),
        (r"(?<=\d)\s*MiB\b", " mebibytes"),
        (r"(?<=\d)\s*GB\b", " gigabytes"),
        (r"(?<=\d)\s*MB\b", " megabytes"),
        (r"(?<=\d)\s*[kK]B\b", " kilobytes"),
        (r"(?<=\d)\s*TB\b", " terabytes"),
        (r"(?<=\d)\s*GHz\b", " gigahertz"),
        (r"(?<=\d)\s*MHz\b", " megahertz"),
        (r"(?<=\d)\s*ms\b", " milliseconds"),
        (r"(?<=\d)\s*min\b", " minutes"),
        (r"(?<=\d)\s*W\b", " watts"),
        # "13.9 s", "3s" and "60s" are seconds; "1990s" and "the 80s" are decades
        # (see _is_decade).  Not inside a word: "k8s" stays.
        (r"(?<=\d)\s+s\b", " seconds"),
        (r"(?<![\w.])(\d+(?:\.\d+)?)s\b", _seconds_unless_decade),
        # numeric ranges, but not dates (2026-10-16), version/decimal tails,
        # identifiers (Qwen3-8B) or calibers (30-06: a right side starting 0)
        (r"(?<![\w.-])(\d+)-(?!0)(\d+)(?![\w.-])", "\\1 to \\2"),
        # abbreviations
        (r"\be\.g\.", "for example"),
        (r"\bi\.e\.", "that is"),
        # "etc." keeps its full stop unless the sentence clearly carries on
        (r"\betc\.(?=\s*[,;:)]|\s+[a-z])", "et cetera"),
        (r"\betc\.", "et cetera."),
        (r"\bvs\b\.?", "versus"),
    )
)


def parse_pronunciations(raw: str) -> dict[str, str]:
    """Parse ``word = respelling`` lines into a mapping.

    Operators edit this as free text in a settings box, so bad input must
    degrade to "that line is ignored" rather than an exception that would
    break read-aloud.  Blank lines, ``#`` comments and lines missing a key or
    a respelling are skipped.
    """
    out: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and value:
            out[key] = value
    return out


def _apply_pronunciations(text: str, pronunciations: Mapping[str, str]) -> str:
    """Replace whole-word, case-insensitive keys in a single pass.

    One alternation (longest key first) keeps a respelling from being
    re-substituted by another key, so the result does not depend on dict order.
    """
    folded_keys: list[str] = []
    values: list[str] = []
    seen: set[str] = set()
    for key, value in sorted(pronunciations.items(), key=lambda kv: len(kv[0]), reverse=True):
        folded = key.lower()
        if key and folded not in seen:
            seen.add(folded)
            folded_keys.append(folded)
            values.append(value)
    if not values:
        return text
    # One capturing group per key: the group index names the value directly, so
    # a Unicode case fold ("ſ" matching "s") cannot miss a re-derived dict key.
    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(f"({re.escape(k)})" for k in folded_keys) + r")(?!\w)",
        re.IGNORECASE,
    )
    return pattern.sub(lambda m: values[(m.lastindex or 1) - 1], text)


def normalize_for_speech(text: str, pronunciations: Mapping[str, str] | None = None) -> str:
    """Rewrite symbols, units and versions in *text* into speakable English.

    Respellings (the built-in technical terms, then the operator's own, which
    win) run last so they can override anything the rules produced.
    """
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    # Keys match case-insensitively, so merge them folded: an operator's "yaml"
    # replaces the built-in "YAML".
    terms = {key.lower(): value for key, value in _TECH_TERMS.items()}
    if pronunciations:
        terms.update({key.lower(): value for key, value in pronunciations.items() if key})
    return _apply_pronunciations(text, terms)
