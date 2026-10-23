"""Tests for the voice persona's per-turn spawn guard.

A voice coordinator hears spoken turns, so a passing aside can sound like a
request.  It may therefore start only ``coordinator.voice_max_agents_per_turn``
children per genuine user turn, and an unnamed child defaults to
``coordinator.voice_child_model``.  Both rules key off the persona stamp, so
the tests build the session the same way the console factory does.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from turnstone.core.personas import PersonaSnapshot
from turnstone.core.session import (
    ChatSession,
    _GenuineUserTurnPlan,
    _voice_explicit_agent_count,
)
from turnstone.core.settings_registry import SETTINGS
from turnstone.prompts import ClientType


class _StubUI:
    """Minimal SessionUI that records signals without doing anything with them."""

    def __init__(self) -> None:
        self._user_id = "user-1"
        self.infos: list[str] = []
        self.errors: list[str] = []
        self.tool_results: list[tuple[str, str, str, bool]] = []

    def on_info(self, msg: str) -> None:
        self.infos.append(msg)

    def on_error(self, msg: str) -> None:
        self.errors.append(msg)

    def on_tool_result(
        self,
        call_id: str,
        name: str,
        output: str,
        is_error: bool = False,
        preview: dict[str, Any] | None = None,
    ) -> None:
        self.tool_results.append((call_id, name, output, is_error))

    # Other SessionUI methods — only stubs, not exercised here.
    def on_turn_start(self) -> None:
        pass

    def on_turn_end(self) -> None:
        pass

    def on_stream_start(self) -> None:
        pass

    def on_stream_end(self) -> None:
        pass

    def on_message_delta(self, delta: str) -> None:
        pass

    def on_reasoning_delta(self, delta: str) -> None:
        pass

    def on_tool_call(self, call_id: str, name: str, header: str, preview: str) -> None:
        pass

    def on_completion(self, content: str) -> None:
        pass

    def on_attention(self, header: str, preview: str = "") -> None:
        pass

    def on_state_change(self, state: str) -> None:
        pass

    def approve_tools(self, items: list) -> tuple[bool, str | None]:
        return True, None

    def wait_for_approval(
        self,
        call_id: str,
        name: str,
        header: str,
        preview: str,
        *,
        label: str = "",
    ) -> tuple[bool, str | None]:
        return True, None


class _FakeConfigStore:
    """Read side of ConfigStore, including registry defaults for unset keys."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self._values = dict(values or {})

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._values:
            return self._values[key]
        defn = SETTINGS.get(key)
        return defn.default if defn is not None else default


def _voice_persona() -> PersonaSnapshot:
    return PersonaSnapshot(name="voice", prompt="", tools=None, mcp=True, memory=True)


def _make_session(
    monkeypatch,
    *,
    voice: bool,
    settings: dict[str, Any] | None = None,
) -> tuple[ChatSession, MagicMock, _StubUI]:
    """Build a coordinator ChatSession with a mocked CoordinatorClient."""
    monkeypatch.setattr(ChatSession, "_load_skills", lambda self: None)
    monkeypatch.setattr(ChatSession, "_init_system_messages", lambda self: None)
    monkeypatch.setattr(ChatSession, "_save_config", lambda self: None)

    ui = _StubUI()
    coord_client = MagicMock()
    sess = ChatSession(
        client=MagicMock(),
        model="gpt-test",
        ui=ui,  # type: ignore[arg-type]
        instructions=None,
        temperature=0.0,
        max_tokens=1024,
        tool_timeout=30,
        context_window=16384,
        ws_id="coord-voice",
        user_id="user-1",
        client_type=ClientType.WEB,
        kind="coordinator",
        coord_client=coord_client,
        config_store=_FakeConfigStore(settings),
        persona_snapshot=_voice_persona() if voice else None,
    )
    return sess, coord_client, ui


