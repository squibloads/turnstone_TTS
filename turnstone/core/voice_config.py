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

The read-aloud tuning settings live here too: per-model pacing profiles,
custom rewrite rules, the cleanup groups an operator can switch off and the
built-in terms.  They follow the same strict-write / tolerant-read split.
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

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


# -- read-aloud pacing profiles -----------------------------------------------
#
# How a reply is cut into clips and how long the player pauses between them
# used to be constants in tts_player.js, tuned by ear against one model.
# Models differ most exactly there (one barely pauses at a paragraph, another
# adds its own gap), so each TTS model alias keeps its own profile in
# ``audio.tts_profiles``: ``{"<alias>": {"<field>": value, ...}}``.  Stored
# values are overrides; a field a profile never set reads as its default, and
# an alias with no profile reads as DEFAULT_PROFILE — today's player.

# Where every voice setting's "learn more" link points: reference_url on the
# audio.* SettingDefs and ``anchor`` on PROFILE_FIELDS both resolve against it.
VOICE_DOCS_URL = "https://github.com/turnstonelabs/turnstone/blob/main/docs/voice.md"

MAX_PROFILES = 20
MAX_PROFILE_ALIAS_CHARS = 100
MAX_MARKER_CHARS = 60
TABLE_CELL_JOINERS = ("comma", "semicolon", "pause")
TABLE_HEADER_MODES = ("row", "prefix", "skip")


@dataclass(frozen=True)
class TtsProfile:
    pause_heading_ms: int = 600
    pause_paragraph_ms: int = 500
    pause_table_row_ms: int = 150
    pause_code_ms: int = 400
    pause_list_item_ms: int = 0  # 0 = a whole list is one clip
    table_cell_joiner: str = "comma"  # TABLE_CELL_JOINERS
    table_header_mode: str = "row"  # TABLE_HEADER_MODES
    inline_code_max_chars: int = 40
    code_marker: str = "Code omitted."  # "" = skip silently
    diagram_marker: str = "Diagram omitted."  # "" = skip silently
    clip_min_chars: int = 20
    clip_max_chars: int = 600
    max_clips: int = 200


DEFAULT_PROFILE = TtsProfile()


@dataclass(frozen=True)
class ProfileField:
    """One profile field as the Settings tab shows it.

    The help text lives here rather than on a SettingDef because the fields
    are members of one JSON setting; the admin meta endpoint serves this table
    so the page renders the same "?" popover a real setting gets.
    """

    key: str
    group: str  # "pacing" | "structure" | "clips"
    kind: str  # "int" | "str" | "choice"
    default: int | str
    label: str
    help: str
    anchor: str  # section of docs/voice.md
    min_value: int | None = None
    max_value: int | None = None
    choices: tuple[str, ...] = ()
    unit: str = ""


def _pause(key: str, label: str, help_text: str) -> ProfileField:
    default = getattr(DEFAULT_PROFILE, key)
    return ProfileField(
        key, "pacing", "int", default, label, help_text, "pacing", 0, 5000, unit="ms"
    )


