"""Tests for voice-session agent reports (the report queue slice).

A voice session is a coordinator created with the ``voice`` persona.  It must
never block its turn inside ``wait_for_workstream`` — the user has stopped
listening by the time a child finishes — so a finished child leaves a report
row, offered on the next genuine user turn.  These tests cover the three
halves that must agree: the tool lists each coordinator kind sees, the
capture path in ``CoordinatorAdapter`` (one row per finished turn), and the
session half (offer note, ``voice_report`` read/dismiss, persistence across a
fresh session object).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests._session_helpers import make_session
from turnstone.console.coordinator_adapter import CoordinatorAdapter
from turnstone.core.personas import PersonaSnapshot
from turnstone.core.storage._registry import init_storage, is_storage_initialized, reset_storage
from turnstone.core.tools import COORDINATOR_TOOLS, VOICE_COORDINATOR_TOOLS
from turnstone.core.voice_config import VOICE_REPORT_STATES
from turnstone.core.workstream import Workstream, WorkstreamKind

_COORD = "a" * 32
_CHILD = "b" * 32


@pytest.fixture
def storage(tmp_path: Any) -> Any:
    """Process-global backend on its own SQLite file, closed after the test."""
    reset_storage()
    backend = init_storage("sqlite", path=str(tmp_path / "voice-reports.db"), run_migrations=False)
    try:
        yield backend
    finally:
        reset_storage()


# ---------------------------------------------------------------------------
# Tool lists
# ---------------------------------------------------------------------------


def _names(tools: list[dict[str, Any]]) -> set[str]:
    return {t["function"]["name"] for t in tools}


def test_voice_gets_the_report_tool_and_not_the_blocking_wait() -> None:
    names = _names(VOICE_COORDINATOR_TOOLS)
    assert "voice_report" in names
    assert "wait_for_workstream" not in names


def test_plain_coordinator_keeps_the_wait_and_not_the_report_tool() -> None:
    names = _names(COORDINATOR_TOOLS)
    assert "wait_for_workstream" in names
    assert "voice_report" not in names


def test_voice_report_is_absent_from_every_other_surface() -> None:
    # The voice tool must not leak into the interactive union either: it
    # reads a voice user's private reports, and a text session has none.
    from turnstone.core.tools import INTERACTIVE_TOOLS, TASK_AGENT_TOOLS, TOOLS

    assert "voice_report" in _names(TOOLS)  # the loader still owns the file
    for lane in (INTERACTIVE_TOOLS, TASK_AGENT_TOOLS):
        assert "voice_report" not in _names(lane)


def test_a_voice_session_is_built_with_the_swap(storage: Any) -> None:
    """The constants are only half the contract: the session must build its
    lane from them, and a plain coordinator must build the other one."""
    voice = _voice_session()
    assert "voice_report" in _names(voice._tools)
    assert "wait_for_workstream" not in _names(voice._tools)

    plain = _plainer_session()
    assert "wait_for_workstream" in _names(plain._tools)
    assert "voice_report" not in _names(plain._tools)


def test_voice_surface_is_the_coordinator_surface_plus_the_swap() -> None:
    # Nothing else may drift: the two lists differ by exactly one tool each
    # way, so a later tool change cannot silently fall out of voice.
    voice, plain = _names(VOICE_COORDINATOR_TOOLS), _names(COORDINATOR_TOOLS)
    assert voice - plain == {"voice_report"}
    assert plain - voice == {"wait_for_workstream"}


# ---------------------------------------------------------------------------
# Capture — the adapter side
# ---------------------------------------------------------------------------


def _adapter(storage: Any) -> CoordinatorAdapter:
    return CoordinatorAdapter(
        collector=MagicMock(),
        ui_factory=lambda ws: MagicMock(),
        session_factory=lambda *a, **kw: MagicMock(),
    )


class _UIRecorder:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []

    def _enqueue(self, payload: dict[str, Any]) -> None:
        self.enqueued.append(payload)


def _wire(adapter: CoordinatorAdapter, persona: str) -> _UIRecorder:
    """Install a coordinator under ``persona`` and register ``_CHILD`` under it."""
    coord_ws = Workstream(id=_COORD, name="ws-coord")
    coord_ws.kind = WorkstreamKind.COORDINATOR
    coord_ws.user_id = "u1"
    coord_ws.persona = persona
    recorder = _UIRecorder()
    coord_ws.ui = recorder  # type: ignore[assignment]
    adapter._registry.install(_COORD, recorder)
    adapter._registry.merge_children(_COORD, [_CHILD])

    manager = MagicMock()
    manager.get.side_effect = lambda ws_id: coord_ws if ws_id == _COORD else None
    adapter.attach(manager)
    return recorder


def _seed_child(storage: Any, message: str = "The research is done.") -> None:
    storage.register_workstream(
        _CHILD,
        node_id="node-1",
        name="ws-child",
        user_id="u1",
        kind=WorkstreamKind.INTERACTIVE,
    )
    storage.update_workstream_title(_CHILD, "Research the market")
    storage.save_message(_CHILD, "assistant", message)


def _state(adapter: CoordinatorAdapter, state: str) -> None:
    adapter._dispatch_child_event(
        {"type": "cluster_state", "ws_id": _CHILD, "state": state, "node_id": "node-1"}
    )


class TestCapture:
    def test_a_finished_child_leaves_one_pending_report(self, storage: Any) -> None:
        _seed_child(storage)
        adapter = _adapter(storage)
        _wire(adapter, persona="voice")

        _state(adapter, "running")
        _state(adapter, "idle")

        reports = storage.list_voice_reports(_COORD, None)
        assert len(reports) == 1
        assert reports[0]["state"] == "pending"
        assert reports[0]["child_ws_id"] == _CHILD
        assert reports[0]["title"] == "Research the market"
        assert reports[0]["body"] == "The research is done."

    def test_an_unchanged_state_event_is_not_a_second_report(self, storage: Any) -> None:
        # The collector re-emits unchanged states on every token tick; only a
        # transition INTO a finished state is news.
        _seed_child(storage)
        adapter = _adapter(storage)
        _wire(adapter, persona="voice")

        _state(adapter, "idle")
        _state(adapter, "idle")
        _state(adapter, "idle")
        assert len(storage.list_voice_reports(_COORD, None)) == 1

    def test_a_second_turn_after_work_is_a_second_report(self, storage: Any) -> None:
        _seed_child(storage)
        adapter = _adapter(storage)
        _wire(adapter, persona="voice")

        _state(adapter, "idle")
        _state(adapter, "running")
        storage.save_message(_CHILD, "assistant", "Second answer.")
        _state(adapter, "idle")

        reports = storage.list_voice_reports(_COORD, None)
        assert [r["body"] for r in reports] == ["The research is done.", "Second answer."]

    def test_an_error_captures_the_persisted_cause(self, storage: Any) -> None:
        _seed_child(storage, message="partial output")
        storage.save_workstream_config(_CHILD, {"last_error": "provider 500 after retries"})
        adapter = _adapter(storage)
        _wire(adapter, persona="voice")

        _state(adapter, "error")

        reports = storage.list_voice_reports(_COORD, None)
        assert len(reports) == 1
        assert "provider 500" in reports[0]["body"]

    def test_text_coordinators_capture_nothing(self, storage: Any) -> None:
        _seed_child(storage)
        adapter = _adapter(storage)
        _wire(adapter, persona="orchestrator")

        _state(adapter, "running")
        _state(adapter, "idle")
        assert storage.list_voice_reports(_COORD, None) == []

    def test_a_working_child_captures_nothing(self, storage: Any) -> None:
        _seed_child(storage)
        adapter = _adapter(storage)
        _wire(adapter, persona="voice")

        for state in ("thinking", "running", "attention", "evaluation"):
            _state(adapter, state)
        assert storage.list_voice_reports(_COORD, None) == []

    def test_capture_never_breaks_the_dashboard_fanout(self, storage: Any) -> None:
        # A report failure must not cost the child_ws_state event: the state
        # dispatch thread is the only path a dashboard has.
        _seed_child(storage)
        adapter = _adapter(storage)
        recorder = _wire(adapter, persona="voice")
        storage.create_voice_report = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

        _state(adapter, "idle")
        assert [p["type"] for p in recorder.enqueued] == ["child_ws_state"]

    def test_closing_a_coordinator_forgets_its_capture_state(self, storage: Any) -> None:
        _seed_child(storage)
        adapter = _adapter(storage)
        _wire(adapter, persona="voice")
        adapter._last_child_states[(_COORD, _CHILD)] = "idle"

        adapter.emit_closed(_COORD)
        assert adapter._last_child_states == {}


# ---------------------------------------------------------------------------
# Offer, read, dismiss — the session side
# ---------------------------------------------------------------------------


def _voice_session(*, ws_id: str = _COORD) -> Any:
    kwargs: dict[str, Any] = dict(
        ws_id=ws_id,
        user_id="u1",
        kind=WorkstreamKind.COORDINATOR,
        persona_snapshot=PersonaSnapshot(
            name="voice", prompt="", tools=None, mcp=True, memory=True
        ),
    )
    session = make_session(**kwargs)
    return session


def _plainer_session() -> Any:
    return make_session(
        ws_id=_COORD,
        user_id="u1",
        kind=WorkstreamKind.COORDINATOR,
    )


def _report(storage: Any, body: str = "All done.", title: str = "Research") -> dict[str, Any]:
    return storage.create_voice_report(_COORD, _CHILD, title=title, body=body)


def _user_turn(session: Any, text: str = "what's new?") -> None:
    from turnstone.core.session import _GenuineUserTurnPlan

    session._append_genuine_user_turn(
        _GenuineUserTurnPlan(content=text, principal_id="u1", memory_pointer=""),
        (),
        deferred_persistence=[],
    )


def _notes(session: Any) -> list[str]:
    return [turn.text for turn in session.messages if turn.source == "voice_reports"]


class TestOffer:
    def test_the_note_appears_once_and_marks_pending_offered(self, storage: Any) -> None:
        report = _report(storage)
        session = _voice_session()

        _user_turn(session)

        notes = _notes(session)
        assert len(notes) == 1
        assert "Research" in notes[0]
        assert report["report_id"] in notes[0]
        assert "Answer the user first" in notes[0]
        assert (
            storage.list_voice_reports(_COORD, ("offered",))[0]["report_id"]
            == (report["report_id"])
        )
        assert storage.list_voice_reports(_COORD, ("pending",)) == []

    def test_an_offered_report_is_still_listed_on_the_next_turn(self, storage: Any) -> None:
        # The user may not answer the offer in the same breath.  It keeps
        # being offered, and no second state change is attempted.
        _report(storage)
        session = _voice_session()
        _user_turn(session)
        _user_turn(session, "still there?")
        assert len(_notes(session)) == 2
        assert storage.list_voice_reports(_COORD, ("offered",))[0]["state"] == "offered"

    def test_no_reports_means_no_note(self, storage: Any) -> None:
        session = _voice_session()
        _user_turn(session)
        assert _notes(session) == []

    def test_a_text_coordinator_gets_no_note(self, storage: Any) -> None:
        _report(storage)
        session = _plainer_session()
        _user_turn(session)
        assert _notes(session) == []
        # Untouched: the report stays pending for the voice session.
        assert len(storage.list_voice_reports(_COORD, ("pending",))) == 1

    def test_the_offer_never_initializes_storage(self, tmp_path: Any, monkeypatch: Any) -> None:
        # ``get_storage`` auto-initializes SQLite, which would drop a stray
        # database in the process's cwd from a pure read path.  Construct
        # first, reset, then chdir somewhere new: the offer must stay a no-op.
        session = _voice_session()
        reset_storage()
        elsewhere = tmp_path / "after-reset"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        before = len(session.messages)

        session._append_voice_report_note([])

        assert len(session.messages) == before
        assert is_storage_initialized() is False
        assert not (elsewhere / ".turnstone.db").exists()

    def test_the_note_lists_at_most_six_reports(self, storage: Any) -> None:
        for n in range(8):
            storage.create_voice_report(_COORD, f"{n:032x}", title=f"Agent {n}", body="done")
        session = _voice_session()
        _user_turn(session)
        note = _notes(session)[0]
        bullets = [line for line in note.splitlines() if line.startswith("  - ")]
        assert len(bullets) == 6
        assert "...and 2 more" in note
        # Only the listed ones were offered; the rest wait for the next turn.
        assert len(storage.list_voice_reports(_COORD, ("pending",))) == 2

    def test_the_note_is_a_system_turn_not_user_text(self, storage: Any) -> None:
        _report(storage)
        session = _voice_session()
        _user_turn(session, "hello")
        assert session.messages[-2].role.value == "user"  # the human turn
        assert session.messages[-2].text == "hello"
        assert session.messages[-1].role.value == "system"
        assert session.messages[-1].source == "voice_reports"


def test_voice_wait_call_is_refused_before_it_blocks(storage: Any) -> None:
    """The tool is not offered, but a replayed transcript can still call it.

    A voice turn that parks in ``wait_for_workstream`` is the exact failure
    this slice exists to remove, so the preparer refuses it outright - the
    client is never asked to block.
    """
    session = _voice_session()
    session._coord_client = MagicMock()
    item = session._prepare_tool(
        {
            "id": "call-w",
            "type": "function",
            "function": {
                "name": "wait_for_workstream",
                "arguments": json.dumps({"ws_ids": [_CHILD]}),
            },
        }
    )
    assert "error" in item
    assert "not available in a voice session" in item["error"]
    session._coord_client.wait_for_workstream.assert_not_called()


def test_a_text_coordinator_can_still_prepare_the_wait(storage: Any) -> None:
    session = _plainer_session()
    session._coord_client = MagicMock()
    item = session._prepare_tool(
        {
            "id": "call-w",
            "type": "function",
            "function": {
                "name": "wait_for_workstream",
                "arguments": json.dumps({"ws_ids": [_CHILD]}),
            },
        }
    )
    assert "error" not in item
    assert item["func_name"] == "wait_for_workstream"


class TestVoiceReportTool:
    def _prepared(self, session: Any, action: str, report_id: str) -> dict[str, Any]:
        item = session._prepare_tool(
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "voice_report",
                    "arguments": json.dumps({"action": action, "report_id": report_id}),
                },
            }
        )
        return item

    def test_read_returns_the_body_and_marks_it_read(self, storage: Any) -> None:
        report = _report(storage, body="The market grew 12%.")
        session = _voice_session()

        item = self._prepared(session, "read", report["report_id"])
        assert item.get("needs_approval") is False
        _call_id, output = item["execute"](item)

        payload = json.loads(output)
        assert payload["body"] == "The market grew 12%."
        assert payload["title"] == "Research"
        assert payload["state"] == "read"
        assert (
            storage.list_voice_reports(_COORD, ("read",))[0]["report_id"] == (report["report_id"])
        )

    def test_dismiss_marks_it_dismissed_without_the_body(self, storage: Any) -> None:
        report = _report(storage, body="secret body")
        session = _voice_session()

        item = self._prepared(session, "dismiss", report["report_id"])
        _call_id, output = item["execute"](item)

        payload = json.loads(output)
        assert payload == {"report_id": report["report_id"], "state": "dismissed"}
        assert "secret body" not in output
        assert (
            storage.list_voice_reports(_COORD, ("dismissed",))[0]["report_id"]
            == (report["report_id"])
        )

    def test_an_unknown_report_is_refused(self, storage: Any) -> None:
        session = _voice_session()
        item = self._prepared(session, "read", "f" * 32)
        index, output = item["execute"](item)
        assert index == "call-1"
        assert output.startswith("Error: no report")

    def test_another_sessions_report_is_not_readable(self, storage: Any) -> None:
        # ``report_id`` is globally unique, so a guessed/foreign id must read
        # as missing rather than leak a different user's report.
        foreign = storage.create_voice_report("c" * 32, _CHILD, title="Theirs", body="theirs")
        session = _voice_session()
        item = self._prepared(session, "read", foreign["report_id"])
        _call_id, output = item["execute"](item)
        assert output.startswith("Error: no report")
        assert storage.list_voice_reports("c" * 32, ("pending",))[0]["state"] == "pending"

    def test_a_dismissed_report_cannot_be_read(self, storage: Any) -> None:
        report = _report(storage)
        storage.update_voice_report_state(report["report_id"], "dismissed")
        session = _voice_session()
        item = self._prepared(session, "read", report["report_id"])
        _call_id, output = item["execute"](item)
        assert "dismissed" in output

    def test_a_text_coordinator_is_refused(self, storage: Any) -> None:
        report = _report(storage)
        session = _plainer_session()
        item = self._prepared(session, "read", report["report_id"])
        assert "error" in item
        assert "voice session" in item["error"]

    def test_bad_arguments_are_refused_at_prepare(self, storage: Any) -> None:
        session = _voice_session()
        assert "action must be one of" in self._prepared(session, "shout", "a" * 32)["error"]
        assert "report_id is required" in self._prepared(session, "read", "")["error"]


class TestPersistence:
    def test_pending_reports_survive_a_fresh_session(self, storage: Any) -> None:
        # A console restart reopens the workstream as a new ChatSession on the
        # same storage; the queue is in the DB precisely so nothing is lost.
        _report(storage, body="Still waiting.")
        first = _voice_session()
        assert first.ws_id == _COORD

        second = _voice_session()
        _user_turn(second, "back again")

        note = _notes(second)[0]
        assert "Still waiting." not in note  # the note carries the title + id
        assert "Research" in note
        assert storage.list_voice_reports(_COORD, None)[0]["state"] == "offered"


def test_report_states_are_the_contract_vocabulary() -> None:
    assert VOICE_REPORT_STATES == ("pending", "offered", "read", "dismissed")


# ---------------------------------------------------------------------------
# Announce: a finished agent speaks up while the session is idle
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str) -> Any:
        from turnstone.core.settings_registry import SETTINGS

        return self.values.get(key, SETTINGS[key].default)


def _wire_announce(
    adapter: CoordinatorAdapter,
    monkeypatch: Any,
    *,
    on: bool = True,
    state: Any = None,
) -> tuple[Any, list[str]]:
    """A voice coordinator with a real nudge queue and a recorded wake."""
    from turnstone.console import coordinator_adapter as mod
    from turnstone.core.nudge_queue import NudgeQueue
    from turnstone.core.workstream import WorkstreamState

    _wire(adapter, persona="voice")
    coord_ws = adapter._manager.get(_COORD)
    session = MagicMock()
    session._nudge_queue = NudgeQueue()
    session._config_store = _Store({"audio.voice_announce_reports": on})
    coord_ws.session = session
    coord_ws.state = state if state is not None else WorkstreamState.IDLE
    woken: list[str] = []
    monkeypatch.setattr(
        mod, "wake_workstream_if_pending", lambda ws, **kw: woken.append(kw.get("trigger", ""))
    )
    return session, woken


class TestAnnounce:
    def test_setting_is_registered_off_with_help_and_docs(self) -> None:
        from turnstone.core.settings_registry import validate_key
        from turnstone.core.voice_config import VOICE_DOCS_URL

        d = validate_key("audio.voice_announce_reports")
        assert d.default is False
        assert d.help
        assert d.reference_url == VOICE_DOCS_URL + "#announcing-finished-agents"

    def test_an_idle_session_is_woken_with_a_one_line_offer(
        self, storage: Any, monkeypatch: Any
    ) -> None:
        _seed_child(storage)
        adapter = _adapter(storage)
        session, woken = _wire_announce(adapter, monkeypatch)

        _state(adapter, "running")
        _state(adapter, "idle")

        assert woken == ["voice-report"]
        entries = session._nudge_queue.drain(frozenset({"wake"}))
        assert len(entries) == 1
        nudge_type, text, meta = entries[0]
        assert nudge_type == "voice_reports"
        assert "Research the market" in text
        assert "ask" in text.lower()
        # The announcement never carries the body: the user chooses to hear it.
        assert "The research is done." not in text
        assert meta and meta.get("report_id")

    def test_setting_off_announces_nothing(self, storage: Any, monkeypatch: Any) -> None:
        _seed_child(storage)
        adapter = _adapter(storage)
        session, woken = _wire_announce(adapter, monkeypatch, on=False)

        _state(adapter, "running")
        _state(adapter, "idle")

        assert woken == []
        assert session._nudge_queue.drain(frozenset({"wake"})) == []
        # The report is still saved for the next spoken turn.
        assert len(storage.list_voice_reports(_COORD, None)) == 1

    def test_a_busy_session_is_not_interrupted(self, storage: Any, monkeypatch: Any) -> None:
        from turnstone.core.workstream import WorkstreamState

        _seed_child(storage)
        adapter = _adapter(storage)
        session, woken = _wire_announce(adapter, monkeypatch, state=WorkstreamState.RUNNING)

        _state(adapter, "running")
        _state(adapter, "idle")

        assert woken == []
        assert session._nudge_queue.drain(frozenset({"wake"})) == []

    def test_an_offer_already_made_drops_the_queued_announcement(
        self, storage: Any, monkeypatch: Any
    ) -> None:
        """The user spoke first: the report was offered on that turn, so the
        queued announcement must not repeat it."""
        _seed_child(storage)
        adapter = _adapter(storage)
        session, _woken = _wire_announce(adapter, monkeypatch)

        _state(adapter, "running")
        _state(adapter, "idle")
        report = storage.list_voice_reports(_COORD, None)[0]
        storage.update_voice_report_state(report["report_id"], "offered")

        # drain() re-checks valid_until at delivery and drops the stale entry.
        assert session._nudge_queue.drain(frozenset({"wake"})) == []

    def test_text_coordinators_are_never_announced(self, storage: Any, monkeypatch: Any) -> None:
        from turnstone.console import coordinator_adapter as mod

        _seed_child(storage)
        adapter = _adapter(storage)
        _wire(adapter, persona="coordinator")
        woken: list[str] = []
        monkeypatch.setattr(mod, "wake_workstream_if_pending", lambda ws, **kw: woken.append("x"))

        _state(adapter, "running")
        _state(adapter, "idle")

        assert woken == []

    def test_a_failing_wake_never_breaks_capture(self, storage: Any, monkeypatch: Any) -> None:
        from turnstone.console import coordinator_adapter as mod

        _seed_child(storage)
        adapter = _adapter(storage)
        _wire_announce(adapter, monkeypatch)

        def _boom(ws: Any, **kw: Any) -> None:
            raise RuntimeError("wake down")

        monkeypatch.setattr(mod, "wake_workstream_if_pending", _boom)
        _state(adapter, "running")
        _state(adapter, "idle")

        assert len(storage.list_voice_reports(_COORD, None)) == 1
