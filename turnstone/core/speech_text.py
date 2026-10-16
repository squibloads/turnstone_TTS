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
- dates (``2026-10-16`` is left to the model)
- currency beyond what models already do, and phone numbers
- ambiguous tokens such as ``8k``
- LLM-based rewriting
- pluralisation ("1 seconds" is accepted)
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

# Ordered (pattern, replacement).  Order matters: ``GB/s`` before ``GB`` and
# ``s``.  Dotted versions are handled first, outside the table (they need a
# callback), so no later rule can eat their digits.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), repl)
    for pattern, repl in (
        # symbols
        (r"~\s*(?=\d)", "about "),
        (r"\s*(?:->|→)\s*", " to "),
        (r"(?<=\d)\s*×\s*(?=\d)", " by "),
        (r"(?<![\w.])(?!0x)(\d+)x(?=\d)", "\\1 by "),
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
        (r"(?<=\d)\s*s\b", " seconds"),
        # numeric ranges, but not dates (2026-10-16) or version/decimal tails
        (r"(?<![\d.-])(\d+)-(\d+)(?![\d.-])", "\\1 to \\2"),
        # abbreviations
        (r"\be\.g\.", "for example"),
        (r"\bi\.e\.", "that is"),
        (r"\betc\.(?!\s+[A-Z])", "et cetera"),
        (r"\betc\.", "et cetera."),
        (r"\bvs\b\.?", "versus"),
    )
)

# 3+ dotted parts only; two-part decimals ("13.9") are read fine by models.
_VERSION_PREFIX = re.compile(r"\bv(?=\d+(?:\.\d+){2,})")
_DOT_VERSION = re.compile(r"(?<![\w.])\d+(?:\.\d+){2,}(?![\w.]*\d)")


def _speak_version(match: re.Match[str]) -> str:
    return match.group(0).replace(".", " point ")


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
    lowered = {k.lower(): v for k, v in pronunciations.items() if k}
    if not lowered:
        return text
    keys = sorted(lowered, key=len, reverse=True)
    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(k) for k in keys) + r")(?!\w)",
        re.IGNORECASE,
    )
    return pattern.sub(lambda m: lowered[m.group(0).lower()], text)


def normalize_for_speech(text: str, pronunciations: Mapping[str, str] | None = None) -> str:
    """Rewrite symbols, units and versions in *text* into speakable English.

    Operator pronunciations run last so they can override anything the built-in
    rules produced.
    """
    text = _VERSION_PREFIX.sub("version ", text)
    text = _DOT_VERSION.sub(_speak_version, text)
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    if pronunciations:
        text = _apply_pronunciations(text, pronunciations)
    return text