PROFILE_FIELDS: tuple[ProfileField, ...] = (
    _pause(
        "pause_heading_ms",
        "Silence after a heading (H1–H6)",
        "Silence added after a heading before the next clip. Raise it if sections run "
        "together; lower it if the model already pauses after short lines. 400–800 suits "
        "most voices.",
    ),
    _pause(
        "pause_paragraph_ms",
        "Silence after a paragraph or blockquote",
        "Silence between paragraphs. Models differ most here: some barely pause on their "
        "own, others add a noticeable gap. Lower it for a model that adds its own.",
    ),
    _pause(
        "pause_table_row_ms",
        "Silence between table rows",
        "Short on purpose, so a table sounds like a list rather than a speech. 0–300.",
    ),
    _pause(
        "pause_code_ms",
        "Silence after a code or diagram marker",
        "Silence after the code or diagram marker, so it does not run into the next sentence.",
    ),
    _pause(
        "pause_list_item_ms",
        "Silence between list items (0 = read a whole list as one clip)",
        "0 reads a whole list as one clip with full stops between items, which sounds "
        "natural on most models. Above 0, each item becomes its own clip followed by this "
        "pause; use it if items blur together.",
    ),
    ProfileField(
        "table_cell_joiner",
        "structure",
        "choice",
        "comma",
        "What goes between the cells of a table row",
        "comma sounds most natural; semicolon gives a longer breath; pause makes each cell "
        "its own clip with the table-row pause between them (slowest).",
        "structure",
        choices=TABLE_CELL_JOINERS,
    ),
    ProfileField(
        "table_header_mode",
        "structure",
        "choice",
        "row",
        "How a table's header row is read",
        "row reads the header like any other row. prefix says each cell's header before it "
        "(“Name: Ryan, Speed: 1.4”), which makes wide tables understandable. skip leaves "
        "the header out when it only repeats the obvious.",
        "structure",
        choices=TABLE_HEADER_MODES,
    ),
    ProfileField(
        "inline_code_max_chars",
        "structure",
        "int",
        40,
        "Inline code up to this length is spoken; longer becomes the code marker",
        "Short inline code (a flag or a name) is read out; anything this long or longer "
        "is replaced by the code marker, because a command line read aloud is noise.",
        "structure",
        0,
        500,
        unit="chars",
    ),
    ProfileField(
        "code_marker",
        "structure",
        "str",
        "Code omitted.",
        "Said in place of a code block (empty = skip silently)",
        "What the listener hears in place of a fenced code block or long inline code. "
        "Leave it empty to skip code without saying anything.",
        "structure",
    ),
    ProfileField(
        "diagram_marker",
        "structure",
        "str",
        "Diagram omitted.",
        "Said in place of a diagram (empty = skip silently)",
        "What the listener hears in place of a diagram (mermaid or SVG). Leave it empty to "
        "skip diagrams without saying anything.",
        "structure",
    ),
    ProfileField(
        "clip_min_chars",
        "clips",
        "int",
        20,
        "A shorter line folds into the next clip (headings, labels)",
        "Lines shorter than this join the next clip instead of being a request of their "
        "own. Some models clip or mispronounce very short inputs; raise it if one-word "
        "lines sound cut off.",
        "clips",
        0,
        200,
        unit="chars",
    ),
    ProfileField(
        "clip_max_chars",
        "clips",
        "int",
        600,
        "Longest clip; longer text splits at a comma or space",
        "Longest text per speech request. Smaller clips start playing sooner; larger ones "
        "keep intonation across sentences. 300–800 suits most models.",
        "clips",
        100,
        4000,
        unit="chars",
    ),
    ProfileField(
        "max_clips",
        "clips",
        "int",
        200,
        "Clips read per reply before it stops with a notice",
        "Stops a huge reply (a 500-row table) from queuing hundreds of requests. The "
        "listener is told where reading stopped.",
        "clips",
        1,
        1000,
        unit="clips",
    ),
)
_PROFILE_FIELD_BY_KEY = {f.key: f for f in PROFILE_FIELDS}


def _check_profile_value(field_def: ProfileField, value: Any, where: str) -> int | str:
    key = field_def.key
    if field_def.kind == "int":
        # bool is an int subclass; True must not read as 1 ms
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{where}: {key} must be a whole number")
        lo, hi = field_def.min_value, field_def.max_value
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise ValueError(f"{where}: {key} must be between {lo} and {hi}")
        return value
    if not isinstance(value, str):
        raise ValueError(f"{where}: {key} must be a string")
    if field_def.kind == "choice":
        if value not in field_def.choices:
            raise ValueError(f"{where}: {key} must be one of {', '.join(field_def.choices)}")
        return value
    text: str = value.strip()
    if len(text) > MAX_MARKER_CHARS:
        raise ValueError(f"{where}: {key} is longer than {MAX_MARKER_CHARS} characters")
    return text


def _profile_from(values: dict[str, int | str]) -> TtsProfile:
    return dataclasses.replace(DEFAULT_PROFILE, **values)  # type: ignore[arg-type]


def _check_profile_overrides(item: Any, where: str) -> dict[str, int | str]:
    if not isinstance(item, dict):
        raise ValueError(f"{where}: must be an object")
    out: dict[str, int | str] = {}
    for key, value in item.items():
        field_def = _PROFILE_FIELD_BY_KEY.get(key)
        if field_def is None:
            raise ValueError(f"{where}: unknown field {key!r}")
        out[key] = _check_profile_value(field_def, value, where)
    merged = _profile_from(out)
    if merged.clip_min_chars >= merged.clip_max_chars:
        raise ValueError(f"{where}: clip_min_chars must be smaller than clip_max_chars")
    return out


def _load_object(raw: str) -> dict[str, Any]:
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON: {exc.msg}") from None
    if not isinstance(data, dict):
        raise ValueError("must be a JSON object keyed by TTS model alias")
    return data


