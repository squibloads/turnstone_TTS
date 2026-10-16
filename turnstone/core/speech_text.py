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
- dates: ``2026-10-16`` and ``16.10.2026`` are left alone, not read out
- currency beyond what models already do
- phone numbers: ``555-1234`` still reads as a range (known limitation)
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


# Ordered (pattern, replacement); a replacement is a string or a callback.
# Order matters: ``GB/s`` before ``GB`` and ``s``.  The two version rows stay at
# the head so no later rule (ranges, units, "x") can eat their digits.
_RULES: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = tuple(
    (re.compile(pattern), repl)
    for pattern, repl in (
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
        (r"±\s*", "plus or minus "),
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
        # "13.9 s" and "3s" are seconds; "1990s" and "80s" are decades, so an attached
        # "s" after a multi-digit number ending in 0 is left alone ("30s" too: rare).
        (r"(?<=\d)\s+s\b", " seconds"),
        (r"(?<=\d)(?<!\d0)s\b", " seconds"),
        # numeric ranges, but not dates (2026-10-16), version/decimal tails,
        # identifiers (Qwen3-8B) or calibers (30-06: a right side starting 0)
        (r"(?<![\w.-])(\d+)-(?!0)(\d+)(?![\w.-])", "\\1 to \\2"),
        # abbreviations
        (r"\be\.g\.", "for example"),
        (r"\bi\.e\.", "that is"),
        (r"\betc\.(?!\s+[A-Z])", "et cetera"),
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

    Operator pronunciations run last so they can override anything the built-in
    rules produced.
    """
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    if pronunciations:
        text = _apply_pronunciations(text, pronunciations)
    return text
