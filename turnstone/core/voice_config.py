"""The voice dictionary and speaking styles, as stored in ``audio.*`` settings.

Both are lists an operator edits in Admin › Voice, stored as JSON strings in
the settings table rather than in tables of their own: they are small (capped
at 200 and 20 entries), always read whole, and the settings store already
gives them scoping, reset-to-default and the admin API.

Each has two parsers on purpose.  The **strict** ``validate_*`` runs on the
write path (the settings PUT) and rejects bad input with a message naming the
entry, so the editor can show it.  The **tolerant** ``parse_*`` runs on every
read-aloud and dictation call and degrades bad input to "that entry is
ignored", because a stored value that predates a rule change must never break
speech.

One dictionary serves both directions.  Read-aloud replaces ``term`` with its
``respelling`` (or ``ipa`` for a model that accepts IPA); dictation replaces
any ``also_hears`` phrase in a transcript with ``term`` and can hint the terms
to the transcription model.  An entry with only ``also_hears`` is
dictation-only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_DICTIONARY_ENTRIES = 200
MAX_TERM_CHARS = 100
MAX_ALSO_HEARS = 10
MAX_STYLES = 20
MAX_STYLE_NAME_CHARS = 40
MAX_INSTRUCTION_CHARS = 500

DICTIONARY_KINDS = ("respelling", "ipa")


@dataclass(frozen=True)
class DictionaryEntry:
    term: str
    kind: str  # "respelling" | "ipa"
    respelling: str  # read-aloud text for every model; may be "" (dictation-only)
    ipa: str  # only for kind == "ipa"; used by models that accept IPA
    match_case: bool  # False: whole-word, case-insensitive (the legacy behaviour)
    also_hears: tuple[str, ...]  # transcript phrases dictation turns into ``term``


@dataclass(frozen=True)
class SpeakingStyle:
    name: str
    instruction: str  # "" = no instruction (plain delivery)
    in_menu: bool  # offered in the read-aloud style picker


DEFAULT_STYLES = json.dumps(
    [
        {
            "name": "Professional",
            "instruction": (
                "Speak in a calm, even, professional tone, like a knowledgeable "
                "colleague briefing a teammate. Keep emphasis subtle and natural, "
                "with no dramatic emotion."
            ),
            "in_menu": True,
        },
        {
            "name": "Social",
            "instruction": (
                "Speak in a relaxed, warm, conversational tone, like chatting with "
                "a friend. Be natural and expressive."
            ),
            "in_menu": True,
        },
        {"name": "Plain", "instruction": "", "in_menu": True},
    ],
    ensure_ascii=False,
)


# -- shared field checks ------------------------------------------------------


def _text(item: dict[str, Any], key: str, limit: int, where: str, *, required: bool) -> str:
    value = item.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{where}: {key} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{where}: {key} is required")
    if len(value) > limit:
        raise ValueError(f"{where}: {key} is longer than {limit} characters")
    return value


def _flag(item: dict[str, Any], key: str, default: bool, where: str) -> bool:
    value = item.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{where}: {key} must be true or false")
    return value


def _load_list(raw: str) -> list[Any]:
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON: {exc.msg}") from None
    except (ValueError, RecursionError):
        # The decoder's other failures: an integer past the conversion limit
        # (plain ValueError) or nesting past the recursion limit.
        raise ValueError("not valid JSON: nested too deeply or a number too large") from None
    if not isinstance(data, list):
        raise ValueError("must be a JSON list")
    return data


# -- dictionary ---------------------------------------------------------------


def _check_entry(item: Any, where: str) -> DictionaryEntry:
    if not isinstance(item, dict):
        raise ValueError(f"{where}: must be an object")
    term = _text(item, "term", MAX_TERM_CHARS, where, required=True)
    kind = item.get("kind", "respelling") or "respelling"
    if kind not in DICTIONARY_KINDS:
        raise ValueError(f"{where}: kind must be one of {', '.join(DICTIONARY_KINDS)}")
    respelling = _text(item, "respelling", MAX_TERM_CHARS, where, required=False)
    ipa = _text(item, "ipa", MAX_TERM_CHARS, where, required=kind == "ipa")
    if kind != "ipa":
        ipa = ""
    match_case = _flag(item, "match_case", False, where)
    hears_raw = item.get("also_hears", [])
    if hears_raw is None:
        hears_raw = []
    if not isinstance(hears_raw, list):
        raise ValueError(f"{where}: also_hears must be a list of phrases")
    if len(hears_raw) > MAX_ALSO_HEARS:
        raise ValueError(f"{where}: also_hears has more than {MAX_ALSO_HEARS} phrases")
    hears: list[str] = []
    for phrase in hears_raw:
        if not isinstance(phrase, str) or not phrase.strip():
            raise ValueError(f"{where}: also_hears phrases must be non-empty strings")
        if len(phrase.strip()) > MAX_TERM_CHARS:
            raise ValueError(f"{where}: an also_hears phrase is longer than {MAX_TERM_CHARS}")
        hears.append(phrase.strip())
    if not respelling and not ipa and not hears:
        raise ValueError(f"{where}: nothing to say or hear (add a respelling or also_hears)")
    return DictionaryEntry(term, kind, respelling, ipa, match_case, tuple(hears))


def _entry_key(entry: DictionaryEntry) -> tuple[str, bool]:
    return (entry.term if entry.match_case else entry.term.casefold(), entry.match_case)


def _entry_json(entry: DictionaryEntry) -> dict[str, Any]:
    return {
        "term": entry.term,
        "kind": entry.kind,
        "respelling": entry.respelling,
        "ipa": entry.ipa,
        "match_case": entry.match_case,
        "also_hears": list(entry.also_hears),
    }


def parse_dictionary(raw: str) -> list[DictionaryEntry]:
    """Tolerant read: bad JSON → [], bad entries skipped, a later duplicate wins.

    Two entries are duplicates when they would match the same text: same term
    (case-folded unless ``match_case``) and same ``match_case``.  Extra
    ``also_hears`` phrases past the cap are dropped rather than the entry.
    """
    try:
        items = _load_list(raw)
    except ValueError:
        return []
    out: dict[tuple[str, bool], DictionaryEntry] = {}
    for i, item in enumerate(items):
        if isinstance(item, dict) and isinstance(item.get("also_hears"), list):
            item = {**item, "also_hears": item["also_hears"][:MAX_ALSO_HEARS]}
        try:
            entry = _check_entry(item, f"entry {i + 1}")
        except ValueError:
            continue
        key = _entry_key(entry)
        if key not in out and len(out) >= MAX_DICTIONARY_ENTRIES:
            continue
        out.pop(key, None)  # re-insert so the later entry also takes the later position
        out[key] = entry
    return list(out.values())


def validate_dictionary(raw: str) -> str:
    """Strict write-path check; returns canonical JSON or raises ValueError."""
    items = _load_list(raw)
    if len(items) > MAX_DICTIONARY_ENTRIES:
        raise ValueError(f"at most {MAX_DICTIONARY_ENTRIES} dictionary entries")
    seen: set[tuple[str, bool]] = set()
    entries: list[DictionaryEntry] = []
    for i, item in enumerate(items):
        entry = _check_entry(item, f"entry {i + 1}")
        key = _entry_key(entry)
        if key in seen:
            raise ValueError(f"entry {i + 1}: duplicate term {entry.term!r}")
        seen.add(key)
        entries.append(entry)
    return json.dumps([_entry_json(e) for e in entries], ensure_ascii=False)


# -- styles -------------------------------------------------------------------


def _check_style(item: Any, where: str) -> SpeakingStyle:
    if not isinstance(item, dict):
        raise ValueError(f"{where}: must be an object")
    name = _text(item, "name", MAX_STYLE_NAME_CHARS, where, required=True)
    instruction = _text(item, "instruction", MAX_INSTRUCTION_CHARS, where, required=False)
    return SpeakingStyle(name, instruction, _flag(item, "in_menu", True, where))


def parse_styles(raw: str) -> list[SpeakingStyle]:
    """Tolerant read; names are unique case-insensitively and a later one wins."""
    try:
        items = _load_list(raw)
    except ValueError:
        return []
    out: dict[str, SpeakingStyle] = {}
    for i, item in enumerate(items):
        try:
            style = _check_style(item, f"style {i + 1}")
        except ValueError:
            continue
        key = style.name.casefold()
        if key not in out and len(out) >= MAX_STYLES:
            continue
        out.pop(key, None)
        out[key] = style
    return list(out.values())


def validate_styles(raw: str) -> str:
    """Strict write-path check; returns canonical JSON or raises ValueError."""
    items = _load_list(raw)
    if len(items) > MAX_STYLES:
        raise ValueError(f"at most {MAX_STYLES} speaking styles")
    seen: set[str] = set()
    styles: list[SpeakingStyle] = []
    for i, item in enumerate(items):
        style = _check_style(item, f"style {i + 1}")
        if style.name.casefold() in seen:
            raise ValueError(f"style {i + 1}: duplicate name {style.name!r}")
        seen.add(style.name.casefold())
        styles.append(style)
    return json.dumps(
        [{"name": s.name, "instruction": s.instruction, "in_menu": s.in_menu} for s in styles],
        ensure_ascii=False,
    )


def resolve_style(styles: list[SpeakingStyle], name: str) -> SpeakingStyle | None:
    """The style called *name* (case-insensitive); "" or an unknown name → None."""
    wanted = (name or "").strip().casefold()
    if not wanted:
        return None
    for style in styles:
        if style.name.casefold() == wanted:
            return style
    return None
