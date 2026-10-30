"""Tests for the read-aloud tuning contract in turnstone.core.voice_config.

Pacing profiles (per TTS model), custom rewrite rules, the cleanup rule groups
and the built-in terms — the settings the Text cleanup tab and the Settings
tab's Pacing / Structure / Clips groups edit.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from turnstone.core.settings_registry import validate_key, validate_value
from turnstone.core.voice_config import (
    BUILTIN_TERMS,
    CLEANUP_GROUPS,
    DEFAULT_PROFILE,
    MAX_PROFILES,
    MAX_REWRITE_RULES,
    MAX_RULE_FIND_CHARS,
    PROFILE_FIELDS,
    CleanupOptions,
    RewriteRule,
    TtsProfile,
    cleanup_options,
    compile_rewrite_rule,
    expand_rewrite,
    parse_builtin_terms_off,
    parse_disabled_groups,
    parse_profiles,
    parse_rewrite_rules,
    profile_json,
    resolve_profile,
    validate_builtin_terms_off,
    validate_disabled_groups,
    validate_profiles,
    validate_rewrite_rules,
)

# -- profiles -----------------------------------------------------------------


def test_profile_fields_describe_every_profile_attribute_with_help() -> None:
    names = [f.name for f in dataclasses.fields(TtsProfile)]
    assert [f.key for f in PROFILE_FIELDS] == names
    for f in PROFILE_FIELDS:
        assert f.group in ("pacing", "structure", "clips")
        assert f.label and f.help and f.anchor
        assert getattr(DEFAULT_PROFILE, f.key) == f.default


def test_default_profile_is_todays_hard_coded_player() -> None:
    # tts_player.js constants before this change; a model with no profile must
    # sound exactly as it did.
    assert profile_json(DEFAULT_PROFILE) == {
        "pause_heading_ms": 600,
        "pause_paragraph_ms": 500,
        "pause_table_row_ms": 150,
        "pause_code_ms": 400,
        "pause_list_item_ms": 0,
        "table_cell_joiner": "comma",
        "table_header_mode": "row",
        "inline_code_max_chars": 40,
        "code_marker": "Code omitted.",
        "diagram_marker": "Diagram omitted.",
        "clip_min_chars": 20,
        "clip_max_chars": 600,
        "max_clips": 200,
    }


def test_parse_profiles_fills_missing_fields_and_resolves_by_alias() -> None:
    raw = json.dumps({"tts-qwen": {"pause_paragraph_ms": 250, "code_marker": ""}})
    profiles = parse_profiles(raw)
    p = resolve_profile(profiles, "tts-qwen")
    assert p.pause_paragraph_ms == 250
    assert p.code_marker == ""  # empty marker = skip silently, a valid choice
    assert p.pause_heading_ms == 600
    assert resolve_profile(profiles, "tts-piper") == DEFAULT_PROFILE
    assert resolve_profile(profiles, "") == DEFAULT_PROFILE


def test_parse_profiles_is_tolerant_per_field() -> None:
    raw = json.dumps(
        {
            "a": {"pause_heading_ms": -5, "table_cell_joiner": "tab", "max_clips": "lots"},
            "b": "not an object",
            "c": {"clip_min_chars": 150, "clip_max_chars": 120},
        }
    )
    profiles = parse_profiles(raw)
    assert profiles["a"] == DEFAULT_PROFILE
    assert "b" not in profiles
    # an inconsistent pair falls back as a pair
    assert (profiles["c"].clip_min_chars, profiles["c"].clip_max_chars) == (20, 600)
    assert parse_profiles("not json") == {}
    assert parse_profiles("[]") == {}
    assert parse_profiles("") == {}


def test_validate_profiles_names_the_bad_field() -> None:
    with pytest.raises(ValueError, match=r"tts-qwen: pause_heading_ms must be between 0 and 5000"):
        validate_profiles(json.dumps({"tts-qwen": {"pause_heading_ms": 9000}}))
    with pytest.raises(ValueError, match="table_cell_joiner must be one of"):
        validate_profiles(json.dumps({"x": {"table_cell_joiner": "tab"}}))
    with pytest.raises(ValueError, match="unknown field"):
        validate_profiles(json.dumps({"x": {"pause_everything": 1}}))
    with pytest.raises(ValueError, match="clip_min_chars must be smaller than clip_max_chars"):
        validate_profiles(json.dumps({"x": {"clip_min_chars": 150, "clip_max_chars": 120}}))
    with pytest.raises(ValueError, match="must be a JSON object"):
        validate_profiles("[]")
    too_many = {f"m{i}": {} for i in range(MAX_PROFILES + 1)}
    with pytest.raises(ValueError, match="at most"):
        validate_profiles(json.dumps(too_many))


def test_validate_profiles_keeps_only_given_fields() -> None:
    # Stored values are overrides: a later change to a default reaches every
    # profile that never set that field.
    out = validate_profiles(json.dumps({" tts-qwen ": {"pause_paragraph_ms": 250}}))
    assert json.loads(out) == {"tts-qwen": {"pause_paragraph_ms": 250}}
    assert validate_profiles("") == "{}"


def test_bool_is_not_an_int_field() -> None:
    with pytest.raises(ValueError, match="must be a whole number"):
        validate_profiles(json.dumps({"x": {"max_clips": True}}))


# -- rewrite rules ------------------------------------------------------------


def _rule(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {"find": "k8s", "regex": False, "replace": "kubernetes"}
    base.update(kw)
    return base


def test_plain_text_rule_is_whole_word_and_ignores_case() -> None:
    rule = RewriteRule("k8s", False, "kubernetes", "", True)
    pat = compile_rewrite_rule(rule)
    text = "Deploy to k8s, then the K8s build; kubectl and k8sctl stay."
    assert pat.sub(lambda m: expand_rewrite(rule, m), text) == (
        "Deploy to kubernetes, then the kubernetes build; kubectl and k8sctl stay."
    )


def test_plain_text_rule_treats_symbols_literally() -> None:
    rule = RewriteRule("w/o", False, r"with\out $1", "", True)
    pat = compile_rewrite_rule(rule)
    # the replacement is literal too: no group syntax in plain-text rules
    assert pat.sub(lambda m: expand_rewrite(rule, m), "runs w/o a GPU") == r"runs with\out $1 a GPU"


def test_plain_text_rule_matches_flexible_inner_whitespace() -> None:
    rule = RewriteRule("pull request", False, "PR", "", True)
    assert compile_rewrite_rule(rule).search("a pull  request here")


def test_regex_rule_expands_groups_python_style() -> None:
    rule = RewriteRule(r"\bPR (\d+)", True, r"pull request \1", "", True)
    pat = compile_rewrite_rule(rule)
    assert (
        pat.sub(lambda m: expand_rewrite(rule, m), "Merged PR 119.") == "Merged pull request 119."
    )
    named = RewriteRule(r"(?P<t>DT|TT)-(?P<n>\d+)", True, r"\g<t> \g<n>", "", True)
    pat = compile_rewrite_rule(named)
    assert pat.sub(lambda m: expand_rewrite(named, m), "DT-1412") == "DT 1412"


def test_regex_rule_is_case_sensitive_unless_flagged() -> None:
    assert not compile_rewrite_rule(RewriteRule("pr", True, "x", "", True)).search("PR")
    assert compile_rewrite_rule(RewriteRule("(?i)pr", True, "x", "", True)).search("PR")


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        (_rule(find="(unclosed", regex=True), "not a valid pattern"),
        (_rule(find="a*", regex=True), "can match empty text"),
        (_rule(find="(a)", regex=True, replace=r"\2"), "replacement"),
        (_rule(find=""), "find is required"),
        (_rule(find="x" * (MAX_RULE_FIND_CHARS + 1)), "longer than"),
        (_rule(regex="yes"), "regex must be true or false"),
    ],
)
def test_validate_rewrite_rules_rejects(rule: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=f"rule 1: .*{message}"):
        validate_rewrite_rules(json.dumps([rule]))


def test_validate_rewrite_rules_canonicalises_and_caps() -> None:
    out = validate_rewrite_rules(json.dumps([_rule(find=" k8s ", test="to k8s")]))
    assert json.loads(out) == [
        {"find": "k8s", "regex": False, "replace": "kubernetes", "test": "to k8s", "enabled": True}
    ]
    # an empty replacement deletes the match: allowed, and not stripped to nothing else
    out = validate_rewrite_rules(json.dumps([_rule(replace="")]))
    assert json.loads(out)[0]["replace"] == ""
    with pytest.raises(ValueError, match="at most"):
        validate_rewrite_rules(json.dumps([_rule()] * (MAX_REWRITE_RULES + 1)))
    assert validate_rewrite_rules("") == "[]"


def test_replacement_keeps_its_spaces() -> None:
    out = validate_rewrite_rules(json.dumps([_rule(replace=" kube ")]))
    assert json.loads(out)[0]["replace"] == " kube "


def test_parse_rewrite_rules_skips_bad_rules_and_keeps_disabled_ones() -> None:
    raw = json.dumps(
        [
            _rule(),
            _rule(find="(bad", regex=True),
            _rule(find="w/o", replace="without", enabled=False),
            "nonsense",
        ]
    )
    rules = parse_rewrite_rules(raw)
    assert [(r.find, r.enabled) for r in rules] == [("k8s", True), ("w/o", False)]
    assert parse_rewrite_rules("not json") == []


# -- groups and built-in terms ------------------------------------------------


def test_cleanup_groups_are_unique_and_documented() -> None:
    ids = [g.id for g in CLEANUP_GROUPS]
    assert len(ids) == len(set(ids))
    assert "identifiers" in ids
    for g in CLEANUP_GROUPS:
        assert g.label and g.description and g.example


def test_disabled_groups_round_trip() -> None:
    assert (
        validate_disabled_groups(json.dumps(["units", "versions", "units"]))
        == '["units", "versions"]'
    )
    with pytest.raises(ValueError, match="unknown group 'colours'"):
        validate_disabled_groups(json.dumps(["colours"]))
    # a group removed in a later release must not break a stored value
    assert parse_disabled_groups(json.dumps(["units", "colours"])) == frozenset({"units"})
    assert parse_disabled_groups("garbage") == frozenset()


def test_builtin_terms_off_round_trip() -> None:
    terms = [t for t, _ in BUILTIN_TERMS]
    assert "YAML" in terms
    assert validate_builtin_terms_off(json.dumps(["YAML"])) == '["YAML"]'
    with pytest.raises(ValueError, match="unknown built-in term"):
        validate_builtin_terms_off(json.dumps(["nginx"]))
    assert parse_builtin_terms_off(json.dumps(["YAML", "nginx"])) == frozenset({"YAML"})


def test_cleanup_options_reads_every_setting_tolerantly() -> None:
    stored = {
        "audio.tts_cleanup_disabled": json.dumps(["units"]),
        "audio.tts_rewrite_rules": json.dumps([_rule()]),
        "audio.tts_builtin_terms_off": json.dumps(["JSON"]),
    }
    opts = cleanup_options(stored.get)
    assert opts.disabled_groups == frozenset({"units"})
    assert [r.find for r in opts.rules] == ["k8s"]
    assert opts.builtin_terms_off == frozenset({"JSON"})
    assert cleanup_options(lambda _k: None) == CleanupOptions()
    assert cleanup_options(lambda _k: "garbage") == CleanupOptions()


# -- registry -----------------------------------------------------------------


def test_registry_declares_the_tuning_settings() -> None:
    for key, default in [
        ("audio.tts_profiles", "{}"),
        ("audio.tts_rewrite_rules", "[]"),
        ("audio.tts_cleanup_disabled", "[]"),
        ("audio.tts_builtin_terms_off", "[]"),
    ]:
        d = validate_key(key)
        assert (d.type, d.default, d.section) == ("str", default, "audio")
        assert d.help


def test_registry_validates_tuning_writes() -> None:
    with pytest.raises(ValueError, match="can match empty text"):
        validate_value("audio.tts_rewrite_rules", json.dumps([_rule(find="x?", regex=True)]))
    with pytest.raises(ValueError):
        validate_value("audio.tts_profiles", json.dumps({"x": {"max_clips": 0}}))
    assert validate_value("audio.tts_profiles", "garbage", clamp_range=True) == "garbage"


_UNHOLDABLE_JSON = [
    '{"a": ' + "[" * 100_000 + "]" * 100_000 + "}",  # nested past the recursion limit
    '{"a": {"speed": ' + "9" * 5000 + "}}",  # an integer past the conversion limit
]


@pytest.mark.parametrize("value", _UNHOLDABLE_JSON, ids=["too-deep", "huge-int"])
def test_profiles_json_the_decoder_cannot_hold_is_malformed(value: str) -> None:
    # The save refuses it like any bad JSON; the tolerant read falls back to no
    # profiles instead of raising out of every speech request.
    with pytest.raises(ValueError, match="not valid JSON"):
        validate_profiles(value)
    assert parse_profiles(value) == {}
