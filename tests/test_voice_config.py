"""Tests for turnstone.core.voice_config — the voice dictionary and speaking styles."""

from __future__ import annotations

import json

import pytest

from turnstone.core.settings_registry import validate_key, validate_value
from turnstone.core.voice_config import (
    DEFAULT_STYLES,
    MAX_ALSO_HEARS,
    MAX_DICTIONARY_ENTRIES,
    MAX_STYLES,
    DictionaryEntry,
    SpeakingStyle,
    parse_dictionary,
    parse_styles,
    resolve_style,
    validate_dictionary,
    validate_styles,
)


def _entry(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "term": "BAA",
        "kind": "respelling",
        "respelling": "B A A",
        "ipa": "",
        "match_case": True,
        "also_hears": ["Bay A"],
    }
    base.update(kw)
    return base


# -- dictionary: tolerant parse (read path) ---------------------------------


def test_parse_dictionary_round_trips_a_full_entry() -> None:
    raw = json.dumps(
        [
            _entry(),
            _entry(
                term="nginx",
                kind="ipa",
                ipa="/ˈɛndʒɪn ˈɛks/",
                respelling="engine ex",
                match_case=False,
                also_hears=["engine x"],
            ),
        ]
    )
    got = parse_dictionary(raw)
    assert got == [
        DictionaryEntry("BAA", "respelling", "B A A", "", True, ("Bay A",)),
        DictionaryEntry("nginx", "ipa", "engine ex", "/ˈɛndʒɪn ˈɛks/", False, ("engine x",)),
    ]


@pytest.mark.parametrize("raw", ["", "   ", "not json", "{}", "42", "null", '"x"'])
def test_parse_dictionary_degrades_to_empty(raw: str) -> None:
    assert parse_dictionary(raw) == []


def test_parse_dictionary_skips_bad_entries_and_keeps_good_ones() -> None:
    raw = json.dumps(
        [
            "nope",
            {"term": ""},
            {"term": "x" * 101, "respelling": "y"},
            {"term": "lonely"},  # nothing to say or hear
            _entry(term="SQL", respelling="sequel", also_hears=[]),
        ]
    )
    assert [e.term for e in parse_dictionary(raw)] == ["SQL"]


def test_parse_dictionary_defaults_missing_fields() -> None:
    got = parse_dictionary(json.dumps([{"term": "SQL", "respelling": "sequel"}]))
    assert got == [DictionaryEntry("SQL", "respelling", "sequel", "", False, ())]


def test_parse_dictionary_drops_ipa_on_respelling_entries() -> None:
    got = parse_dictionary(json.dumps([_entry(ipa="/x/")]))
    assert got[0].ipa == ""


def test_parse_dictionary_later_duplicate_wins() -> None:
    raw = json.dumps(
        [
            _entry(term="sql", respelling="S Q L", match_case=False),
            _entry(term="SQL", respelling="sequel", match_case=False),
            _entry(term="SQL", respelling="ess cue ell", match_case=True),
        ]
    )
    got = parse_dictionary(raw)
    # case-insensitive "sql"/"SQL" collide; the case-sensitive one is distinct
    assert [(e.term, e.respelling, e.match_case) for e in got] == [
        ("SQL", "sequel", False),
        ("SQL", "ess cue ell", True),
    ]


def test_parse_dictionary_caps_entries_and_also_hears() -> None:
    many = [
        _entry(term=f"t{i}", also_hears=[f"h{j}" for j in range(MAX_ALSO_HEARS + 5)])
        for i in range(MAX_DICTIONARY_ENTRIES + 10)
    ]
    got = parse_dictionary(json.dumps(many))
    assert len(got) == MAX_DICTIONARY_ENTRIES
    assert len(got[0].also_hears) == MAX_ALSO_HEARS


# -- dictionary: strict validation (write path) -----------------------------


def test_validate_dictionary_returns_canonical_json() -> None:
    out = validate_dictionary(json.dumps([{"term": " SQL ", "respelling": " sequel "}]))
    assert json.loads(out) == [
        {
            "term": "SQL",
            "kind": "respelling",
            "respelling": "sequel",
            "ipa": "",
            "match_case": False,
            "also_hears": [],
        }
    ]


def test_validate_dictionary_accepts_empty() -> None:
    assert validate_dictionary("") == "[]"
    assert validate_dictionary("[]") == "[]"