def parse_profiles(raw: str) -> dict[str, TtsProfile]:
    """Tolerant read: a bad field reads as its default, a bad profile is skipped.

    Per-field rather than per-profile tolerance because one stale value (a
    choice a later release dropped) should not reset an operator's other
    tuning.  An inconsistent clip_min/clip_max pair falls back as a pair.
    """
    try:
        data = _load_object(raw)
    except ValueError:
        return {}
    out: dict[str, TtsProfile] = {}
    for alias, item in data.items():
        if not isinstance(alias, str) or not alias.strip() or not isinstance(item, dict):
            continue
        if len(out) >= MAX_PROFILES:
            break
        good: dict[str, int | str] = {}
        for key, value in item.items():
            field_def = _PROFILE_FIELD_BY_KEY.get(key)
            if field_def is None:
                continue
            try:
                good[key] = _check_profile_value(field_def, value, alias)
            except ValueError:
                continue
        profile = _profile_from(good)
        if profile.clip_min_chars >= profile.clip_max_chars:
            profile = dataclasses.replace(
                profile,
                clip_min_chars=DEFAULT_PROFILE.clip_min_chars,
                clip_max_chars=DEFAULT_PROFILE.clip_max_chars,
            )
        out[alias.strip()] = profile
    return out


def validate_profiles(raw: str) -> str:
    """Strict write-path check; returns canonical JSON (overrides only) or raises."""
    data = _load_object(raw)
    if len(data) > MAX_PROFILES:
        raise ValueError(f"at most {MAX_PROFILES} profiles")
    out: dict[str, dict[str, int | str]] = {}
    for alias, item in data.items():
        name = alias.strip() if isinstance(alias, str) else ""
        if not name:
            raise ValueError("a profile needs a model alias")
        if len(name) > MAX_PROFILE_ALIAS_CHARS:
            raise ValueError(f"{name[:20]}…: alias is longer than {MAX_PROFILE_ALIAS_CHARS}")
        if name in out:
            raise ValueError(f"{name}: duplicate profile")
        out[name] = _check_profile_overrides(item, name)
    return json.dumps(out, ensure_ascii=False)


def resolve_profile(profiles: dict[str, TtsProfile], alias: str) -> TtsProfile:
    """The profile for TTS model *alias*; no profile (or no alias) → DEFAULT_PROFILE."""
    return profiles.get((alias or "").strip(), DEFAULT_PROFILE)


def profile_json(profile: TtsProfile) -> dict[str, Any]:
    """Every field, defaults included: the shape the player receives."""
    return dataclasses.asdict(profile)


# -- custom rewrite rules -----------------------------------------------------
#
# ``audio.tts_rewrite_rules`` is a JSON list of rules run after the built-in
# cleanup groups and before the dictionary.  A plain-text rule (``regex``
# false) is what most operators write: the find text is matched literally,
# whole-word and ignoring case, and the replacement is literal.  A regex rule
# is Python ``re`` syntax, case-sensitive unless it starts with ``(?i)``, and
# its replacement may use ``\1`` / ``\g<name>``.  The engine is Python because
# the cleanup runs on the server; the Text cleanup tab previews through the
# server for the same reason, so what an admin sees is what is spoken.
#
# Rules are admin-authored and compiled per read; a pathological regex can
# still backtrack.  The length caps bound the damage, and the write path
# rejects patterns that match empty text (which would insert the replacement
# between every character).

MAX_REWRITE_RULES = 100
MAX_RULE_FIND_CHARS = 500
MAX_RULE_REPLACE_CHARS = 500
MAX_RULE_TEST_CHARS = 500


@dataclass(frozen=True)
class RewriteRule:
    find: str
    regex: bool  # False: plain text, whole-word, ignoring case
    replace: str  # "" deletes the match
    test: str  # the editor's test line, kept as the rule's example
    enabled: bool


def compile_rewrite_rule(rule: RewriteRule) -> re.Pattern[str]:
    """Compile *rule*; raises ValueError with an editor-ready message."""
    if not rule.regex:
        words = [re.escape(w) for w in rule.find.split()]
        return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.IGNORECASE)
    try:
        pattern = re.compile(rule.find)
    except re.error as exc:
        where = f" at position {exc.pos}" if exc.pos is not None else ""
        raise ValueError(f"not a valid pattern: {exc.msg}{where}") from None
    if pattern.search("") is not None:
        raise ValueError(
            "this pattern can match empty text, which would insert the replacement "
            "between every character; make at least one part required"
        )
    try:
        pattern.sub(rule.replace, "")  # the template compiles eagerly: a bad \N raises here
    except (re.error, IndexError) as exc:
        raise ValueError(f"the replacement is not valid for this pattern: {exc}") from None
    return pattern


