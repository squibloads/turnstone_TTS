"""Voice-persona memory: the interactive scopes, never ``coordinator``.

A voice session is a coordinator created with the ``voice`` persona, but it is
for spitballing and setting work up for an orchestrator rather than holding a
private orchestration memory.  It therefore uses the same scope envelope as an
interactive session -- ``global``, ``workstream``, ``user`` and the attached
``project`` -- and must never reach the ``coordinator`` scope.  The separate
``voice`` scope is retired: naming it is an unknown-scope error, not an
unavailable-but-real namespace.
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


def _attach_writable_project(monkeypatch: pytest.MonkeyPatch, project_id: str = "p1") -> None:
    monkeypatch.setattr(
        auth,
        "resolve_project_access",
        lambda *_a, **_k: auth.ProjectAccess(True, True, "P", "active"),
    )
    storage = get_storage()
    if storage.get_project(project_id) is None:
        storage.create_project(project_id, "P", "u1")


def _action_args(action: str, name: str, scope: str) -> dict[str, Any]:
    args: dict[str, Any] = {"action": action, "name": name, "scope": scope}
    if action == "save":
        args.update(content="the kettle is on", description="Kettle state")
    elif action == "search":
        args["query"] = "kettle"
    return args


class TestVoiceScopeDefaults:
    """An unscoped voice save uses the interactive inheritance rule."""

    def test_unscoped_save_lands_in_global(self) -> None:
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

        assert "scope=global" in str(msg)
        assert get_structured_memory_by_name("kettle_state", "global", "") is not None
        assert get_structured_memory_by_name("kettle_state", "coordinator", "u1") is None

    def test_unscoped_save_lands_in_project_when_attached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _attach_writable_project(monkeypatch)
        session = _voice_session(project_id="p1")
        item = session._prepare_memory(
            "cid",
            {
                "action": "save",
                "name": "project_kettle",
                "content": "the kettle is on",
                "description": "Kettle state",
            },
        )
        assert "error" not in item

        _, msg = _execute_prepared_tool(session, item)

        assert "scope=project" in str(msg)
        assert get_structured_memory_by_name("project_kettle", "project", "p1") is not None

    def test_voice_without_project_inherits_global(self) -> None:
        session = _voice_session()

        assert session._inherited_memory_scope() == "global"
        assert session._resolve_scope_id("global") == ""

    def test_voice_with_project_inherits_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _attach_writable_project(monkeypatch)
        session = _voice_session(project_id="p1")

        assert session._inherited_memory_scope() == "project"
        assert session._resolve_scope_id("project") == "p1"


class TestVoiceScopeActions:
    """Every interactive scope is usable for every memory action."""

    @pytest.mark.parametrize(
        ("scope", "scope_id"),
        [
            ("global", ""),
            ("workstream", "voice-ws"),
            ("user", "u1"),
            ("project", "p1"),
        ],
    )
    def test_voice_can_save_get_search_list_delete(
        self,
        scope: str,
        scope_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session_kwargs: dict[str, Any] = {}
        if scope == "project":
            _attach_writable_project(monkeypatch)
            session_kwargs["project_id"] = "p1"
        session = make_registered_session(
            ws_id="voice-ws",
            user_id="u1",
            kind=WorkstreamKind.COORDINATOR,
            persona_snapshot=_voice_snapshot(),
            **session_kwargs,
        )
        name = f"voice_{scope}_fact"

        save = session._prepare_memory("save-cid", _action_args("save", name, scope))
        assert "error" not in save
        _, save_msg = _execute_prepared_tool(session, save)
        assert f"scope={scope}" in str(save_msg)
        assert get_structured_memory_by_name(name, scope, scope_id) is not None

        get = session._prepare_memory("get-cid", _action_args("get", name, scope))
        assert "error" not in get
        _, get_msg = _execute_prepared_tool(session, get)
        assert f"[general:{scope}] {name}" in str(get_msg)

        search = session._prepare_memory("search-cid", _action_args("search", name, scope))
        assert "error" not in search
        _, search_msg = _execute_prepared_tool(session, search)
        assert f"[general:{scope}] {name}" in str(search_msg)

        listing = session._prepare_memory("list-cid", _action_args("list", name, scope))
        assert "error" not in listing
        _, list_msg = _execute_prepared_tool(session, listing)
        assert f"[general:{scope}] {name}" in str(list_msg)

        delete = session._prepare_memory("delete-cid", _action_args("delete", name, scope))
        assert "error" not in delete
        _, delete_msg = _execute_prepared_tool(session, delete)
        assert "Deleted memory" in str(delete_msg)
        assert get_structured_memory_by_name(name, scope, scope_id) is None


class TestVoiceScopeValidation:
    """The coordinator namespace is closed to voice; the voice scope is gone."""

    @pytest.mark.parametrize("action", ["save", "get", "delete", "search", "list"])
    def test_voice_rejects_coordinator_on_every_action(self, action: str) -> None:
        session = _voice_session()

        out = session._prepare_memory("cid", _action_args(action, "k", "coordinator"))

        assert "error" in out
        assert "scope 'coordinator' is unavailable" in out["error"]

    @pytest.mark.parametrize("action", ["save", "get", "delete", "search", "list"])
    def test_voice_scope_is_unknown_on_every_action(self, action: str) -> None:
        session = _voice_session()

        out = session._prepare_memory("cid", _action_args(action, "k", "voice"))

        assert "error" in out
        assert "invalid scope 'voice'" in out["error"]

    @pytest.mark.parametrize("factory", [_coordinator_session, _interactive_session])
    @pytest.mark.parametrize("action", ["save", "get", "delete", "search", "list"])
    def test_voice_scope_is_unknown_for_other_kinds(self, factory: Any, action: str) -> None:
        session = factory()

        out = session._prepare_memory("cid", _action_args(action, "k", "voice"))

        assert "error" in out
        assert "invalid scope 'voice'" in out["error"]

    def test_plain_coordinator_keeps_coordinator_and_project(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _attach_writable_project(monkeypatch)
        session = _coordinator_session(project_id="p1")

        coord = session._prepare_memory("cid", _action_args("save", "c", "coordinator"))
        assert "error" not in coord
        assert coord["scope"] == "coordinator"
        assert coord["scope_id"] == "u1"

        project = session._prepare_memory("cid", _action_args("save", "p", "project"))
        assert "error" not in project
        assert project["scope"] == "project"
        assert project["scope_id"] == "p1"

    def test_plain_coordinator_still_rejects_interactive_scopes(self) -> None:
        session = _coordinator_session()

        for scope in ("global", "workstream", "user"):
            out = session._prepare_memory("cid", _action_args("save", "k", scope))
            assert "error" in out
            assert f"scope '{scope}' is unavailable" in out["error"]

    def test_voice_never_resolves_the_coordinator_namespace(self) -> None:
        # Defence in depth: the validator is the gate, but an empty key means a
        # caller that skipped it cannot alias the coordinator namespace either.
        session = _voice_session()

        assert session._resolve_scope_id("coordinator") == ""
        assert session._coordinator_scope_id() == ""


class TestVoiceVisibility:
    """The live and implicit lanes use the interactive envelope."""

    def test_voice_scopes_are_the_interactive_envelope(self) -> None:
        session = _voice_session()

        assert session._visible_scopes() == [
            ("global", ""),
            ("workstream", "voice-ws"),
            ("user", "u1"),
        ]

    def test_voice_scopes_add_the_attached_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _attach_writable_project(monkeypatch)
        session = _voice_session(project_id="p1")

        assert session._visible_scopes() == [
            ("global", ""),
            ("workstream", "voice-ws"),
            ("user", "u1"),
            ("project", "p1"),
        ]

    def test_voice_index_matches_an_interactive_session_for_shared_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _attach_writable_project(monkeypatch)
        storage = get_storage()
        storage.register_workstream(
            "voice-ws", user_id="u1", kind=WorkstreamKind.COORDINATOR, persona="voice"
        )
        storage.register_workstream("ic-ws", user_id="u1")
        save_structured_memory(
            "global_fact", "shared", description="Global row", scope="global", scope_id=""
        )
        save_structured_memory(
            "user_fact", "shared", description="User row", scope="user", scope_id="u1"
        )
        save_structured_memory(
            "project_fact", "shared", description="Project row", scope="project", scope_id="p1"
        )
        save_structured_memory(
            "voice_ws_fact",
            "shared",
            description="Voice workstream row",
            scope="workstream",
            scope_id="voice-ws",
        )
        save_structured_memory(
            "ic_ws_fact",
            "shared",
            description="Interactive workstream row",
            scope="workstream",
            scope_id="ic-ws",
        )

        voice_text = _index_text(_voice_session(project_id="p1"))
        interactive_text = _index_text(_interactive_session(project_id="p1"))

        for name in ("global_fact", "user_fact", "project_fact"):
            assert name in voice_text
            assert name in interactive_text
        # Workstream scope follows the session, exactly as it does for an
        # interactive session: each sees its own and not the other's.
        assert "voice_ws_fact" in voice_text
        assert "voice_ws_fact" not in interactive_text
        assert "ic_ws_fact" in interactive_text
        assert "ic_ws_fact" not in voice_text

    def test_voice_index_excludes_coordinator_and_retired_voice_rows(self) -> None:
        save_structured_memory(
            "global_pref", "shared", description="Global row", scope="global", scope_id=""
        )
        save_structured_memory(
            "coord_pref",
            "private",
            description="Coordinator row",
            scope="coordinator",
            scope_id="u1",
        )
        save_structured_memory(
            "legacy_voice_pref",
            "private",
            description="Retired voice row",
            scope="voice",
            scope_id="u1",
        )

        content = _index_text(_voice_session())

        assert "[global/general] global_pref" in content
        assert "coord_pref" not in content
        assert "legacy_voice_pref" not in content
        assert "[coordinator/" not in content
        assert "[voice/" not in content

    def test_unscoped_search_and_list_never_include_coordinator_rows(self) -> None:
        save_structured_memory(
            "global_kettle", "the kettle is on", description="Global row", scope="global"
        )
        save_structured_memory(
            "coord_kettle",
            "the kettle is hidden",
            description="Coordinator row",
            scope="coordinator",
            scope_id="u1",
        )
        session = _voice_session()

        search = session._prepare_memory("search-cid", {"action": "search", "query": "kettle"})
        assert "error" not in search
        _, search_msg = _execute_prepared_tool(session, search)
        assert "global_kettle" in str(search_msg)
        assert "coord_kettle" not in str(search_msg)

        listing = session._prepare_memory("list-cid", {"action": "list"})
        assert "error" not in listing
        _, list_msg = _execute_prepared_tool(session, listing)
        assert "global_kettle" in str(list_msg)
        assert "coord_kettle" not in str(list_msg)

    def test_plain_coordinator_visibility_is_unchanged(self) -> None:
        session = _coordinator_session()

        assert session._visible_scopes() == [("coordinator", "u1")]

    def test_interactive_visibility_is_unchanged(self) -> None:
        session = _interactive_session()

        assert session._visible_scopes() == [
            ("global", ""),
            ("workstream", "ic-ws"),
            ("user", "u1"),
        ]


class TestVoicePointerIsolation:
    """Live pointers come from the same envelope as the immutable index."""

    def test_voice_pointer_ignores_coordinator_rows(self) -> None:
        save_structured_memory(
            "global_coffee",
            "oat milk",
            description="Coffee preference for interactive and voice sessions",
            scope="global",
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

        assert "global_coffee" in pointer
        assert "coord_coffee" not in pointer


class TestVoiceSnapshotCapture:
    """The first-turn snapshot captures the interactive envelope, not voice."""

    def test_persona_is_applied_before_the_snapshot_is_taken(self) -> None:
        session = make_registered_session(
            ws_id="voice-order",
            user_id="u1",
            kind=WorkstreamKind.COORDINATOR,
            persona_snapshot=_voice_snapshot(),
        )

        assert session._persona_name == "voice"
        assert session._memory_index_snapshot is None
        assert session._visible_scopes() == [
            ("global", ""),
            ("workstream", "voice-order"),
            ("user", "u1"),
        ]

    def test_voice_snapshot_captures_the_interactive_envelope(self) -> None:
        get_storage().register_workstream(
            "voice-snapshot", user_id="u1", kind=WorkstreamKind.COORDINATOR, persona="voice"
        )
        save_structured_memory(
            "global_pref", "brief", description="Global row", scope="global", scope_id=""
        )
        save_structured_memory(
            "user_pref", "brief", description="User row", scope="user", scope_id="u1"
        )
        save_structured_memory(
            "coord_pref",
            "research everything",
            description="Coordinator research preference",
            scope="coordinator",
            scope_id="u1",
        )
        save_structured_memory(
            "legacy_voice_pref",
            "short replies",
            description="Retired voice preference",
            scope="voice",
            scope_id="u1",
        )
        save_structured_memory(
            "voice_ws_pref",
            "brief",
            description="Voice workstream row",
            scope="workstream",
            scope_id="voice-snapshot",
        )
        session = make_registered_session(
            ws_id="voice-snapshot",
            user_id="u1",
            kind=WorkstreamKind.COORDINATOR,
            persona_snapshot=_voice_snapshot(),
        )

        snapshot = _admit_first_index(session, "u1")

        assert "[global/general] global_pref" in snapshot["content"]
        assert "[user/general] user_pref" in snapshot["content"]
        assert "[workstream/general] voice_ws_pref" in snapshot["content"]
        assert "coord_pref" not in snapshot["content"]
        assert "legacy_voice_pref" not in snapshot["content"]
        assert 'entries="3"' in snapshot["content"]

    def test_plain_coordinator_snapshot_is_unchanged(self) -> None:
        save_structured_memory(
            "global_pref", "brief", description="Global row", scope="global", scope_id=""
        )
        save_structured_memory(
            "user_pref", "brief", description="User row", scope="user", scope_id="u1"
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
        assert "global_pref" not in snapshot["content"]
        assert "user_pref" not in snapshot["content"]
        assert 'entries="1"' in snapshot["content"]


class TestVoiceChildIsolation:
    """A child of a voice coordinator cannot reach the coordinator scope."""

    def test_child_cannot_read_or_plant_coordinator_rows(self) -> None:
        save_structured_memory(
            "coord_secret",
            "orchestration only",
            description="Coordinator row",
            scope="coordinator",
            scope_id="u1",
        )
        child = _interactive_session(ws_id="voice-child", parent_ws_id="voice-ws")

        assert child._visible_scopes() == [
            ("global", ""),
            ("workstream", "voice-child"),
            ("user", "u1"),
        ]
        assert "coord_secret" not in _index_text(child)

        get = child._prepare_memory(
            "cid", {"action": "get", "name": "coord_secret", "scope": "coordinator"}
        )
        assert "error" in get

        save = child._prepare_memory(
            "cid",
            {
                "action": "save",
                "name": "planted",
                "content": "planted by a child",
                "description": "Child attempt",
                "scope": "coordinator",
            },
        )
        assert "error" in save
        assert get_structured_memory_by_name("planted", "coordinator", "u1") is None


class TestVoiceMemoryToolSchema:
    """The voice variant offers the interactive scopes and no coordinator."""

    def test_voice_memory_tool_offers_the_interactive_scopes(self) -> None:
        session = _voice_session()

        assert _memory_scope_enum(session) == ["global", "workstream", "user", "project"]

    def test_voice_memory_description_says_it_shares_interactive_scopes(self) -> None:
        session = _voice_session()

        scope_desc = _memory_schema(session)["scope"]["description"]
        assert "same scopes as an interactive session" in scope_desc
        assert "never" in scope_desc.lower()
        assert "coordinator" in scope_desc.lower()

    def test_retired_voice_scope_is_not_advertised_anywhere(self) -> None:
        from turnstone.core.tools import TOOLS

        memory = next(t for t in TOOLS if t["function"]["name"] == "memory")
        scope = memory["function"]["parameters"]["properties"]["scope"]
        assert "voice" not in scope["enum"]
        assert "coordinator" in scope["enum"]

    def test_plain_coordinator_tool_still_offers_coordinator(self) -> None:
        session = _coordinator_session()

        assert _memory_scope_enum(session) == ["coordinator", "project"]

    def test_interactive_tool_still_offers_its_own_scopes(self) -> None:
        session = _interactive_session()

        assert _memory_scope_enum(session) == ["global", "workstream", "user", "project"]
