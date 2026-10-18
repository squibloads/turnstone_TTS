"""Voice-persona memory isolation: scope, visibility and the tool surface.

A voice session is a coordinator created with the ``voice`` persona.  Spoken
asides are easy to mistake for requests, so its memories live in a namespace
of their own (``voice``), keyed by the acting user: a voice conversation
reuses neither the text coordinator's orchestration memory nor its habits.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tests._session_helpers import make_registered_session
from turnstone.core import auth
from turnstone.core.memory import get_structured_memory_by_name, save_structured_memory
from turnstone.core.memory_index import render_memory_index
from turnstone.core.personas import PersonaSnapshot
from turnstone.core.session import ChatSession
from turnstone.core.storage._registry import get_storage
from turnstone.core.workstream import WorkstreamKind


@pytest.fixture(autouse=True)
def _isolated_storage(tmp_db: str) -> None:
    """Keep constructor-only session tests off the repository database."""


def _session(**kwargs: Any) -> ChatSession:
    """Construct a ChatSession with minimal mocked plumbing (no UI calls here)."""
    defaults: dict[str, Any] = dict(
        client=MagicMock(),
        model="test-model",
        ui=MagicMock(),
        instructions=None,
        temperature=0.5,
        max_tokens=4096,
        tool_timeout=30,
    )
    defaults.update(kwargs)
    return ChatSession(**defaults)


def _voice_snapshot() -> PersonaSnapshot:
    return PersonaSnapshot(name="voice", prompt="", tools=None, mcp=True, memory=True)


def _voice_session(**kwargs: Any) -> ChatSession:
    kwargs.setdefault("user_id", "u1")
    kwargs.setdefault("ws_id", "voice-ws")
    kwargs.setdefault("kind", WorkstreamKind.COORDINATOR)
    kwargs.setdefault("persona_snapshot", _voice_snapshot())
    return _session(**kwargs)


def _coordinator_session(**kwargs: Any) -> ChatSession:
    kwargs.setdefault("user_id", "u1")
    kwargs.setdefault("ws_id", "coord-ws")
    kwargs.setdefault("kind", WorkstreamKind.COORDINATOR)
    return _session(**kwargs)


def _interactive_session(**kwargs: Any) -> ChatSession:
    kwargs.setdefault("user_id", "u1")
    kwargs.setdefault("ws_id", "ic-ws")
    return _session(**kwargs)


def _admit_first_index(session: ChatSession, principal_id: str) -> dict[str, Any]:
    """Drive the first-provider-request admission exactly as ``send`` does."""
    generation = session._claim_generation(principal_id=principal_id)
    session._memory_index_admission_generation = generation
    session._admit_memory_index_request(
        session._primary_lane(),
        my_generation=generation,
        principal_id=principal_id,
    )
    snapshot = get_storage().get_memory_index_snapshot(session.ws_id)
    assert snapshot is not None
    return snapshot


def _save_args(scope: str) -> dict[str, Any]:
    return {
        "action": "save",
        "scope": scope,
        "name": "spoken_fact",
        "content": "the kettle is on",
        "description": "Kettle state",
    }


def _execute_prepared_tool(
    session: ChatSession,
    item: dict[str, Any],
) -> tuple[str, str | list[dict[str, Any]]]:
    item.setdefault("_principal_id", session._tool_prepare_principal_id())
    result: tuple[str, str | list[dict[str, Any]]] = item["execute"](item)
    return result


def _memory_schema(session: ChatSession) -> dict[str, Any]:
    memory = next(t for t in session._tools if t["function"]["name"] == "memory")
    properties: dict[str, Any] = memory["function"]["parameters"]["properties"]
    return properties


def _memory_scope_enum(session: ChatSession) -> list[str]:
    scope: list[str] = _memory_schema(session)["scope"]["enum"]
    return scope


def _index_text(session: ChatSession) -> str:
    """Render the live index envelope for this session's visible scopes."""
    rows = get_storage().list_visible_memory_index_entries(
        session._visible_scopes(),
        acting_principal_id=session._memory_principal_id(),
    )
    return render_memory_index(rows).content


def _attach_writable_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auth,
        "resolve_project_access",
        lambda *_a, **_k: auth.ProjectAccess(True, True, "P", "active"),
    )