def expand_rewrite(rule: RewriteRule, match: re.Match[str]) -> str:
    """The replacement for one match: literal for plain text, \\1-expanded for regex."""
    return match.expand(rule.replace) if rule.regex else rule.replace


def _check_rule(item: Any, where: str) -> RewriteRule:
    if not isinstance(item, dict):
        raise ValueError(f"{where}: must be an object")
    find = _text(item, "find", MAX_RULE_FIND_CHARS, where, required=True)
    regex = _flag(item, "regex", False, where)
    replace = item.get("replace", "")
    if replace is None:
        replace = ""
    if not isinstance(replace, str):
        raise ValueError(f"{where}: replace must be a string")
    if len(replace) > MAX_RULE_REPLACE_CHARS:
        raise ValueError(f"{where}: replace is longer than {MAX_RULE_REPLACE_CHARS} characters")
    test = _text(item, "test", MAX_RULE_TEST_CHARS, where, required=False)
    rule = RewriteRule(find, regex, replace, test, _flag(item, "enabled", True, where))
    try:
        compile_rewrite_rule(rule)
    except ValueError as exc:
        raise ValueError(f"{where}: {exc}") from None
    return rule


def _rule_json(rule: RewriteRule) -> dict[str, Any]:
    return {
        "find": rule.find,
        "regex": rule.regex,
        "replace": rule.replace,
        "test": rule.test,
        "enabled": rule.enabled,
    }


def parse_rewrite_rules(raw: str) -> list[RewriteRule]:
    """Tolerant read: bad JSON → [], a rule that does not compile is skipped.

    Disabled rules are kept (the editor lists them); callers filter on
    ``enabled``.
    """
    try:
        items = _load_list(raw)
    except ValueError:
        return []
    out: list[RewriteRule] = []
    for i, item in enumerate(items[:MAX_REWRITE_RULES]):
        try:
            out.append(_check_rule(item, f"rule {i + 1}"))
        except ValueError:
            continue
    return out


def validate_rewrite_rules(raw: str) -> str:
    """Strict write-path check; returns canonical JSON or raises ValueError."""
    items = _load_list(raw)
    if len(items) > MAX_REWRITE_RULES:
        raise ValueError(f"at most {MAX_REWRITE_RULES} rewrite rules")
    rules = [_check_rule(item, f"rule {i + 1}") for i, item in enumerate(items)]
    return json.dumps([_rule_json(r) for r in rules], ensure_ascii=False)


# -- cleanup groups and built-in terms ----------------------------------------


@dataclass(frozen=True)
class CleanupGroup:
    id: str
    label: str
    description: str
    example: str  # input the Text cleanup tab runs through this group alone


# Display order.  speech_text keeps its own rule order (some groups' rules are
# interleaved there on purpose); this table only names and documents them.
CLEANUP_GROUPS: tuple[CleanupGroup, ...] = (
    CleanupGroup(
        "phone_numbers",
        "phone numbers",
        "Ten-digit numbers, read digit by digit",
        "Call 303-555-0142",
    ),
    CleanupGroup("dates", "dates", "ISO dates and year-months", "Shipped 2026-10-16"),
    CleanupGroup(
        "file_names", "file names", "The dot before a known file extension", "Edit config.yaml"
    ),
    CleanupGroup(
        "versions", "versions", "Dotted versions with three or more parts", "Upgrade to v1.8.5"
    ),
    CleanupGroup(
        "symbols", "symbols", "~, →, ≈, ±, % and × between numbers", "~5 users, a -> b, 50%"
    ),
    CleanupGroup("dimensions", "dimensions", "Sizes written NxM", "A 1024x768 screen"),
    CleanupGroup(
        "units",
        "units",
        "Size, time, rate and frequency units after a number",
        "120ms, 32GB, 300W",
    ),
    CleanupGroup(
        "seconds_decades",
        "seconds vs decades",
        "“90s” as seconds unless it reads as a decade",
        "the 90s vs a 90s timeout",
    ),
    CleanupGroup("ranges", "ranges", "Number ranges", "Takes 10-20 minutes"),
    CleanupGroup("abbreviations", "abbreviations", "e.g., i.e., etc., vs.", "e.g. this, i.e. that"),
    CleanupGroup(
        "identifiers",
        "identifiers",
        "snake_case names read as words, known acronyms spelled out",
        "Check user_id and aid_kit",
    ),
)
CLEANUP_GROUP_IDS = frozenset(g.id for g in CLEANUP_GROUPS)