def _tc(name: str, args: dict[str, Any], call_id: str = "call-1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _spawn_result(ws_id: str = "child-1") -> dict[str, Any]:
    return {"ws_id": ws_id, "name": "kid", "node_id": "node-1", "status": 200}


def _batch_args(count: int, *, requested: bool | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {
        "children": [{"initial_message": f"task {idx}"} for idx in range(count)]
    }
    if requested is not None:
        args["user_requested_count"] = requested
    return args


def _append_user_turn(sess: ChatSession, text: str = "next question") -> None:
    """Admit one genuine USER turn through the same funnel production uses."""
    sess._append_genuine_user_turn(
        _GenuineUserTurnPlan(content=text, principal_id="user-1", memory_pointer=""),
        (),
        deferred_persistence=[],
    )


# ---------------------------------------------------------------------------
# Per-turn cap
# ---------------------------------------------------------------------------


def test_voice_second_spawn_in_same_turn_is_denied(monkeypatch):
    sess, coord, _ui = _make_session(monkeypatch, voice=True)
    coord.spawn.return_value = _spawn_result()

    first = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "one"}))
    assert "error" not in first
    sess._exec_spawn_workstream(first)

    second = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "two"}))
    assert "error" in second
    assert "at most 1 agent" in second["error"]
    assert "Ask the user" in second["error"]
    # No user_requested_count on the call: ask the user, do not ask them to
    # name a number the message never contained.
    assert "Ask the user before starting another." in second["error"]
    assert "user_requested_count" not in second["error"]
    assert second.get("needs_approval") is not True
    # Refused before any child was created.
    assert coord.spawn.call_count == 1