class TestVoiceScopeDefaults:
    """An unscoped voice save lands in the voice namespace, not the coord one."""

    def test_unscoped_save_lands_in_voice_namespace(self) -> None:
        session = _voice_session()
        item = session._prepare_memory(
            "cid",
            {
                "action": "save",
                "name": "kettle_state",
                "content": "the kettle is on",
                "description": "Kettle state",
            },
        )
        assert "error" not in item

        _, msg = _execute_prepared_tool(session, item)

        assert "scope=voice" in str(msg)
        assert get_structured_memory_by_name("kettle_state", "voice", "u1") is not None
        assert get_structured_memory_by_name("kettle_state", "coordinator", "u1") is None

    def test_voice_inherits_project_when_attached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _attach_writable_project(monkeypatch)
        session = _voice_session(project_id="p1")

        assert session._inherited_memory_scope() == "project"
        assert session._resolve_scope_id("voice") == "u1"

    def test_voice_without_project_inherits_voice(self) -> None:
        session = _voice_session()

        assert session._inherited_memory_scope() == "voice"
        assert session._resolve_scope_id("voice") == "u1"


class TestVoiceScopeValidation:
    """Voice scopes are exclusive to the voice persona in both directions."""

    @pytest.mark.parametrize("scope", ["coordinator", "global", "workstream", "user"])
    def test_voice_rejects_non_voice_scopes(self, scope: str) -> None:
        session = _voice_session()

        out = session._prepare_memory("cid", _save_args(scope))

        assert "error" in out

    def test_voice_accepts_voice_and_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _attach_writable_project(monkeypatch)
        storage = get_storage()
        if storage.get_project("p1") is None:
            storage.create_project("p1", "P", "u1")
        session = _voice_session(project_id="p1")

        assert "error" not in session._prepare_memory("cid", _save_args("voice"))
        assert "error" not in session._prepare_memory("cid", _save_args("project"))

    def test_plain_coordinator_rejects_voice(self) -> None:
        session = _coordinator_session()

        out = session._prepare_memory("cid", _save_args("voice"))

        assert "error" in out
        assert "unavailable to this workstream kind" in out["error"]

    def test_interactive_rejects_voice(self) -> None:
        session = _interactive_session()

        out = session._prepare_memory("cid", _save_args("voice"))

        assert "error" in out
        assert "unavailable to this workstream kind" in out["error"]

    def test_voice_rejects_explicit_voice_get_for_other_kinds(self) -> None:
        for session in (_coordinator_session(), _interactive_session()):
            out = session._prepare_memory("cid", {"action": "get", "name": "k", "scope": "voice"})
            assert "error" in out


class TestVoiceVisibility:
    """The coordinator kind gates on the persona before building its envelope."""

    def test_voice_scopes_are_voice_and_project_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _attach_writable_project(monkeypatch)
        session = _voice_session(project_id="p1")

        assert session._visible_scopes() == [("voice", "u1"), ("project", "p1")]

    def test_voice_without_project_has_no_coordinator_scope(self) -> None:
        session = _voice_session()

        assert session._visible_scopes() == [("voice", "u1")]

    def test_voice_index_includes_voice_rows_and_excludes_coordinator_rows(self) -> None:
        save_structured_memory(
            "voice_pref",
            "short replies",
            description="Voice reply length preference",
            scope="voice",
            scope_id="u1",
        )
        save_structured_memory(
            "coord_pref",
            "research everything",
            description="Coordinator research preference",
            scope="coordinator",
            scope_id="u1",
        )

        content = _index_text(_voice_session())

        assert "[voice/general] voice_pref" in content
        assert "[coordinator/general]" not in content
        assert "coord_pref" not in content

    def test_plain_coordinator_index_excludes_voice_rows(self) -> None:
        save_structured_memory(
            "voice_pref",
            "short replies",
            description="Voice reply length preference",
            scope="voice",
            scope_id="u1",
        )
        save_structured_memory(
            "coord_pref",
            "research everything",
            description="Coordinator research preference",
            scope="coordinator",
            scope_id="u1",
        )

        content = _index_text(_coordinator_session())

        assert "[coordinator/general] coord_pref" in content
        assert "[voice/general]" not in content
        assert "voice_pref" not in content

    def test_interactive_index_excludes_voice_rows(self) -> None:
        save_structured_memory(
            "voice_pref",
            "short replies",
            description="Voice reply length preference",
            scope="voice",
            scope_id="u1",
        )

        content = _index_text(_interactive_session())

        assert "voice_pref" not in content

    def test_voice_index_orders_voice_before_project(self) -> None:
        rows = [
            {
                "memory_id": "2",
                "name": "project_fact",
                "description": "Project hook",
                "type": "general",
                "scope": "project",
                "scope_id": "p1",
            },
            {
                "memory_id": "1",
                "name": "voice_fact",
                "description": "Voice hook",
                "type": "general",
                "scope": "voice",
                "scope_id": "u1",
            },
        ]

        content = render_memory_index(rows).content

        assert content.index("[voice/general]") < content.index("[project/general]")