# Respellings Turnstone ships.  Each can be switched off in the Dictionary tab
# (``audio.tts_builtin_terms_off``), and a dictionary entry for the same term
# overrides it.
BUILTIN_TERMS: tuple[tuple[str, str], ...] = (
    ("PostgreSQL", "Postgres"),
    ("systemctl", "system control"),
    ("kubectl", "kube control"),
    ("YAML", "yammel"),
    ("JSON", "jay-son"),
)
_BUILTIN_TERM_NAMES = frozenset(t for t, _ in BUILTIN_TERMS)


def _parse_names(raw: str, allowed: frozenset[str]) -> frozenset[str]:
    try:
        items = _load_list(raw)
    except ValueError:
        return frozenset()
    return frozenset(i for i in items if isinstance(i, str) and i in allowed)


def _validate_names(raw: str, allowed: frozenset[str], what: str) -> str:
    out: list[str] = []
    for item in _load_list(raw):
        if not isinstance(item, str) or item not in allowed:
            raise ValueError(f"unknown {what} {item!r}")
        if item not in out:
            out.append(item)
    return json.dumps(out, ensure_ascii=False)


def parse_disabled_groups(raw: str) -> frozenset[str]:
    """Tolerant: unknown ids (a group a later release removed) are ignored."""
    return _parse_names(raw, CLEANUP_GROUP_IDS)


def validate_disabled_groups(raw: str) -> str:
    return _validate_names(raw, CLEANUP_GROUP_IDS, "group")


def parse_builtin_terms_off(raw: str) -> frozenset[str]:
    return _parse_names(raw, _BUILTIN_TERM_NAMES)


def validate_builtin_terms_off(raw: str) -> str:
    return _validate_names(raw, _BUILTIN_TERM_NAMES, "built-in term")


@dataclass(frozen=True)
class CleanupOptions:
    """Everything besides the dictionary that shapes the text a TTS model gets."""

    disabled_groups: frozenset[str] = frozenset()
    rules: tuple[RewriteRule, ...] = ()  # every stored rule; apply only ``enabled`` ones
    builtin_terms_off: frozenset[str] = frozenset()


def cleanup_options(get: Callable[[str], Any]) -> CleanupOptions:
    """Read the cleanup settings through *get* (e.g. ``config_store.get``), tolerantly."""

    def raw(key: str) -> str:
        value = get(key)
        return value if isinstance(value, str) else ""

    return CleanupOptions(
        disabled_groups=parse_disabled_groups(raw("audio.tts_cleanup_disabled")),
        rules=tuple(parse_rewrite_rules(raw("audio.tts_rewrite_rules"))),
        builtin_terms_off=parse_builtin_terms_off(raw("audio.tts_builtin_terms_off")),
    )


# -- preview ------------------------------------------------------------------


@dataclass(frozen=True)
class PreviewSpan:
    """One changed stretch of the cleaned text, in output coordinates."""

    start: int
    end: int
    source: str  # "group" | "custom" | "dictionary"
    label: str  # group id, "custom 3" (1-based rule number) or "dictionary"


@dataclass(frozen=True)
class SpeechPreview:
    text: str  # identical to what normalize_for_speech returns for the same input
    spans: tuple[PreviewSpan, ...]


# ---------------------------------------------------------------------------
# Voice persona.  A voice session is a coordinator created with this persona;
# its memory scope, spawn cap and child-model default all key off the name, so
# a voice conversation never inherits the text coordinator's work habits.
# ---------------------------------------------------------------------------

VOICE_PERSONA = "voice"
VOICE_MEMORY_SCOPE = "voice"


def is_voice_persona(name: str | None) -> bool:
    """Persona names arrive from session config, API bodies and tool args with
    inconsistent case and padding; one forgiving check keeps them agreeing."""
    return (name or "").strip().lower() == VOICE_PERSONA


# Lifecycle of a finished agent's report in a voice session: written pending,
# offered on the next spoken turn, then read or dismissed by the user.
VOICE_REPORT_STATES = ("pending", "offered", "read", "dismissed")
