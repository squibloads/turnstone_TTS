"""Slash commands in coordinator (and therefore voice) sessions.

A coordinator composer posts chat to ``POST .../send``.  ``POST
/v1/api/command`` is mounted only on the node server, and the coordinator
pane never called it, so a typed ``/model X`` used to arrive at the model as
an ordinary message.  These tests pin the two halves of the fix:

* the shared ``/send`` route refuses to treat a leading ``/`` as chat for a
  kind that routes commands (coordinators), and
* the console's path-keyed command route runs the same
  ``ChatSession.handle_command`` registry interactive sessions use.

Text that does not *begin* with ``/`` — a URL, a path inside a sentence —
stays a normal message.  The parse rule is deliberately the interactive
client's (``text.startsWith("/")``), not a new one.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import (
    _AuthMiddleware,
    _build_mgr_with_factory,
    _fake_registry,
    _FakeConfigStore,
)
from turnstone.console.server import _require_admin_coordinator
from turnstone.core.model_registry import ModelConfig, ModelRegistry
from turnstone.core.model_turn import resolve_model_binding
from turnstone.core.session import ChatSession, GenerationCancelled
from turnstone.core.session_routes import (
    SessionEndpointConfig,
    make_command_handler,
    make_send_handler,
)
from turnstone.core.workstream import WorkstreamKind
from turnstone.prompts import ClientType

_COORD_HEADERS = {"X-Test-User": "user-1", "X-Test-Perms": "admin.coordinator"}


class _RecordingSession:
    """A coordinator session double that records the two dispatch surfaces.

    ``SessionManager.create`` checks that the session writes with the slot's
    lease, so the double keeps the one the factory handed it.
    """

    def __init__(self, **kwargs: Any) -> None:
        self.sends: list[tuple[str, dict[str, Any]]] = []
        self.commands: list[tuple[str, str | None]] = []
        self._workstream_lease = kwargs.get("workstream_lease")

    def write_fence(self) -> Any:
        lease = self._workstream_lease
        return lease.fence if lease is not None else None

    def send(self, message: str, **kwargs: Any) -> None:
        self.sends.append((message, kwargs))

    def queue_message(self, message: str, **kwargs: Any) -> tuple[str, str, str]:
        raise AssertionError("a slash command must not enter the message queue")

    def handle_command(self, cmd_line: str, *, principal_id: str | None = None) -> bool:
        self.commands.append((cmd_line, principal_id))
        return False


class _CancellingSession(_RecordingSession):
    """A command stopped by the Stop button: ``GenerationCancelled`` is a
    ``BaseException``, so the dispatcher must catch it explicitly."""

    def handle_command(self, cmd_line: str, *, principal_id: str | None = None) -> bool:
        self.commands.append((cmd_line, principal_id))
        raise GenerationCancelled()


def _coord_endpoint_config() -> SessionEndpointConfig:
    return SessionEndpointConfig(
        permission_gate=_require_admin_coordinator,
        manager_lookup=lambda request: (request.app.state.coord_mgr, None),
        tenant_check=None,
        not_found_label="coordinator not found",
        audit_action_prefix="coordinator",
        supports_attachments=True,
        attachment_owner_resolver=lambda request, ws_id, mgr: ("user-1", None),
        routes_slash_commands=True,
        list_kind=WorkstreamKind.COORDINATOR,
    )


def _make_client(storage, *, mgr, config: SessionEndpointConfig) -> TestClient:
    app = Starlette(
        routes=[
            Route(
                "/v1/api/workstreams/{ws_id}/send",
                make_send_handler(config),
                methods=["POST"],
            ),
            Route(
                "/v1/api/workstreams/{ws_id}/command",
                make_command_handler(config),
                methods=["POST"],
            ),
        ],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.coord_mgr = mgr
    app.state.coord_adapter = mgr._adapter
    app.state.config_store = _FakeConfigStore({"coordinator.model_alias": "default"})
    app.state.coord_registry = _fake_registry()
    app.state.coord_registry_error = ""
    app.state.auth_storage = storage
    app.state.jwt_secret = "x" * 64
    return TestClient(app)


@pytest.fixture
def storage(tmp_path):
    from turnstone.core.storage import init_storage, reset_storage

    reset_storage()
    backend = init_storage("sqlite", path=str(tmp_path / "coord.db"), run_migrations=False)
    yield backend
    reset_storage()


def _create_coord(mgr) -> tuple[str, _RecordingSession]:
    ws = mgr.create(user_id="user-1", name="spoken")
    return ws.id, ws.session


# ---------------------------------------------------------------------------
# The bug: a slash command reaches the model as chat
# ---------------------------------------------------------------------------


def test_send_routes_a_slash_command_to_handle_command(storage) -> None:
    """``/model X`` in a coordinator must never become a model turn.

    Today the composer's ``POST .../send`` passes the text straight to
    ``session.send`` — the model is asked to switch itself and the session
    stays on its old model.  The send route must dispatch it through
    ``handle_command`` instead.
    """
    sessions: list[_RecordingSession] = []

    def factory(ui, model_alias=None, ws_id=None, **kwargs):  # type: ignore[no-untyped-def]
        sessions.append(_RecordingSession(**kwargs))
        return sessions[-1]

    mgr = _build_mgr_with_factory(storage, factory)
    client = _make_client(storage, mgr=mgr, config=_coord_endpoint_config())
    ws_id, session = _create_coord(mgr)

    resp = client.post(
        f"/v1/api/workstreams/{ws_id}/send",
        json={"message": "/model deepseek-flash-high"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200, resp.text

    ws = mgr.get(ws_id)
    assert ws is not None and ws.worker_thread is not None
    ws.worker_thread.join(timeout=5)
    assert not ws.worker_thread.is_alive()

    # The model turn is the bug: the text reached ChatSession.send.
    assert session.sends == []
    assert session.commands == [("/model deepseek-flash-high", "user-1")]


@pytest.mark.parametrize(
    "message",
    [
        "the /model command switches aliases",
        "https://example.com/model/deepseek-flash-high",
        "run /usr/bin/env python",
    ],
)
def test_send_keeps_ordinary_text_as_chat(storage, message: str) -> None:
    """Only a *leading* slash is a command — the interactive client's rule."""
    sessions: list[_RecordingSession] = []

    def factory(ui, model_alias=None, ws_id=None, **kwargs):  # type: ignore[no-untyped-def]
        sessions.append(_RecordingSession(**kwargs))
        return sessions[-1]

    mgr = _build_mgr_with_factory(storage, factory)
    client = _make_client(storage, mgr=mgr, config=_coord_endpoint_config())
    ws_id, session = _create_coord(mgr)

    resp = client.post(
        f"/v1/api/workstreams/{ws_id}/send",
        json={"message": message},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200, resp.text

    ws = mgr.get(ws_id)
    assert ws is not None and ws.worker_thread is not None
    ws.worker_thread.join(timeout=5)
    assert not ws.worker_thread.is_alive()

    assert session.commands == []
    assert [sent for sent, _kwargs in session.sends] == [message]


def test_send_keeps_slash_text_as_chat_when_the_kind_has_its_own_command_route(
    storage,
) -> None:
    """The guard is opt-in per kind. Interactive sessions already post commands
    to the node's dedicated route, so an interactive /send keeps its old
    behaviour and a message beginning with "/" is still chat there."""
    from dataclasses import replace

    sessions: list[_RecordingSession] = []

    def factory(ui, model_alias=None, ws_id=None, **kwargs):  # type: ignore[no-untyped-def]
        sessions.append(_RecordingSession(**kwargs))
        return sessions[-1]

    config = replace(_coord_endpoint_config(), routes_slash_commands=False)
    mgr = _build_mgr_with_factory(storage, factory)
    client = _make_client(storage, mgr=mgr, config=config)
    ws_id, session = _create_coord(mgr)

    resp = client.post(
        f"/v1/api/workstreams/{ws_id}/send",
        json={"message": "/model alt"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    ws = mgr.get(ws_id)
    assert ws is not None and ws.worker_thread is not None
    ws.worker_thread.join(timeout=5)
    assert session.commands == []
    assert [sent for sent, _kwargs in session.sends] == ["/model alt"]


def test_send_treats_a_lone_slash_like_the_interactive_client(storage) -> None:
    """The rule is the interactive client's ``startsWith("/")``: "/ " is a
    command attempt (answered visibly by the registry), never chat."""
    sessions: list[_RecordingSession] = []

    def factory(ui, model_alias=None, ws_id=None, **kwargs):  # type: ignore[no-untyped-def]
        sessions.append(_RecordingSession(**kwargs))
        return sessions[-1]

    mgr = _build_mgr_with_factory(storage, factory)
    client = _make_client(storage, mgr=mgr, config=_coord_endpoint_config())
    ws_id, session = _create_coord(mgr)

    resp = client.post(
        f"/v1/api/workstreams/{ws_id}/send",
        json={"message": "/ model"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    ws = mgr.get(ws_id)
    assert ws is not None and ws.worker_thread is not None
    ws.worker_thread.join(timeout=5)
    assert session.sends == []
    assert session.commands == [("/ model", "user-1")]


# ---------------------------------------------------------------------------
# The console's path-keyed command route
# ---------------------------------------------------------------------------


def _events(listener) -> list[dict[str, Any]]:
    import queue as queue_mod

    out: list[dict[str, Any]] = []
    while True:
        try:
            out.append(listener.get_nowait())
        except queue_mod.Empty:
            return out


def _real_registry() -> ModelRegistry:
    return ModelRegistry(
        models={
            "default": ModelConfig(
                "default", "https://example.invalid/v1", "test", "default-model"
            ),
            "alt": ModelConfig(
                "alt",
                "https://example.invalid/v1",
                "test",
                "alt-model",
                context_window=64000,
            ),
        },
        default="default",
    )


def _real_factory(registry: ModelRegistry, config_store: _FakeConfigStore):
    def factory(ui, model_alias=None, ws_id=None, **kwargs):  # type: ignore[no-untyped-def]
        alias = model_alias or registry.default
        binding = resolve_model_binding(registry, alias)
        return ChatSession(
            client=binding.lane.client,
            model=binding.lane.model,
            ui=ui,
            instructions=None,
            temperature=0.5,
            max_tokens=4096,
            tool_timeout=30,
            registry=registry,
            model_alias=alias,
            registry_generation=binding.registry_generation,
            context_window=binding.config.context_window,
            kind=WorkstreamKind.COORDINATOR,
            user_id="user-1",
            ws_id=ws_id,
            config_store=config_store,
            client_type=ClientType.WEB,
            model_binding=binding,
            workstream_lease=kwargs.get("workstream_lease"),
        )

    return factory


def test_command_route_switches_the_model_and_persists_it(storage) -> None:
    """``/model <alias>`` applies to the live coordinator session, survives a
    reload (workstream config), and confirms itself in the pane."""
    from turnstone.core.memory import load_workstream_config

    registry = _real_registry()
    mgr = _build_mgr_with_factory(storage, _real_factory(registry, _FakeConfigStore({})))
    client = _make_client(storage, mgr=mgr, config=_coord_endpoint_config())
    ws = mgr.create(user_id="user-1", name="spoken")
    listener = ws.ui._register_listener()

    resp = client.post(
        f"/v1/api/workstreams/{ws.id}/command",
        json={"command": "/model alt"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok"}
    assert ws.worker_thread is not None
    ws.worker_thread.join(timeout=5)
    assert not ws.worker_thread.is_alive()

    assert ws.session.model_alias == "alt"
    assert ws.session.model == "alt-model"
    assert load_workstream_config(ws.id)["model_alias"] == "alt"
    infos = [ev for ev in _events(listener) if ev.get("type") == "info"]
    assert any("Switched to" in ev.get("message", "") and "alt" in ev["message"] for ev in infos)


def test_command_route_unknown_alias_is_a_clear_error(storage) -> None:
    registry = _real_registry()
    mgr = _build_mgr_with_factory(storage, _real_factory(registry, _FakeConfigStore({})))
    client = _make_client(storage, mgr=mgr, config=_coord_endpoint_config())
    ws = mgr.create(user_id="user-1", name="spoken")
    listener = ws.ui._register_listener()

    resp = client.post(
        f"/v1/api/workstreams/{ws.id}/command",
        json={"command": "/model no-such-model"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert ws.worker_thread is not None
    ws.worker_thread.join(timeout=5)

    # The switch is a no-op and the binding is untouched — but the pane says
    # so instead of silently keeping the old model.
    assert ws.session.model_alias == "default"
    infos = [ev for ev in _events(listener) if ev.get("type") == "info"]
    assert any(
        "Unknown model alias: no-such-model" in ev.get("message", "")
        and "Available: default, alt" in ev["message"]
        for ev in infos
    )


def test_command_route_survives_a_cancelled_command(storage) -> None:
    """``GenerationCancelled`` subclasses ``BaseException``: an uncaught one
    would kill the worker thread instead of answering the caller."""
    sessions: list[_RecordingSession] = []

    def factory(ui, model_alias=None, ws_id=None, **kwargs):  # type: ignore[no-untyped-def]
        sessions.append(_CancellingSession(**kwargs))
        return sessions[-1]

    mgr = _build_mgr_with_factory(storage, factory)
    client = _make_client(storage, mgr=mgr, config=_coord_endpoint_config())
    ws = mgr.create(user_id="user-1", name="spoken")
    listener = ws.ui._register_listener()

    resp = client.post(
        f"/v1/api/workstreams/{ws.id}/command",
        json={"command": "/model alt"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok"}
    assert ws.worker_thread is not None
    ws.worker_thread.join(timeout=5)
    assert not ws.worker_thread.is_alive()
    infos = [ev for ev in _events(listener) if ev.get("type") == "info"]
    assert any(ev.get("message") == "Command cancelled." for ev in infos)


def test_command_route_rejects_an_unsupported_command(storage) -> None:
    sessions: list[_RecordingSession] = []

    def factory(ui, model_alias=None, ws_id=None, **kwargs):  # type: ignore[no-untyped-def]
        sessions.append(_RecordingSession(**kwargs))
        return sessions[-1]

    mgr = _build_mgr_with_factory(storage, factory)
    client = _make_client(storage, mgr=mgr, config=_coord_endpoint_config())
    ws = mgr.create(user_id="user-1", name="spoken")
    listener = ws.ui._register_listener()

    resp = client.post(
        f"/v1/api/workstreams/{ws.id}/command",
        json={"command": "/rewind 2"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 400
    assert "/rewind is not a coordinator command" in resp.json()["error"]
    # The pane renders the error body; the route must not double it over SSE.
    assert [ev for ev in _events(listener) if ev.get("type") == "error"] == []
    assert sessions[-1].commands == []


def test_send_refusal_reaches_the_ui_even_for_a_stale_client(storage) -> None:
    """The /send guard is the backstop for a pane that still posts commands
    as chat: the refusal must be visible even if its HTTP body is ignored."""
    sessions: list[_RecordingSession] = []

    def factory(ui, model_alias=None, ws_id=None, **kwargs):  # type: ignore[no-untyped-def]
        sessions.append(_RecordingSession(**kwargs))
        return sessions[-1]

    mgr = _build_mgr_with_factory(storage, factory)
    client = _make_client(storage, mgr=mgr, config=_coord_endpoint_config())
    ws_id, session = _create_coord(mgr)
    ws = mgr.get(ws_id)
    assert ws is not None
    listener = ws.ui._register_listener()

    resp = client.post(
        f"/v1/api/workstreams/{ws_id}/send",
        json={"message": "/rewind 2"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 400
    assert "/rewind is not a coordinator command" in resp.json()["error"]
    errors = [ev for ev in _events(listener) if ev.get("type") == "error"]
    assert any("/rewind is not a coordinator command" in ev.get("message", "") for ev in errors)
    assert session.commands == []


def test_command_route_refuses_while_a_turn_runs(storage) -> None:
    sessions: list[_RecordingSession] = []

    def factory(ui, model_alias=None, ws_id=None, **kwargs):  # type: ignore[no-untyped-def]
        sessions.append(_RecordingSession(**kwargs))
        return sessions[-1]

    mgr = _build_mgr_with_factory(storage, factory)
    client = _make_client(storage, mgr=mgr, config=_coord_endpoint_config())
    ws = mgr.create(user_id="user-1", name="spoken")
    with ws._lock:
        ws._worker_running = True
    try:
        resp = client.post(
            f"/v1/api/workstreams/{ws.id}/command",
            json={"command": "/model alt"},
            headers=_COORD_HEADERS,
        )
    finally:
        with ws._lock:
            ws._worker_running = False
    assert resp.status_code == 409
    assert resp.json()["status"] == "busy"
    assert sessions[-1].commands == []


def test_command_route_requires_a_leading_slash(storage) -> None:
    mgr = _build_mgr_with_factory(storage, _real_factory(_real_registry(), _FakeConfigStore({})))
    client = _make_client(storage, mgr=mgr, config=_coord_endpoint_config())
    ws = mgr.create(user_id="user-1", name="spoken")

    resp = client.post(
        f"/v1/api/workstreams/{ws.id}/command",
        json={"command": "model alt"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "command must start with '/'"


def test_clear_command_emits_the_structural_reset(storage) -> None:
    sessions: list[_RecordingSession] = []

    def factory(ui, model_alias=None, ws_id=None, **kwargs):  # type: ignore[no-untyped-def]
        sessions.append(_RecordingSession(**kwargs))
        return sessions[-1]

    mgr = _build_mgr_with_factory(storage, factory)
    client = _make_client(storage, mgr=mgr, config=_coord_endpoint_config())
    ws = mgr.create(user_id="user-1", name="spoken")
    listener = ws.ui._register_listener()

    resp = client.post(
        f"/v1/api/workstreams/{ws.id}/command",
        json={"command": "/clear"},
        headers=_COORD_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert ws.worker_thread is not None
    ws.worker_thread.join(timeout=5)
    events = _events(listener)
    assert any(ev.get("type") == "clear_ui" for ev in events)
    assert sessions[-1].commands == [("/clear", "user-1")]
