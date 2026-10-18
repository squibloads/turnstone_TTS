"""Environment notes: operator-supplied host facts in every agent's prompt.

A voice session once started four agents against one host path.  Two found it
under the deployment's read-only host mount, one looked only at the container
path, concluded it was missing, and silently read an old developer worktree
instead.  ``session.environment_notes`` gives an operator one place to state
deployment facts such as where the host filesystem is mounted, so every agent
starts from the same picture of the machine it is on.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests._session_helpers import make_session
from tests.test_voice_docs import heading_slugs
from turnstone.core.settings_registry import SETTINGS_DOCS_URL, validate_key
from turnstone.core.workstream import WorkstreamKind
from turnstone.prompts import ClientType, SessionContext, compose_system_message

if TYPE_CHECKING:
    from collections.abc import Callable

_ROOT = Path(__file__).resolve().parents[1]
_PROMPTS = _ROOT / "turnstone" / "prompts"
_SETTINGS_DOC = _ROOT / "docs" / "settings.md"
_NO_TOOLS: frozenset[str] = frozenset()
_CONTEXT = SessionContext(
    current_datetime="2026-01-01T00:00",
    timezone="UTC",
    username="test",
)
_NOTES = "The host filesystem is mounted read-only at /host; a host path /x is /host/x here."


def _composed(environment_notes: str = "") -> str:
    return compose_system_message(
        ClientType.WEB, _CONTEXT, _NO_TOOLS, environment_notes=environment_notes
    )


def _system_text(session: Any) -> str:
    """The identity prefix: the system message(s) a provider receives first."""
    return "\n".join(str(message["content"]) for message in session.system_messages)


# -- prompt composition -------------------------------------------------------


def test_empty_notes_leave_the_prompt_unchanged() -> None:
    baseline = _composed()
    assert _composed(environment_notes="") == baseline
    assert _composed(environment_notes="  \n\t ") == baseline
    assert "## Environment" not in baseline


def test_notes_are_appended_once_under_an_environment_heading() -> None:
    text = _composed(environment_notes=_NOTES)
    assert text.count(_NOTES) == 1
    assert text.count("## Environment") == 1
    assert f"## Environment\n\n{_NOTES}" in text


def test_new_session_prompt_carries_the_operator_notes(
    tmp_db: str, make_config_store: Callable[..., Any]
) -> None:
    session = make_session(
        config_store=make_config_store(**{"session.environment_notes": _NOTES}),
    )
    assert _system_text(session).count(_NOTES) == 1


def test_spawned_child_prompt_carries_the_operator_notes(
    tmp_db: str, make_config_store: Callable[..., Any]
) -> None:
    """A spawned child is an interactive session built by the node factory.

    The child reads the same ConfigStore, so the notes describe the host for
    the child as well as for its coordinator; they are not inherited from the
    parent's persisted workstream config.
    """
    child = make_session(
        config_store=make_config_store(**{"session.environment_notes": _NOTES}),
        kind=WorkstreamKind.INTERACTIVE,
        parent_ws_id="parent-workstream",
    )
    assert _system_text(child).count(_NOTES) == 1


def test_empty_stored_value_adds_nothing(
    tmp_db: str, make_config_store: Callable[..., Any]
) -> None:
    session = make_session(
        config_store=make_config_store(**{"session.environment_notes": "   "}),
    )
    assert "## Environment" not in _system_text(session)


# -- settings registry and docs ----------------------------------------------


def test_environment_notes_setting_is_registered() -> None:
    defn = validate_key("session.environment_notes")
    assert defn.type == "str"
    assert defn.default == ""
    assert defn.section == "session"
    assert defn.help
    assert defn.reference_url == SETTINGS_DOCS_URL + "#environment-notes"


def test_environment_notes_docs_anchor_exists() -> None:
    slugs = heading_slugs(_SETTINGS_DOC.read_text(encoding="utf-8"))
    assert "environment-notes" in slugs


# -- persona and base-prompt wording -----------------------------------------


def test_voice_persona_carries_paths_verbatim_and_forbids_substitution() -> None:
    text = (_PROMPTS / "personas" / "voice.md").read_text(encoding="utf-8")
    assert "path or location the user named" in text
    assert "verbatim" in text
    assert "cannot be found" in text
    assert "never reading a different directory as a substitute" in text


def test_engineer_prompt_reports_a_missing_path_instead_of_substituting() -> None:
    text = (_PROMPTS / "personas" / "engineer.md").read_text(encoding="utf-8")
    assert "When a request names a path, use that path" in text
    assert "never read a different directory as a substitute" in text