def test_voice_unbacked_flag_keeps_the_confirmation_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag was set, but the user's words did not name the count, so the
    refusal still asks for that confirmation."""
    sess, coord, _ui = _make_session(monkeypatch, voice=True)
    coord.spawn.return_value = _spawn_result()
    _append_user_turn(sess, "go ahead")

    first = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "one"}))
    assert "error" not in first
    sess._exec_spawn_workstream(first)

    second = sess._prepare_tool(
        _tc("spawn_workstream", {"initial_message": "two", "user_requested_count": True})
    )
    assert "error" in second
    assert "Ask the user to confirm the number" in second["error"]
    assert "user_requested_count" in second["error"]
    assert coord.spawn.call_count == 1


def test_voice_budget_resets_on_a_genuine_user_turn(monkeypatch):
    sess, coord, _ui = _make_session(monkeypatch, voice=True)
    coord.spawn.return_value = _spawn_result()

    first = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "one"}))
    sess._exec_spawn_workstream(first)
    denied = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "two"}))
    assert "error" in denied

    _append_user_turn(sess)

    allowed = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "three"}))
    assert "error" not in allowed
    sess._exec_spawn_workstream(allowed)
    assert coord.spawn.call_count == 2


def test_voice_wake_turn_does_not_reset_the_budget(monkeypatch):
    """Only genuine USER turns open a fresh budget; a wake's synthetic row doesn't."""
    sess, coord, _ui = _make_session(monkeypatch, voice=True)
    coord.spawn.return_value = _spawn_result()

    first = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "one"}))
    sess._exec_spawn_workstream(first)

    sess._append_user_turn("", (), from_wake=True, deferred_persistence=[])

    denied = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "two"}))
    assert "error" in denied
    assert coord.spawn.call_count == 1


def test_voice_exec_guard_catches_two_calls_prepared_before_any_exec(monkeypatch):
    """Prepare-time checks alone can't see a sibling tool call in the same
    assistant message — both prepare at count 0.  Exec re-checks."""
    sess, coord, ui = _make_session(monkeypatch, voice=True)
    coord.spawn.return_value = _spawn_result()

    first = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "one"}, "call-1"))
    second = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "two"}, "call-2"))
    assert "error" not in first and "error" not in second

    sess._exec_spawn_workstream(first)
    _call_id, output = sess._exec_spawn_workstream(second)

    assert "at most 1 agent" in output
    assert coord.spawn.call_count == 1
    assert ui.tool_results[-1][3] is True  # is_error


def test_voice_batch_over_cap_is_denied_whole(monkeypatch):
    sess, coord, _ui = _make_session(monkeypatch, voice=True)

    item = sess._prepare_tool(
        _tc(
            "spawn_batch",
            {"children": [{"initial_message": "a"}, {"initial_message": "b"}]},
        )
    )
    assert "error" in item
    assert "at most 1 agent" in item["error"]
    assert "whole batch" in item["error"]
    assert item.get("needs_approval") is not True
    assert coord.spawn.call_count == 0


def test_voice_batch_over_cap_at_exec_is_denied_whole(monkeypatch):
    """The same sibling-call race as singles: a one-child batch prepared at
    count 0 must not spawn when the budget is gone by exec time."""
    sess, coord, ui = _make_session(monkeypatch, voice=True)
    coord.spawn.return_value = _spawn_result()

    filler = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "one"}))
    batch = sess._prepare_tool(
        _tc(
            "spawn_batch",
            {"children": [{"initial_message": "a"}]},
            "call-batch",
        )
    )
    assert "error" not in batch

    sess._exec_spawn_workstream(filler)
    _call_id, output = sess._exec_spawn_batch(batch)

    assert "whole batch" in output
    assert coord.spawn.call_count == 1
    assert ui.tool_results[-1][3] is True


def test_voice_failed_child_releases_its_slot(monkeypatch):
    """A spawn that never starts must give its reserved slot back: the turn's
    budget counts children that exist, not attempts."""
    sess, coord, _ui = _make_session(monkeypatch, voice=True)
    coord.spawn.return_value = {"error": "upstream unavailable", "status": 502}

    failed = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "one"}))
    sess._exec_spawn_workstream(failed)
    assert sess._voice_spawns_this_turn == 0

    coord.spawn.return_value = _spawn_result("child-2")
    allowed = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "two"}))
    assert "error" not in allowed
    _call_id, output = sess._exec_spawn_workstream(allowed)
    assert "child-2" in output
    assert coord.spawn.call_count == 2

    denied = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "three"}))
    assert "error" in denied


def test_voice_batch_releases_slots_for_children_that_never_start(monkeypatch):
    """Per-row denials inside an approved batch hand their slots back, so the
    count matches the children that actually exist."""
    sess, coord, _ui = _make_session(
        monkeypatch,
        voice=True,
        settings={"coordinator.voice_max_agents_per_turn": 2},
    )

    def _spawn(**kwargs):
        if kwargs["initial_message"] == "a":
            return {"error": "upstream unavailable", "status": 502}
        return _spawn_result("child-b")

    coord.spawn.side_effect = _spawn
    batch = sess._prepare_tool(
        _tc(
            "spawn_batch",
            {"children": [{"initial_message": "a"}, {"initial_message": "b"}]},
        )
    )
    sess._exec_spawn_batch(batch)

    assert sess._voice_spawns_this_turn == 1
    allowed = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "third"}))
    assert "error" not in allowed


def test_voice_batch_counts_each_child_against_the_cap(monkeypatch):
    sess, coord, _ui = _make_session(
        monkeypatch,
        voice=True,
        settings={"coordinator.voice_max_agents_per_turn": 2},
    )
    spawned: list[str] = []

    def _spawn(**kwargs):
        spawned.append(kwargs["initial_message"])
        return _spawn_result(f"child-{len(spawned)}")

    coord.spawn.side_effect = _spawn
    batch = sess._prepare_tool(
        _tc(
            "spawn_batch",
            {"children": [{"initial_message": "a"}, {"initial_message": "b"}]},
        )
    )
    assert "error" not in batch
    sess._exec_spawn_batch(batch)
    assert len(spawned) == 2

    denied = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "third"}))
    assert "error" in denied
    assert "at most 2 agents" in denied["error"]


def test_voice_batch_counts_only_children_that_started(monkeypatch):
    sess, coord, _ui = _make_session(
        monkeypatch,
        voice=True,
        settings={"coordinator.voice_max_agents_per_turn": 2},
    )

    def _spawn(**kwargs):
        if kwargs["initial_message"] == "a":
            return {"error": "upstream unavailable", "status": 502}
        return _spawn_result("child-b")

    coord.spawn.side_effect = _spawn
    batch = sess._prepare_tool(
        _tc(
            "spawn_batch",
            {"children": [{"initial_message": "a"}, {"initial_message": "b"}]},
        )
    )
    sess._exec_spawn_batch(batch)

    # One child actually started, so one slot remains in this turn.
    allowed = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "third"}))
    assert "error" not in allowed


def test_voice_cap_zero_denies_single_and_batch(monkeypatch):
    sess, coord, _ui = _make_session(
        monkeypatch,
        voice=True,
        settings={"coordinator.voice_max_agents_per_turn": 0},
    )

    single = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "go"}))
    assert "error" in single
    assert "no agents" in single["error"]

    batch = sess._prepare_tool(_tc("spawn_batch", {"children": [{"initial_message": "a"}]}))
    assert "error" in batch
    assert "no agents" in batch["error"]
    assert coord.spawn.call_count == 0


# ---------------------------------------------------------------------------
# Default child model
# ---------------------------------------------------------------------------


def test_voice_omitted_model_uses_configured_child_model(monkeypatch):
    sess, coord, _ui = _make_session(
        monkeypatch,
        voice=True,
        settings={"coordinator.voice_child_model": "voice-child-alias"},
    )
    item = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "hi"}))
    assert item["model"] == "voice-child-alias"

    coord.spawn.return_value = _spawn_result()
    sess._exec_spawn_workstream(item)
    assert coord.spawn.call_args.kwargs["model"] == "voice-child-alias"


def test_voice_empty_model_arg_uses_configured_child_model(monkeypatch):
    sess, _coord, _ui = _make_session(
        monkeypatch,
        voice=True,
        settings={"coordinator.voice_child_model": "voice-child-alias"},
    )
    item = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "hi", "model": "   "}))
    assert item["model"] == "voice-child-alias"


def test_voice_batch_rows_use_configured_child_model(monkeypatch):
    sess, _coord, _ui = _make_session(
        monkeypatch,
        voice=True,
        settings={"coordinator.voice_child_model": "voice-child-alias"},
    )
    item = sess._prepare_tool(_tc("spawn_batch", {"children": [{"initial_message": "a"}]}))
    assert item["children"][0]["model"] == "voice-child-alias"


def test_voice_explicit_model_is_never_overridden(monkeypatch):
    sess, _coord, _ui = _make_session(
        monkeypatch,
        voice=True,
        settings={"coordinator.voice_child_model": "voice-child-alias"},
    )
    item = sess._prepare_tool(
        _tc("spawn_workstream", {"initial_message": "hi", "model": "explicit-alias"})
    )
    assert item["model"] == "explicit-alias"


def test_voice_empty_child_model_setting_leaves_model_unset(monkeypatch):
    sess, _coord, _ui = _make_session(
        monkeypatch,
        voice=True,
        settings={"coordinator.voice_child_model": ""},
    )
    item = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "hi"}))
    assert item["model"] == ""


# ---------------------------------------------------------------------------
# Non-voice coordinators are untouched
# ---------------------------------------------------------------------------


def test_plain_coordinator_is_unaffected_by_the_voice_guard(monkeypatch):
    sess, coord, _ui = _make_session(
        monkeypatch,
        voice=False,
        settings={
            "coordinator.voice_max_agents_per_turn": 0,
            "coordinator.voice_child_model": "voice-child-alias",
        },
    )
    coord.spawn.side_effect = lambda **kwargs: _spawn_result(f"child-{coord.spawn.call_count}")

    for idx, message in enumerate(("one", "two")):
        item = sess._prepare_tool(
            _tc("spawn_workstream", {"initial_message": message}, f"call-{idx}")
        )
        assert "error" not in item
        assert item["model"] == ""
        sess._exec_spawn_workstream(item)

    assert coord.spawn.call_count == 2
    assert all(call.kwargs["model"] == "" for call in coord.spawn.call_args_list)


# ---------------------------------------------------------------------------
# Explicitly requested batches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("chunk it up to four different agents", 4),
        ("start 4 agents", 4),
        ("please start four agents", 4),
        ("spawn four agents", 4),
        ("three separate agents", 3),
        ("one agent each", 1),
        ("hand one to each of the 3 agents", 3),
        ("spawn 10 agents", 10),
        ("one agent is enough for that", 1),
        ("my four kids, each one wants a snack", 0),
        ("use three agents each with five minute budget", 3),
        ("two agents and three agents", 0),
        ("four agents, then four agents", 4),
        ("back in five minutes", 0),
        ("start some agents", 0),
        ("do eleven agents", 0),
        ("four different researchers", 0),
        ("four kids want an agent", 0),
        ("", 0),
    ],
)
def test_voice_explicit_agent_count(message: str, expected: int) -> None:
    """Only a count directly before "agent(s)" licenses the requested ceiling.

    A number elsewhere in the sentence ("my four kids"), a standalone "each"
    and a second, different qualifying count (ambiguous) all read as no
    request rather than guessing which noun the speaker meant.
    """
    assert _voice_explicit_agent_count(message) == expected


def test_voice_explicit_four_agent_batch_is_allowed(monkeypatch):
    sess, coord, _ui = _make_session(monkeypatch, voice=True)
    coord.spawn.side_effect = lambda **kwargs: _spawn_result(f"child-{coord.spawn.call_count}")
    _append_user_turn(sess, "chunk it up to four different agents")

    batch = sess._prepare_tool(_tc("spawn_batch", _batch_args(4, requested=True)))

    assert "error" not in batch
    assert batch["needs_approval"] is True
    sess._exec_spawn_batch(batch)
    assert coord.spawn.call_count == 4


def test_voice_four_agent_batch_without_the_flag_is_refused(monkeypatch):
    sess, coord, _ui = _make_session(monkeypatch, voice=True)
    _append_user_turn(sess, "chunk it up to four different agents")

    batch = sess._prepare_tool(_tc("spawn_batch", _batch_args(4)))

    assert "error" in batch
    assert "at most 1 agent" in batch["error"]
    assert "whole batch" in batch["error"]
    # No flag on the call, so the base wording applies here too.
    assert "Ask the user before starting another." in batch["error"]
    assert "user_requested_count" not in batch["error"]
    assert coord.spawn.call_count == 0


def test_voice_spawn_four_agents_batch_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    # "spawn four agents" is the count directly governing the cue; the batch
    # of four the model asks for is exactly what the user requested.
    sess, coord, _ui = _make_session(monkeypatch, voice=True)
    coord.spawn.side_effect = lambda **kwargs: _spawn_result(f"child-{coord.spawn.call_count}")
    _append_user_turn(sess, "spawn four agents")

    batch = sess._prepare_tool(_tc("spawn_batch", _batch_args(4, requested=True)))

    assert "error" not in batch
    sess._exec_spawn_batch(batch)
    assert coord.spawn.call_count == 4


def test_voice_one_agent_request_caps_every_spawn_in_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "one agent is enough" licenses one agent for the whole turn.  The flag
    # repeated on later spawn_workstream calls must not reopen the ceiling.
    sess, coord, _ui = _make_session(monkeypatch, voice=True)
    coord.spawn.side_effect = lambda **kwargs: _spawn_result(f"child-{coord.spawn.call_count}")
    _append_user_turn(sess, "one agent is enough for that")

    for idx in range(4):
        item = sess._prepare_tool(
            _tc(
                "spawn_workstream",
                {"initial_message": f"task {idx}", "user_requested_count": True},
                f"call-{idx}",
            )
        )
        if idx == 0:
            assert "error" not in item
            sess._exec_spawn_workstream(item)
        else:
            assert "error" in item
            assert "at most 1 agent" in item["error"]

    assert coord.spawn.call_count == 1


def test_voice_four_kids_each_is_not_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # "each" alone no longer ties the number to agents: the four children are
    # the subject of the sentence, not the fan-out the user asked for.
    sess, coord, _ui = _make_session(monkeypatch, voice=True)
    _append_user_turn(sess, "my four kids, each one wants a snack")

    batch = sess._prepare_tool(_tc("spawn_batch", _batch_args(4, requested=True)))

    assert "error" in batch
    assert "at most 1 agent" in batch["error"]
    assert coord.spawn.call_count == 0


def test_voice_three_agents_with_a_five_minute_budget_is_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The five minutes belong to the budget, not to the agent count: three is
    # the request, and the turn's budget is that count, not the ceiling.
    sess, coord, _ui = _make_session(monkeypatch, voice=True)
    coord.spawn.side_effect = lambda **kwargs: _spawn_result(f"child-{coord.spawn.call_count}")
    _append_user_turn(sess, "use three agents each with five minute budget")

    batch = sess._prepare_tool(_tc("spawn_batch", _batch_args(3, requested=True)))
    assert "error" not in batch
    sess._exec_spawn_batch(batch)
    assert coord.spawn.call_count == 3

    denied = sess._prepare_tool(
        _tc("spawn_workstream", {"initial_message": "another", "user_requested_count": True})
    )
    assert "error" in denied
    assert "at most 3 agents" in denied["error"]


def test_voice_flag_without_a_count_in_the_user_message_is_refused(monkeypatch):
    sess, coord, _ui = _make_session(monkeypatch, voice=True)
    _append_user_turn(sess, "go ahead and chunk it up")

    batch = sess._prepare_tool(_tc("spawn_batch", _batch_args(4, requested=True)))

    assert "error" in batch
    assert "at most 1 agent" in batch["error"]
    assert "Ask the user to confirm the number" in batch["error"]
    assert "user_requested_count" in batch["error"]
    assert coord.spawn.call_count == 0


def test_voice_flag_count_below_the_batch_size_is_refused(monkeypatch):
    sess, coord, _ui = _make_session(monkeypatch, voice=True)
    _append_user_turn(sess, "start two agents")

    batch = sess._prepare_tool(_tc("spawn_batch", _batch_args(4, requested=True)))

    assert "error" in batch
    assert "at most 1 agent" in batch["error"]
    assert coord.spawn.call_count == 0


def test_voice_batch_above_the_requested_ceiling_is_refused(monkeypatch):
    sess, coord, _ui = _make_session(
        monkeypatch,
        voice=True,
        settings={"coordinator.voice_max_requested_agents": 4},
    )
    _append_user_turn(sess, "start five agents")

    batch = sess._prepare_tool(_tc("spawn_batch", _batch_args(5, requested=True)))

    assert "error" in batch
    assert "at most 4 agents" in batch["error"]
    assert "whole batch" in batch["error"]
    assert coord.spawn.call_count == 0


def test_voice_flag_widens_the_single_spawn_budget(monkeypatch):
    """An explicit count applies to spawn_workstream too, not just batches."""
    sess, coord, _ui = _make_session(monkeypatch, voice=True)
    coord.spawn.side_effect = lambda **kwargs: _spawn_result(f"child-{coord.spawn.call_count}")

    first = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "one"}))
    sess._exec_spawn_workstream(first)

    _append_user_turn(sess, "start one more agent")
    second = sess._prepare_tool(
        _tc("spawn_workstream", {"initial_message": "two", "user_requested_count": True})
    )
    assert "error" not in second
    sess._exec_spawn_workstream(second)
    assert coord.spawn.call_count == 2


def test_voice_cap_zero_still_refuses_everything(monkeypatch):
    """The operator's off switch is not reopened by an explicit request."""
    sess, coord, _ui = _make_session(
        monkeypatch,
        voice=True,
        settings={"coordinator.voice_max_agents_per_turn": 0},
    )
    _append_user_turn(sess, "start four agents")

    batch = sess._prepare_tool(_tc("spawn_batch", _batch_args(4, requested=True)))
    assert "error" in batch
    assert "no agents" in batch["error"]

    single = sess._prepare_tool(
        _tc("spawn_workstream", {"initial_message": "go", "user_requested_count": True})
    )
    assert "error" in single
    assert "no agents" in single["error"]
    assert coord.spawn.call_count == 0