class TestVoicePointerIsolation:
    """Live pointers come from the same envelope as the immutable index."""

    def test_voice_pointer_ignores_coordinator_rows(self) -> None:
        save_structured_memory(
            "voice_coffee",
            "oat milk",
            description="Coffee preference for voice sessions",
            scope="voice",
            scope_id="u1",
        )
        save_structured_memory(
            "coord_coffee",
            "black",
            description="Coffee preference for text coordinators",
            scope="coordinator",
            scope_id="u1",
        )
        session = _voice_session()

        pointer = session._plan_memory_pointer("coffee preference", access=session._memory_access())

        assert "voice_coffee" in pointer
        assert "coord_coffee" not in pointer


class TestVoiceSnapshotCapture:
    """The durable row stores the kind, not the persona: the session must say.

    The first admitted turn freezes the private half of the envelope into
    ``memory_index_snapshots``, and that snapshot is what every later turn of
    the workstream replays.  Capturing the coordinator namespace here would
    leak text-coordinator memories into a voice session for its whole life,
    so the capture path — not only the live visibility helper — has to know.
    """

    def test_persona_is_applied_before_the_snapshot_is_taken(self) -> None:
        # The persona stamp is construction-time and the index is captured at
        # the first admitted turn, so the capture always sees it.  Assert the
        # ordering directly rather than trusting it: nothing durable exists
        # yet, and the live envelope is already the voice one.
        session = make_registered_session(
            ws_id="voice-order",
            user_id="u1",
            kind=WorkstreamKind.COORDINATOR,
            persona_snapshot=_voice_snapshot(),
        )

        assert session._persona_name == "voice"
        assert session._memory_index_snapshot is None
        assert session._visible_scopes() == [("voice", "u1")]

    def test_voice_snapshot_captures_voice_not_coordinator_rows(self) -> None:
        save_structured_memory(
            "voice_pref",
            "short replies",
            description="Voice reply length preference",
            scope="voice",
            scope_id="u1",
        )
        save_structured_memory(
            "coord_pref",
            "research everything",
            description="Coordinator research preference",
            scope="coordinator",
            scope_id="u1",
        )
        session = make_registered_session(
            ws_id="voice-snapshot",
            user_id="u1",
            kind=WorkstreamKind.COORDINATOR,
            persona_snapshot=_voice_snapshot(),
        )

        snapshot = _admit_first_index(session, "u1")

        assert "[voice/general] voice_pref" in snapshot["content"]
        assert "coord_pref" not in snapshot["content"]
        assert 'entries="1"' in snapshot["content"]

    def test_plain_coordinator_snapshot_still_captures_coordinator_rows(self) -> None:
        save_structured_memory(
            "voice_pref",
            "short replies",
            description="Voice reply length preference",
            scope="voice",
            scope_id="u1",
        )
        save_structured_memory(
            "coord_pref",
            "research everything",
            description="Coordinator research preference",
            scope="coordinator",
            scope_id="u1",
        )
        session = make_registered_session(
            ws_id="coord-snapshot",
            user_id="u1",
            kind=WorkstreamKind.COORDINATOR,
        )

        snapshot = _admit_first_index(session, "u1")

        assert "[coordinator/general] coord_pref" in snapshot["content"]
        assert "voice_pref" not in snapshot["content"]
        assert 'entries="1"' in snapshot["content"]


class TestVoiceMemoryToolSchema:
    """The voice variant must win over the coordinator variant, not merge with it."""

    def test_voice_memory_tool_offers_only_voice_and_project(self) -> None:
        session = _voice_session()

        assert _memory_scope_enum(session) == ["voice", "project"]

    def test_voice_memory_description_marks_voice_memories_as_separate(self) -> None:
        session = _voice_session()

        assert "separate from text coordinators" in _memory_schema(session)["scope"]["description"]

    def test_plain_coordinator_tool_still_offers_coordinator(self) -> None:
        session = _coordinator_session()

        assert _memory_scope_enum(session) == ["coordinator", "project"]

    def test_interactive_tool_still_offers_its_own_scopes(self) -> None:
        session = _interactive_session()

        assert _memory_scope_enum(session) == ["global", "workstream", "user", "project"]