@pytest.mark.parametrize(
    ("value", "needle"),
    [
        ("not json", "JSON"),
        ("{}", "list"),
        (json.dumps(["x"]), "entry 1"),
        (json.dumps([{"term": ""}]), "term"),
        (json.dumps([_entry(kind="ssml")]), "kind"),
        (json.dumps([_entry(kind="ipa", ipa="")]), "ipa"),
        (json.dumps([{"term": "lonely"}]), "nothing"),
        (json.dumps([_entry(respelling="y" * 101)]), "respelling"),
        (json.dumps([_entry(also_hears="Bay A")]), "also_hears"),
        (json.dumps([_entry(also_hears=["h"] * (MAX_ALSO_HEARS + 1))]), "also_hears"),
        (json.dumps([_entry(match_case="yes")]), "match_case"),
        (json.dumps([_entry(), _entry(respelling="other")]), "duplicate"),
        (json.dumps([_entry(term=f"t{i}") for i in range(MAX_DICTIONARY_ENTRIES + 1)]), "200"),
    ],
)
def test_validate_dictionary_rejects(value: str, needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        validate_dictionary(value)


# -- styles -----------------------------------------------------------------


def test_default_styles_are_valid_and_seeded() -> None:
    styles = parse_styles(DEFAULT_STYLES)
    assert [s.name for s in styles] == ["Professional", "Social", "Plain"]
    assert styles[2].instruction == ""
    assert validate_styles(DEFAULT_STYLES) == DEFAULT_STYLES


@pytest.mark.parametrize("raw", ["", "nope", "{}", "[1]"])
def test_parse_styles_degrades_to_empty(raw: str) -> None:
    assert parse_styles(raw) == []


def test_parse_styles_skips_bad_and_duplicate_names() -> None:
    raw = json.dumps(
        [
            {"name": "", "instruction": "x"},
            {"name": "Calm", "instruction": "Be calm."},
            {"name": "calm", "instruction": "Later wins."},
            {"name": "Hidden", "instruction": "", "in_menu": False},
        ]
    )
    assert parse_styles(raw) == [
        SpeakingStyle("calm", "Later wins.", True),
        SpeakingStyle("Hidden", "", False),
    ]


@pytest.mark.parametrize(
    ("value", "needle"),
    [
        ("nope", "JSON"),
        (json.dumps([{"name": ""}]), "name"),
        (json.dumps([{"name": "x" * 41}]), "name"),
        (json.dumps([{"name": "A", "instruction": "y" * 501}]), "instruction"),
        (json.dumps([{"name": "A"}, {"name": "a"}]), "duplicate"),
        (json.dumps([{"name": f"s{i}"} for i in range(MAX_STYLES + 1)]), "20"),
        (json.dumps([{"name": "A", "in_menu": "no"}]), "in_menu"),
    ],
)
def test_validate_styles_rejects(value: str, needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        validate_styles(value)


def test_resolve_style_is_case_insensitive_and_empty_means_none() -> None:
    styles = parse_styles(DEFAULT_STYLES)
    assert resolve_style(styles, "professional") == styles[0]
    assert resolve_style(styles, "") is None
    assert resolve_style(styles, "  ") is None
    assert resolve_style(styles, "Missing") is None


# -- registry wiring --------------------------------------------------------


def test_registry_declares_the_voice_settings() -> None:
    for key, typ, default in [
        ("audio.voice_dictionary", "str", "[]"),
        ("audio.tts_styles", "str", DEFAULT_STYLES),
        ("audio.tts_style", "str", ""),
        ("audio.stt_dictionary_hint", "bool", True),
        ("audio.stt_dictionary_correct", "bool", True),
        ("audio.tts_playback_rate", "float", 1.0),
    ]:
        d = validate_key(key)
        assert (d.type, d.default, d.section) == (typ, default, "audio")
        assert d.help


def test_registry_write_path_validates_and_canonicalises() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        validate_value("audio.voice_dictionary", json.dumps([_entry(), _entry()]))
    out = validate_value("audio.tts_styles", json.dumps([{"name": " Calm "}]))
    assert json.loads(out) == [{"name": "Calm", "instruction": "", "in_menu": True}]


def test_registry_read_path_does_not_raise_on_a_bad_stored_value() -> None:
    # clamp_range=True is the lenient read path; a bad stored blob must not
    # break the settings page — the tolerant parser handles it at use time.
    assert validate_value("audio.voice_dictionary", "not json", clamp_range=True) == "not json"


def test_playback_rate_is_bounded() -> None:
    assert validate_value("audio.tts_playback_rate", "1.25") == 1.25
    for bad in ("0.4", "2.5"):
        with pytest.raises(ValueError):
            validate_value("audio.tts_playback_rate", bad)