def test_voice_non_numeric_requested_ceiling_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A corrupted row must neither crash the read nor widen the budget past
    # the registry default: "lots" behaves exactly like an unset key.
    default = int(SETTINGS["coordinator.voice_max_requested_agents"].default)
    sess, coord, _ui = _make_session(
        monkeypatch,
        voice=True,
        settings={"coordinator.voice_max_requested_agents": "lots"},
    )
    assert sess._voice_max_requested_agents() == default
    coord.spawn.side_effect = lambda **kwargs: _spawn_result(f"child-{coord.spawn.call_count}")
    _append_user_turn(sess, "spawn four agents")

    batch = sess._prepare_tool(_tc("spawn_batch", _batch_args(default, requested=True)))
    assert "error" not in batch
    sess._exec_spawn_batch(batch)
    assert coord.spawn.call_count == default

    denied = sess._prepare_tool(
        _tc("spawn_workstream", {"initial_message": "more", "user_requested_count": True})
    )
    assert "error" in denied
    assert f"at most {default} agents" in denied["error"]


def test_voice_requested_ceiling_zero_refuses_a_wider_batch(monkeypatch):
    sess, coord, _ui = _make_session(
        monkeypatch,
        voice=True,
        settings={
            "coordinator.voice_max_agents_per_turn": 1,
            "coordinator.voice_max_requested_agents": 0,
        },
    )
    _append_user_turn(sess, "start four agents")

    batch = sess._prepare_tool(_tc("spawn_batch", _batch_args(4, requested=True)))

    assert "error" in batch
    assert "at most 1 agent" in batch["error"]
    assert coord.spawn.call_count == 0


