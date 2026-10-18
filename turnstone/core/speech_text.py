"""Backend-agnostic text normalisation for text-to-speech.

TTS models (Kokoro, OpenAI, vLLM-Omni, ...) read technical replies badly:
``~5`` becomes "tilde five", ``32GB`` is spelled out, ``1.8.5`` is read as a
decimal.  Rewriting those into plain English words before synthesis helps every
backend at once, which is why this is a small ordered rule table rather than
anything model-specific.

Design rules: cover the common 95% and leave text alone when unsure; every rule
in the table is idempotent (its output is not rewritten by the table again); the
respellings are applied once and their output is not rescanned; plain prose
without symbols passes through unchanged.

Deliberately skipped (do not add without a measured need):

- non-English text
- IPA / SSML or any other model-specific pronunciation syntax sent to the
  backend: read-aloud always sends an entry's plain ``respelling``, including
  for dictionary entries of kind ``ipa`` (teaching a model to accept IPA is
  out of scope), and operators can still use ``parse_pronunciations`` for
  legacy ``word = respelling`` lines
- math expressions
- dates other than ISO: ``16.10.2026`` is left alone (day/month order is
  ambiguous), and a two-digit-year dotted date such as ``16.10.26`` reads as a
  version; ISO ``2026-10-16`` is read as "October 16th, 2026" (an impossible day
  such as ``2026-02-31`` stays raw) and ``2026-10`` as "October 2026"
- currency beyond what models already do
- phone numbers other than ten-digit ones: ``555-1234`` still reads as a range;
  numbers with a leading 1 (``1-800-555-1234``, ``1 800 555 1234``) are left alone
- abbreviations beyond e.g., i.e., etc. and vs.
- ambiguous tokens such as ``8k``
- LLM-based rewriting
- pluralisation ("1 seconds" is accepted)
"""

from __future__ import annotations

import datetime
import re
from typing import TYPE_CHECKING

from turnstone.core.voice_config import BUILTIN_TERMS, CleanupOptions

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from turnstone.core.voice_config import DictionaryEntry, SpeechPreview


def _speak_version(match: re.Match[str]) -> str:
    text = match.group(0)
    parts = text.split(".")
    # 1.000.000 is a number with thousands separators (German format), not a
    # version: a 1-3 digit lead and exactly two groups of three digits.  Read the
    # dots literally, since "point" would make it sound like a decimal.  Exactly
    # two dots: an address such as 192.168.100.203 is still read as points.
    if (
        len(parts) == 3
        and 1 <= len(parts[0]) <= 3
        and all(len(p) == 3 and p.isdigit() for p in parts[1:])
    ):
        return text.replace(".", " dot ")
    # 2026.10.16 is a dotted date, left raw like 16.10.2026.
    if re.fullmatch(r"\d{4}\.\d{1,2}\.\d{1,2}", text):
        return text
    return text.replace(".", " point ")


# Characters looked back for a decade cue or an earlier decade: enough for
# "the 80s, " and it keeps every step a constant-size scan.
_DECADE_WINDOW = 24
# Decades walked back along a list; a cue further than this is not searched for,
# so the ninth item of a cued list is read as seconds.
_DECADE_MAX_STEPS = 8

