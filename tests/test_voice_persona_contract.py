"""Tests for the voice-persona contract in turnstone.core.voice_config.

A voice session is a coordinator created with the ``voice`` persona.  The
memory scope, the spawn caps and the child-model default all key off that one
name, so the name, the scope and the settings are pinned here before the
slices that consume them are built.
"""

from __future__ import annotations

import pytest

from turnstone.core.settings_registry import validate_key, validate_value
from turnstone.core.voice_config import (
    VOICE_DOCS_URL,
    VOICE_MEMORY_SCOPE,
    VOICE_PERSONA,
    is_voice_persona,
)


def test_names_are_fixed() -> None:
    assert VOICE_PERSONA == "voice"
    assert VOICE_MEMORY_SCOPE == "voice"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("voice", True),
        ("Voice", True),
        (" voice ", True),
        ("orchestrator", False),
        ("", False),
        (None, False),
        ("voice2", False),
    ],
)
def test_is_voice_persona(name: str | None, expected: bool) -> None:
    assert is_voice_persona(name) is expected


def test_max_agents_per_turn_setting() -> None:
    d = validate_key("coordinator.voice_max_agents_per_turn")
    assert (d.type, d.section, d.default) == ("int", "coordinator", 1)
    assert (d.min_value, d.max_value) == (0, 10)
    assert d.help and d.reference_url.startswith(VOICE_DOCS_URL + "#")
    with pytest.raises(ValueError):
        validate_value("coordinator.voice_max_agents_per_turn", "11")


def test_max_requested_agents_setting() -> None:
    d = validate_key("coordinator.voice_max_requested_agents")
    assert (d.type, d.section, d.default) == ("int", "coordinator", 4)
    assert (d.min_value, d.max_value) == (0, 10)
    assert d.help and d.reference_url.startswith(VOICE_DOCS_URL + "#")
    with pytest.raises(ValueError):
        validate_value("coordinator.voice_max_requested_agents", "11")


def test_child_model_setting() -> None:
    d = validate_key("coordinator.voice_child_model")
    assert (d.type, d.section, d.default) == ("str", "coordinator", "deepseek-flash-high")
    assert d.help and d.reference_url.startswith(VOICE_DOCS_URL + "#")