def test_voice_explicit_budget_resets_on_the_next_user_turn(monkeypatch):
    sess, coord, _ui = _make_session(monkeypatch, voice=True)
    coord.spawn.side_effect = lambda **kwargs: _spawn_result(f"child-{coord.spawn.call_count}")
    _append_user_turn(sess, "start four agents")

    first = sess._prepare_tool(_tc("spawn_batch", _batch_args(4, requested=True), "call-1"))
    assert "error" not in first
    sess._exec_spawn_batch(first)
    assert coord.spawn.call_count == 4

    denied = sess._prepare_tool(_tc("spawn_batch", _batch_args(4, requested=True), "call-2"))
    assert "error" in denied
    assert "at most 4 agents" in denied["error"]

    _append_user_turn(sess, "start four more agents")
    second = sess._prepare_tool(_tc("spawn_batch", _batch_args(4, requested=True), "call-3"))
    assert "error" not in second
    sess._exec_spawn_batch(second)
    assert coord.spawn.call_count == 8


def test_plain_coordinator_ignores_user_requested_count(monkeypatch):
    sess, coord, _ui = _make_session(
        monkeypatch,
        voice=False,
        settings={
            "coordinator.voice_max_agents_per_turn": 0,
            "coordinator.voice_max_requested_agents": 0,
        },
    )
    coord.spawn.side_effect = lambda **kwargs: _spawn_result(f"child-{coord.spawn.call_count}")

    batch = sess._prepare_tool(_tc("spawn_batch", _batch_args(4, requested=True)))
    assert "error" not in batch
    sess._exec_spawn_batch(batch)
    assert coord.spawn.call_count == 4


