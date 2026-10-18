"""Documentation and admin-view completeness for the voice persona.

A voice session is a coordinator created with the ``voice`` persona.  The
settings registry points both voice-persona settings at
``docs/voice.md#voice-persona``, so that heading is a public anchor: renaming
it turns every "learn more" link in Admin › Settings into a dead link.  The
admin Memories scope filter is a fixed list in the console HTML, so it has to
offer the new scope as well; the console admin API itself accepts any scope
string and needs no matching list.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_voice_docs import heading_slugs

_ROOT = Path(__file__).resolve().parents[1]
_VOICE_DOC = _ROOT / "docs" / "voice.md"
_MEMORY_DOC = _ROOT / "docs" / "memory.md"
_CONSOLE_INDEX = _ROOT / "turnstone" / "console" / "static" / "index.html"


def _voice_text() -> str:
    assert _VOICE_DOC.is_file(), f"missing {_VOICE_DOC}"
    return _VOICE_DOC.read_text(encoding="utf-8")


def test_voice_doc_publishes_the_persona_anchor() -> None:
    """The registry's reference_urls end at ``#voice-persona``."""
    assert "voice-persona" in heading_slugs(_voice_text())


def test_voice_doc_documents_the_persona_subsections() -> None:
    slugs = heading_slugs(_voice_text())
    assert "voice-memories" in slugs
    assert "starting-agents" in slugs


def test_voice_doc_names_both_persona_settings() -> None:
    text = _voice_text()
    for key in (
        "coordinator.voice_max_agents_per_turn",
        "coordinator.voice_child_model",
    ):
        assert key in text, f"docs/voice.md must name {key}"


def test_memory_doc_lists_the_voice_scope_and_links_to_the_voice_doc() -> None:
    text = _MEMORY_DOC.read_text(encoding="utf-8")
    assert re.search(r"^\|\s*`voice`\s*\|", text, re.MULTILINE), (
        "docs/memory.md's scope table must list the voice scope"
    )
    assert re.search(r"\]\(voice\.md#voice-persona\)", text), (
        "docs/memory.md must point at the voice persona section"
    )


def test_admin_memories_scope_filter_offers_voice() -> None:
    """The Memories tab's scope select is a fixed list in index.html."""
    html = _CONSOLE_INDEX.read_text(encoding="utf-8")
    select = re.search(
        r'<select[^>]*id="mem-filter-scope".*?</select>',
        html,
        re.DOTALL,
    )
    assert select, "positive control: the admin Memories scope filter exists"
    assert re.search(r'<option value="voice">Voice</option>', select.group(0)), (
        "the admin Memories scope filter must offer the voice scope"
    )