_DECADE_CUE = re.compile(r"(?:\b(?:the|early|mid|late)[\s-]+|')$", re.IGNORECASE)
_DECADE_BEFORE = re.compile(
    r"(?<![\w.])(\d0|\d{3}0)s(?:\s*[,\-–]\s*|\s+(?:and|or|to|through)\s+)$", re.IGNORECASE
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
    for _ in range(_DECADE_MAX_STEPS):
        if len(number) == 4:
            return re.fullmatch(r"1\d\d0|20\d0", number) is not None
        if len(number) != 2 or not number.endswith("0"):
            return False
        before = text[max(0, start - _DECADE_WINDOW) : start]
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
    try:
        datetime.date(int(year), month, day)
    except ValueError:
        return match.group(0)  # 2026-02-31 is not a date: leave it alone
    return f"{_MONTHS[month - 1]} {_ordinal(day)}, {year}"


def _speak_year_month(match: re.Match[str]) -> str:
    return f"{_MONTHS[int(match.group(2)) - 1]} {match.group(1)}"


def _speak_dimensions(match: re.Match[str]) -> str:
    # "2x4s" is lumber: the plural s becomes "'s" so it reads "2 by 4's".
    return match.group(1).replace("x", " by ") + ("'s" if match.group(2) else "")


def _speak_phone(match: re.Match[str]) -> str:
    # Digit by digit in the number's own groups, the way people read them out.
    return ", ".join(" ".join(group) for group in match.groups())


# Parts of a snake_case name that are said as initials rather than words.  The
# set is the qwen-tts backend's ``speakable()`` list, so a name that backend
# already read well sounds the same here.
_IDENTIFIER_ACRONYMS = frozenset(
    {
        "ai", "api", "id", "db", "ui", "url", "uri", "io", "os", "cpu", "gpu",
        "llm", "tts", "stt", "json", "http", "sql", "ssh", "vm", "ip", "phi",
        "pii", "aws", "ssl", "tls", "cli", "sdk", "jwt", "mcp", "ado", "pr", "ci",
    }
)  # fmt: skip

# The qwen-tts backend matches identifiers with
# ``\w*[A-Za-z0-9]_\w*|\w*_[A-Za-z0-9]\w*``.  That form backtracks
# quadratically on a long word run, so this is the same set of matches written
# with boundaries: one whole word run, taken only when an ASCII alphanumeric
# sits next to an underscore.
_IDENTIFIER = re.compile(r"(?<!\w)(?=\w*(?:[A-Za-z0-9]_|_[A-Za-z0-9]))\w+")


def _speak_identifier(match: re.Match[str]) -> str:
    # Leading and trailing underscores are separators, not words: "__init__"
    # is "init".  Interior empty parts ("a__b") collapse the same way.
    parts = [part for part in match.group(0).split("_") if part]
    if not parts:
        return match.group(0)
    return " ".join(
        part.upper() if part.lower() in _IDENTIFIER_ACRONYMS else part for part in parts
    )


# Technical terms most voices mangle, as plain respellings that work on every
# backend.  Universal terms only; an operator's audio.tts_pronunciations entry
# or a dictionary entry for the same word wins.  The list lives in voice_config
# so the Dictionary tab can show it and audio.tts_builtin_terms_off can name it.
_TECH_TERMS = dict(BUILTIN_TERMS)


# Ordered (group, pattern, replacement); a replacement is a string or a
# callback.  Order matters: ``GB/s`` before ``GB`` and ``s``.  Phone numbers,
# ISO dates, file names and the version rows come first so no later rule
# (ranges, units, "x") can eat their digits.  The group names the switch in
# ``audio.tts_cleanup_disabled``; a disabled group skips all of its rows.
_RULES: tuple[tuple[str, re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = tuple(
    (group, re.compile(pattern), repl)
    for group, pattern, repl in (
        # ten-digit phone numbers and ISO dates first: later rules would read
        # their parts as ranges or versions
        (
            "phone_numbers",
            r"(?<![\w.(-])(?<!(?<!\+)\b1[ -])\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-](\d{4})(?![\w-]|\.\w)",
            _speak_phone,
        ),
        (
            "dates",
            r"(?<![\w.-])(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(?![\w-]|\.\w)",
            _speak_iso_date,
        ),
        # year-month ("2026-10"); a pair of years ("2020-2025") stays a range
        (
            "dates",
            r"(?<![\w.-])((?:19|20)\d\d)-(0[1-9]|1[0-2])(?![\w/-]|\.\w)",
            _speak_year_month,
        ),
        # file names: only the dot before a listed extension, so there is no
        # repetition for the engine to backtrack over ("speech_text.py")
        (
            "file_names",
            r"(?<=[\w-])\.(?=(?i:py|js|mjs|ts|tsx|jsx|md|json|ya?ml|toml|txt|sh|html|css|sql"
            r"|csv|log|ini|cfg|rs|go|java|rb|php)\b)",
            " dot ",
        ),
        # versions: 3+ dotted parts only (two-part decimals like "13.9" read fine).
        # The possessive group keeps long dotted chains linear and the date guard
        # leaves 16.10.2026 alone.
        ("versions", r"\bv(?=\d+(?:\.\d+){2,})", "version "),
        (
            "versions",
            r"(?<![\w.])\d+(?:\.\d+){2,}+(?<!\.\d{4})(?![\w.]*\d|-\d)",
            _speak_version,
        ),
        # symbols
        ("symbols", r"~\s*(?=\d)", "about "),
        # lookbehind + possessive tail stop quadratic backtracking on long blanks
        ("symbols", r"(?<!\s)\s*(?:--?>|→)(?:\s*+(?!--?>|→))?", " to "),
        # dimensions: the multiplication sign and the NxM form switch together
        ("dimensions", r"(?<=\d)\s*×\s*(?=\d)", " by "),
        ("dimensions", r"(?<![\w.])(?!0x)(\d+(?:x\d+)+)(s?)(?!\w)", _speak_dimensions),
        ("symbols", r"≈\s*", "approximately "),
        ("symbols", r"±\s*", "plus-or-minus "),  # hyphenated: Kokoro paused after "plus"
        ("symbols", r"(?<=\d)\s*%", " percent"),
        # units (case-sensitive: "MB" is not "mb"); rates before their parts
        ("units", r"(?<=\d)\s*GB/s\b", " gigabytes per second"),
        ("units", r"(?<=\d)\s*MB/s\b", " megabytes per second"),
        ("units", r"(?<=\d)\s*GiB\b", " gibibytes"),
        ("units", r"(?<=\d)\s*MiB\b", " mebibytes"),
        ("units", r"(?<=\d)\s*GB\b", " gigabytes"),
        ("units", r"(?<=\d)\s*MB\b", " megabytes"),
        ("units", r"(?<=\d)\s*[kK]B\b", " kilobytes"),
        ("units", r"(?<=\d)\s*TB\b", " terabytes"),
        ("units", r"(?<=\d)\s*GHz\b", " gigahertz"),
        ("units", r"(?<=\d)\s*MHz\b", " megahertz"),
        ("units", r"(?<=\d)\s*ms\b", " milliseconds"),
        ("units", r"(?<=\d)\s*min\b", " minutes"),
        # attached only: "300W" is watts, "123 W Main Street" is a compass point
        ("units", r"(?<=\d)W\b", " watts"),
        # "13.9 s", "3s" and "60s" are seconds; "1990s" and "the 80s" are decades
        # (see _is_decade).  Not inside a word: "k8s" stays.
        ("units", r"(?<=\d)\s+s\b", " seconds"),
        ("seconds_decades", r"(?<![\w.])(\d+(?:\.\d+)?)s\b", _seconds_unless_decade),
        # numeric ranges, but not dates (2026-10-16), version/decimal tails,
        # identifiers (Qwen3-8B) or calibers (30-06: a right side starting 0)
        ("ranges", r"(?<![\w.-])(\d+)-(?!0)(\d+)(?![\w/-]|\.\w)", "\\1 to \\2"),
        # abbreviations
        ("abbreviations", r"\be\.g\.", "for example"),
        ("abbreviations", r"\bi\.e\.", "that is"),
        # "etc." keeps its full stop unless the sentence clearly carries on
        ("abbreviations", r"\betc\.(?=\s*[,;:)]|\s+[a-z])", "et cetera"),
        ("abbreviations", r"\betc\.", "et cetera."),
        # like "etc.", a sentence-final "vs." keeps its full stop
        ("abbreviations", r"\bvs\.(?=\s*[,;:)]|\s+[a-z])", "versus"),
        ("abbreviations", r"\bvs\.", "versus."),
        ("abbreviations", r"\bvs\b", "versus"),
        # identifiers last: the rules above must see dotted file names and
        # versions as written, before their underscores become spaces
        ("identifiers", _IDENTIFIER, _speak_identifier),
    )
)


# Bound per-request cost: the setting is admin text, but it is compiled into a
# regex on every read-aloud call.
_MAX_PRONUNCIATIONS = 200
_MAX_RESPELLING_CHARS = 100


def parse_pronunciations(raw: str) -> dict[str, str]:
    """Parse ``word = respelling`` lines into a mapping.

    Operators edit this as free text in a settings box, so bad input must
    degrade to "that line is ignored" rather than an exception that would
    break read-aloud.  Blank lines, ``#`` comments and lines missing a key or
    a respelling are skipped, as are keys and respellings over 100 characters and every
    word after the first 200.  A later line for the same word wins.
    """
    out: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key or not value:
            continue
        if max(len(key), len(value)) > _MAX_RESPELLING_CHARS:
            continue
        if key not in out and len(out) >= _MAX_PRONUNCIATIONS:
            continue
        out[key] = value
    return out


# A (key, replacement, match_case) row.  Case-insensitive keys are already
# folded, so each row becomes one branch of a single alternation.
_Replacement = tuple[str, str, bool]


def _replacement_rows(
    pronunciations: Mapping[str, str] | None,
    dictionary: Sequence[DictionaryEntry] | None,
    builtin_terms_off: frozenset[str] = frozenset(),
) -> list[_Replacement]:
    """Collect every respelling into one precedence-ordered row list.

    Later sources win: built-in technical terms, then the legacy
    ``audio.tts_pronunciations`` mapping, then dictionary entries.  A
    dictionary entry without a respelling is dictation-only and is skipped.
    Terms named in *builtin_terms_off* are left out of the built-in layer; an
    operator respelling or dictionary entry for the same word still applies.
    """
    # Keys match case-insensitively, so merge them folded: an operator's "yaml"
    # replaces the built-in "YAML".
    off = {name.lower() for name in builtin_terms_off}
    folded: dict[str, str] = {
        key.lower(): value for key, value in _TECH_TERMS.items() if key.lower() not in off
    }
    if pronunciations:
        folded.update({key.lower(): value for key, value in pronunciations.items() if key})
    exact: dict[str, str] = {}
    for entry in dictionary or ():
        if not entry.term or not entry.respelling:
            continue  # no term to match, or dictation-only with nothing to say
        if entry.match_case:
            exact[entry.term] = entry.respelling
        else:
            folded[entry.term.lower()] = entry.respelling
    rows: list[_Replacement] = [(key, value, False) for key, value in folded.items()]
    rows.extend((key, value, True) for key, value in exact.items())
    # Longest key first so a key cannot shadow a longer one.  On equal length
    # the case-sensitive row goes first, so it wins for text spelled exactly
    # its way while the folded row still covers every other casing.
    rows.sort(key=lambda row: (-len(row[0]), not row[2]))
    return rows


def _apply_replacements(text: str, rows: Sequence[_Replacement]) -> str:
    """Replace whole-word keys in a single pass.

    One alternation (longest key first) keeps a respelling from being
    re-substituted by another key, so the result does not depend on input
    order.  Case-insensitive keys are scoped with ``(?i:...)``, so
    case-sensitive keys can share the same regex, and a replacement is never
    rescanned.
    """
    if not rows:
        return text
    alternation = "|".join(
        f"((?i:{re.escape(key)}))" if not match_case else f"({re.escape(key)})"
        for key, _value, match_case in rows
    )
    values = [value for _key, value, _match_case in rows]
    # One capturing group per key: the group index names the value directly, so
    # a Unicode case fold ("ſ" matching "s") cannot miss a re-derived dict key.
    pattern = re.compile(r"(?<!\w)(?:" + alternation + r")(?!\w)")
    return pattern.sub(lambda m: values[(m.lastindex or 1) - 1], text)


def normalize_for_speech(
    text: str,
    pronunciations: Mapping[str, str] | None = None,
    dictionary: Sequence[DictionaryEntry] | None = None,
    options: CleanupOptions | None = None,
) -> str:
    """Rewrite symbols, units and versions in *text* into speakable English.

    *options* selects which cleanup groups run and which stored rewrite rules
    apply; ``None`` means every group on, no custom rules and every built-in
    term.  Respellings run last so they can override anything the rules
    produced.  Precedence, strongest first: *dictionary* entries, the legacy
    *pronunciations* mapping, then the built-in technical terms.  Dictionary
    keys match whole-word and case-insensitively unless ``match_case`` is set,
    in which case only the exact case matches.  Entries without a
    ``respelling`` are dictation-only and are skipped; for ``kind == "ipa"``
    the entry's ``respelling`` is read aloud (sending IPA to a model that
    accepts it is out of scope here).
    """
    options = options or CleanupOptions()
    for group, pattern, repl in _RULES:
        if group not in options.disabled_groups:
            text = pattern.sub(repl, text)
    return _apply_replacements(
        text, _replacement_rows(pronunciations, dictionary, options.builtin_terms_off)
    )


def preview_for_speech(
    text: str,
    pronunciations: Mapping[str, str] | None = None,
    dictionary: Sequence[DictionaryEntry] | None = None,
    options: CleanupOptions | None = None,
) -> SpeechPreview:
    """``normalize_for_speech`` plus which rule changed which part of the output.

    Contract for the Text cleanup tab: ``.text`` is exactly what
    ``normalize_for_speech(text, pronunciations, dictionary, options)`` returns.
    Implemented by the cleanup-engine slice.
    """
    raise NotImplementedError