def test_voice_batch_sent_at_once_releases_every_kind_of_non_start(monkeypatch):
    """spawn_batch sends its creates concurrently (#1457); a child that raised,
    errored or came back without a ws_id still hands its slot back, and only
    the child that exists keeps one."""
    sess, coord, _ui = _make_session(
        monkeypatch,
        voice=True,
        settings={"coordinator.voice_max_agents_per_turn": 4},
    )

    def _spawn(**kwargs):
        msg = kwargs["initial_message"]
        if msg == "raises":
            raise RuntimeError("node vanished")
        if msg == "errors":
            return {"error": "upstream unavailable", "status": 502}
        if msg == "no-id":
            return {"name": "kid", "node_id": "node-1"}
        return _spawn_result("child-ok")

    coord.spawn.side_effect = _spawn
    batch = sess._prepare_tool(
        _tc(
            "spawn_batch",
            {
                "children": [
                    {"initial_message": "raises"},
                    {"initial_message": "errors"},
                    {"initial_message": "no-id"},
                    {"initial_message": "ok"},
                ]
            },
        )
    )
    _call_id, output = sess._exec_spawn_batch(batch)

    assert len(json.loads(output)["denied"]) == 3
    assert sess._voice_spawns_this_turn == 1


def test_voice_single_spawn_without_ws_id_releases_its_slot(monkeypatch):
    sess, coord, _ui = _make_session(monkeypatch, voice=True)
    coord.spawn.return_value = {"name": "kid", "node_id": "node-1"}

    item = sess._prepare_tool(_tc("spawn_workstream", {"initial_message": "one"}))
    _call_id, output = sess._exec_spawn_workstream(item)

    assert "no ws_id" in output
    assert sess._voice_spawns_this_turn == 0
