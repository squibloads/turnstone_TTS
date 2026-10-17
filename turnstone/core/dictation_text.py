"""Backend-agnostic dictation helpers: the voice dictionary in speech-to-text.

Two independent levers, both driven by the ``audio.voice_dictionary`` setting:

``dictionary_prompt`` appends the dictionary's terms to the transcription
prompt (``audio.stt_dictionary_hint``), which biases Whisper-style models
towards the operator's spellings — a model that ignores the prompt is simply
unaffected.  ``correct_transcript`` rewrites ``also_hears`` phrases in a
finished transcript to their term (``audio.stt_dictionary_correct``), so every
backend gets the same result whether or not it reads a prompt.

Both are deliberately model-agnostic: nothing here knows which STT backend is
configured.  Bad entries never raise — the caller reads the dictionary with the
tolerant :func:`~turnstone.core.voice_config.parse_dictionary`, and these
helpers filter whatever survives.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from turnstone.core.voice_config import DictionaryEntry

# Whisper-style transcription prompts are short: the model reads about 224
# tokens of prompt context, and the operator's own prompt (audio.stt_prompt)
# has to fit alongside this tail.  ~100 tokens of vocabulary leaves the
# backend room for it; a term that does not fit is dropped whole rather than
# truncated, because half a term is noise that can bias the decoder.
MAX_DICTIONARY_HINT_CHARS = 400

_HINT_LEAD_IN = "Vocabulary: "


def dictionary_prompt(base: str, entries: Iterable[DictionaryEntry]) -> str:
    """Append the dictionary's terms to *base* as a short vocabulary hint.

    The operator's prompt stays first and byte-identical; an empty dictionary
    (or one with no term that fits the cap) returns *base* unchanged.  Terms
    are deduplicated case-insensitively, keeping the first spelling seen.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        term = entry.term.strip()
        key = term.casefold()
        if not term or key in seen:
            continue
        seen.add(key)
        terms.append(term)

    kept: list[str] = []
    for term in terms:
        candidate = f"{_HINT_LEAD_IN}{', '.join([*kept, term])}."
        if len(candidate) > MAX_DICTIONARY_HINT_CHARS:
            continue
        kept.append(term)

    if not kept:
        return base
    hint = f"{_HINT_LEAD_IN}{', '.join(kept)}."
    return f"{base} {hint}" if base else hint


def correct_transcript(text: str, entries: Iterable[DictionaryEntry]) -> str:
    """Replace ``also_hears`` phrases in *text* with their entry's term.

    Matching is case-insensitive and whole-phrase: a phrase matches on word
    boundaries, any run of whitespace inside it matches ``\\s+``, and
    punctuation either side is left in place.  Every phrase is replaced in one
    left-to-right pass with the longest tried first, and the replacement text
    is never rescanned — "bay a" → "BAA" cannot then trigger an entry that
    hears "BAA".  A phrase listed by two entries takes the later entry's term,
    matching ``parse_dictionary``'s later-wins rule.
    """
    terms: dict[tuple[str, ...], str] = {}
    for entry in entries:
        for phrase in entry.also_hears:
            words = tuple(phrase.split())
            if words:
                terms[tuple(word.casefold() for word in words)] = entry.term
    if not text or not terms:
        return text

    ordered = sorted(terms, key=lambda words: len(" ".join(words)), reverse=True)
    terms_by_group: dict[str, str] = {}
    alternatives: list[str] = []
    for i, words in enumerate(ordered):
        group = f"phrase{i}"
        terms_by_group[group] = terms[words]
        alternatives.append(f"(?P<{group}>" + r"\s+".join(re.escape(w) for w in words) + ")")
    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)",
        re.IGNORECASE,
    )
    return pattern.sub(lambda match: terms_by_group[str(match.lastgroup)], text)
