"""Voice Styles tab (Admin › Voice): the pure helpers behind the list and shelf.

``turnstone/console/static/voice_styles.js`` is a classic script; its pure
helpers are exposed under ``window.TurnstoneVoiceStyles`` so they run under
node without a DOM.  The mount/render/shelf wiring is DOM-only and
browser-verified; what is pinned here is parsing, display state, draft
validation and the two "which setting do we also write" decisions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._js_harness_helpers import node_skip, run_node_source

pytestmark = node_skip

_JS = Path(__file__).resolve().parents[1] / "turnstone" / "console" / "static" / "voice_styles.js"


def _eval(expr: str) -> Any:
    src = (
        "global.window = global;\n"
        + _JS.read_text(encoding="utf-8")
        + "\nconst VS = global.TurnstoneVoiceStyles;\n"
        + f"const out = ({expr});\n"
        + "process.stdout.write(JSON.stringify(out === undefined ? null : out));\n"
    )
    result = run_node_source(src)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# -- parsing the stored setting (tolerant, mirrors voice_config.parse_styles) --


def test_parse_styles_degrades_to_empty_on_bad_json() -> None:
    raws = ["", "   ", "not json", "{}", "42", "null", '"x"']
    expr = "[" + ",".join(f"VS.parseStyles({json.dumps(raw)})" for raw in raws) + "]"
    assert _eval(expr) == [[] for _ in raws]


def test_parse_styles_skips_bad_entries_and_defaults_missing_fields() -> None:
    raw = json.dumps(
        [
            "nope",
            {"name": ""},
            {"name": "x" * 41},
            {"name": "Long", "instruction": "y" * 501},
            {"name": "Bad", "in_menu": "no"},
            {"name": "Calm", "instruction": " Be calm. "},
            {"name": "calm", "instruction": "Later wins.", "in_menu": False},
            {"name": "Hidden", "instruction": "", "in_menu": False},
        ]
    )
    assert _eval(f"VS.parseStyles({json.dumps(raw)})") == [
        {"name": "calm", "instruction": "Later wins.", "in_menu": False},
        {"name": "Hidden", "instruction": "", "in_menu": False},
    ]


def test_parse_styles_caps_the_list_but_later_duplicates_still_win() -> None:
    many = [{"name": f"style {i}", "instruction": "", "in_menu": True} for i in range(20)]
    many.append({"name": "STYLE 3", "instruction": "later", "in_menu": False})
    many.append({"name": "style 20", "instruction": "", "in_menu": True})
    raw = json.dumps(many)
    got = _eval(f"VS.parseStyles({json.dumps(raw)})")
    assert len(got) == 20
    assert got[-1]["name"] == "STYLE 3"
    assert got[-1]["instruction"] == "later"
    assert [s["name"] for s in got].count("style 20") == 0


# -- the list's display state -------------------------------------------------


def test_style_rows_mark_the_default_case_insensitively() -> None:
    styles = [
        {"name": "Professional", "instruction": "Be calm.", "in_menu": True},
        {"name": "Plain", "instruction": "", "in_menu": False},
    ]
    rows = _eval(f"VS.styleRows({json.dumps(styles)}, ' professional ')")
    assert rows[0]["isDefault"] is True
    assert rows[1]["isDefault"] is False
    assert rows[0]["inMenu"] is True
    assert rows[0]["inMenuText"] == "yes"
    assert rows[1]["inMenuText"] == "\u2014"
    assert rows[0]["instructionText"] == "Be calm."
    assert rows[1]["instructionText"] == "(no instruction)"
    assert _eval(f"VS.styleRows({json.dumps(styles)}, '')")[0]["isDefault"] is False


# -- draft validation and normalization --------------------------------------


def test_validate_draft_requires_a_name_and_enforces_the_caps() -> None:
    assert _eval("VS.validateDraft({name: '', instruction: ''}, [], null)") == "Name is required"
    assert _eval("VS.validateDraft({name: '  Calm  '}, [], null)") is None
    long_name = json.dumps("x" * 41)
    assert "40" in _eval(f"VS.validateDraft({{name: {long_name}}}, [], null)")
    long_instruction = json.dumps("y" * 501)
    assert "500" in _eval(
        f"VS.validateDraft({{name: 'A', instruction: {long_instruction}}}, [], null)"
    )


def test_validate_draft_rejects_duplicate_names_except_the_edited_row() -> None:
    styles = [{"name": "Calm", "instruction": "", "in_menu": True}]
    assert "already exists" in _eval(
        f"VS.validateDraft({{name: 'calm'}}, {json.dumps(styles)}, null)"
    )
    assert _eval(f"VS.validateDraft({{name: 'CALM'}}, {json.dumps(styles)}, 0)") is None


def test_validate_draft_caps_new_styles_but_not_edits() -> None:
    styles = [{"name": f"style {i}", "instruction": "", "in_menu": True} for i in range(20)]
    assert "20" in _eval(f"VS.validateDraft({{name: 'new'}}, {json.dumps(styles)}, null)")
    assert _eval(f"VS.validateDraft({{name: 'renamed'}}, {json.dumps(styles)}, 0)") is None


def test_apply_draft_trims_and_replaces_or_appends_without_mutating() -> None:
    styles = [{"name": "Calm", "instruction": "old", "in_menu": True}]
    out = _eval(
        f"VS.applyDraft({json.dumps(styles)}, 0, "
        "{name: ' Pro ', instruction: '  later  ', in_menu: false})"
    )
    assert out == [{"name": "Pro", "instruction": "later", "in_menu": False}]
    assert _eval(f"VS.applyDraft({json.dumps(styles)}, null, {{name: 'Plain'}})") == [
        {"name": "Calm", "instruction": "old", "in_menu": True},
        {"name": "Plain", "instruction": "", "in_menu": True},
    ]
    assert styles == [{"name": "Calm", "instruction": "old", "in_menu": True}]


def test_serialize_styles_emits_the_wire_shape() -> None:
    out = _eval("VS.serializeStyles([{name: 'Calm', instruction: 'Be calm.', in_menu: false}])")
    assert json.loads(out) == [{"name": "Calm", "instruction": "Be calm.", "in_menu": False}]


# -- which tts_style write follows a styles save -----------------------------


def test_default_update_for_create_and_existing_styles() -> None:
    cases = [
        # current default, old name, new name, default ticked -> tts_style write
        ("", None, "Calm", True, "Calm"),
        ("", None, "Calm", False, None),
        ("Professional", "Professional", "Pro", True, "Pro"),
        ("professional", "Professional", "Plain", False, ""),
        ("Professional", "Social", "Social", False, None),
        ("Professional", "Social", "Friendly", True, "Friendly"),
        ("", "Professional", "Pro", False, None),
        ("Professional", "Professional", "Pro", False, ""),
    ]
    expr = "["
    for current, old, new, ticked, _ in cases:
        expr += (
            "VS.defaultUpdateFor("
            + f"{json.dumps(current)}, {json.dumps(old)}, {json.dumps(new)}, "
            + ("true" if ticked else "false")
            + "),"
        )
    expr += "]"
    assert _eval(expr) == [expected for *_, expected in cases]


def test_delete_update_clears_the_default_only_for_the_deleted_style() -> None:
    styles = [
        {"name": "Calm", "instruction": "", "in_menu": True},
        {"name": "Plain", "instruction": "", "in_menu": True},
    ]
    out = _eval(f"VS.deleteUpdate({json.dumps(styles)}, ' plain ', 1)")
    assert out == {
        "styles": [{"name": "Calm", "instruction": "", "in_menu": True}],
        "clearDefault": True,
    }
    out = _eval(f"VS.deleteUpdate({json.dumps(styles)}, 'Calm', 1)")
    assert out["clearDefault"] is False
    assert len(out["styles"]) == 1


def test_save_plan_carries_the_whole_list_then_the_default_choice() -> None:
    styles = [
        {"name": "Professional", "instruction": "Be calm.", "in_menu": True},
        {"name": "Plain", "instruction": "", "in_menu": True},
    ]
    # Create a style and tick Default: the list blob plus the new default name.
    created = _eval(
        f"VS.savePlan({json.dumps(styles)}, null, "
        "{name: ' Calm ', instruction: '  Softly.  ', in_menu: true}, "
        "'Professional', true)"
    )
    assert json.loads(created["stylesValue"]) == [
        {"name": "Professional", "instruction": "Be calm.", "in_menu": True},
        {"name": "Plain", "instruction": "", "in_menu": True},
        {"name": "Calm", "instruction": "Softly.", "in_menu": True},
    ]
    assert created["defaultStyle"] == "Calm"

    # Rename the default: the list carries the new name and tts_style follows.
    renamed = _eval(
        f"VS.savePlan({json.dumps(styles)}, 0, "
        "{name: 'Pro', instruction: 'Be calm.', in_menu: true}, "
        "'professional', true)"
    )
    assert json.loads(renamed["stylesValue"])[0]["name"] == "Pro"
    assert renamed["defaultStyle"] == "Pro"

    # Editing a non-default with Default unticked leaves tts_style alone.
    untouched = _eval(
        f"VS.savePlan({json.dumps(styles)}, 1, "
        "{name: 'Plain', instruction: '', in_menu: false}, "
        "'Professional', false)"
    )
    assert untouched["defaultStyle"] is None

    # Unticking Default on the default style clears it.
    cleared = _eval(
        f"VS.savePlan({json.dumps(styles)}, 0, "
        "{name: 'Professional', instruction: 'Be calm.', in_menu: true}, "
        "'Professional', false)"
    )
    assert cleared["defaultStyle"] == ""


# -- the contract the Voice page (voice.js) loads ---------------------------


def test_exposes_the_global_loader_and_its_read_only_gate() -> None:
    body = _JS.read_text(encoding="utf-8")
    assert "window.loadVoiceStyles = function (container) {" in body
    assert "window.TurnstoneVoiceStyles = {" in body
    assert 'hasPermission("admin.settings")' in body
    assert "window.voiceSettingsRefresh" in body
