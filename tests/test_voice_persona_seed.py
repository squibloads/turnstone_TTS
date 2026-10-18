"""Tests for the voice persona seed (migration 084 and ``personas/voice.md``).

A voice session is a coordinator created with the ``voice`` persona (see
``turnstone.core.voice_config``).  The persona shelf is data, so these tests
drive the real migration against an isolated SQLite database and then resolve
through the same code paths a server uses: ``resolve_persona_for_kind`` for
eligibility and ``snapshot_from_persona`` for the BASE text a workstream
freezes at creation.  The prompt file is checked for the voice-specific rules
that keep a spoken turn brief and stop an aside from starting an agent.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from turnstone.core.personas import resolve_persona_for_kind, snapshot_from_persona
from turnstone.core.storage._sqlite import SQLiteBackend
from turnstone.core.voice_config import VOICE_PERSONA
from turnstone.prompts import load_persona_prompt

_MIGRATIONS_DIR = str(
    Path(__file__).resolve().parent.parent / "turnstone" / "core" / "storage" / "migrations"
)
_VOICE_PROMPT = (
    Path(__file__).resolve().parent.parent / "turnstone" / "prompts" / "personas" / "voice.md"
)


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _resolve(db_path: Path, name: str, kind: str) -> tuple[dict[str, Any] | None, str]:
    """Resolve through the real storage backend, as the create paths do."""
    with contextlib.closing(SQLiteBackend(str(db_path), create_tables=False)) as storage:
        return resolve_persona_for_kind(storage, name, kind)


def _persona_names(db_path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            return {
                str(row[0]) for row in conn.execute(sa.text("SELECT name FROM personas")).fetchall()
            }
    finally:
        engine.dispose()


class TestVoicePersonaSeed:
    def test_seeded_as_an_unrestricted_coordinator(self, tmp_path: Path) -> None:
        db_path = tmp_path / "voice-seed.db"
        command.upgrade(_alembic_cfg(db_path), "084")

        row, err = _resolve(db_path, VOICE_PERSONA, "coordinator")
        assert err == ""
        assert row is not None
        assert row["name"] == "voice"
        assert row["display_name"] == "Voice"
        assert row["description"]
        # File-backed built-in: the prose lives in the repo, not the row.
        assert row["base_prompt"] is None
        assert row["base_prompt_file"] == "voice.md"
        # Voice keeps every coordinator capability: unrestricted tools, MCP
        # and memory on, exactly like orchestrator.
        assert row["tool_allowlist"] is None
        assert row["mcp_enabled"] is True
        assert row["memory_enabled"] is True
        assert row["applies_to_kinds"] == ["coordinator"]
        # A second coordinator persona, never the kind default.
        assert row["is_default"] is False
        assert row["enabled"] is True

    def test_refused_for_interactive(self, tmp_path: Path) -> None:
        db_path = tmp_path / "voice-kind.db"
        command.upgrade(_alembic_cfg(db_path), "084")

        row, err = _resolve(db_path, "voice", "interactive")
        assert row is None
        assert "does not apply to kind 'interactive'" in err
        # The self-correction list offers only interactive personas.
        available = err.split("Available for interactive:", 1)[1]
        assert "voice" not in available

    def test_snapshot_freezes_the_voice_prompt(self, tmp_path: Path) -> None:
        db_path = tmp_path / "voice-snapshot.db"
        command.upgrade(_alembic_cfg(db_path), "084")

        row, err = _resolve(db_path, "voice", "coordinator")
        assert err == "" and row is not None
        snap = snapshot_from_persona(row)
        assert snap.name == "voice"
        assert snap.prompt == load_persona_prompt("voice.md")
        assert snap.tools is None
        assert snap.mcp is True
        assert snap.memory is True


class TestVoicePromptFile:
    def test_is_prose_without_markdown_tables(self) -> None:
        assert _VOICE_PROMPT.is_file()
        text = _VOICE_PROMPT.read_text(encoding="utf-8")
        assert text.strip()
        # Everything the persona writes is spoken; the base prompt models that
        # by staying plain prose.  A table (or any table syntax) has no place.
        assert "|" not in text
        for line in text.splitlines():
            stripped = line.lstrip()
            assert not stripped.startswith(("#", "- ", "* ")), f"markdown line: {line!r}"

    def test_carries_the_voice_rules(self) -> None:
        text = _VOICE_PROMPT.read_text(encoding="utf-8")
        for rule in (
            "Everything you write is spoken aloud",
            "One to three short sentences",
            "no lists, tables, headings",
            "Acknowledge what the user said first",
            "only when the user explicitly asks for work",
            '"I was wondering" are conversation, not a request',
            "at most one agent per turn",
            "refused spawn",
            "Leave the model empty",
            "under 800 characters",
            "do not read its report unprompted",
            "Never cancel an agent just because a wait timed out",
            "Never claim that work is done",
        ):
            assert rule in text, f"voice prompt is missing: {rule!r}"


class TestVoicePersonaMigration:
    def test_upgrade_skips_an_existing_voice_row(self, tmp_path: Path) -> None:
        # Idempotency: an operator row that already owns the slug is the seed's
        # collision case.  The migration must leave it untouched, and its
        # downgrade must not delete it (it is not the built-in).
        db_path = tmp_path / "voice-existing.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "083")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO personas (persona_id, name, display_name, description, "
                        "base_prompt, base_prompt_file, tool_allowlist, mcp_enabled, "
                        "memory_enabled, applies_to_kinds, is_default, enabled, org_id, "
                        "created_by, created, updated) "
                        "VALUES ('op-voice', 'voice', 'My Voice', 'operator', 'Mine.', NULL, "
                        "NULL, 0, 1, '[\"coordinator\"]', 0, 1, '', 'u1', "
                        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
                    )
                )
        finally:
            engine.dispose()

        command.upgrade(cfg, "084")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    sa.text(
                        "SELECT persona_id, base_prompt, display_name FROM personas "
                        "WHERE name = 'voice'"
                    )
                ).fetchall()
        finally:
            engine.dispose()
        assert rows == [("op-voice", "Mine.", "My Voice")]

        command.downgrade(cfg, "083")
        assert "voice" in _persona_names(db_path)

    def test_downgrade_removes_the_unmodified_seed(self, tmp_path: Path) -> None:
        db_path = tmp_path / "voice-downgrade.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "084")
        assert "voice" in _persona_names(db_path)

        command.downgrade(cfg, "083")
        names = _persona_names(db_path)
        assert "voice" not in names
        # Other built-ins are untouched by 084's downgrade.
        assert {"engineer", "orchestrator", "scribe"} <= names

    def test_downgrade_keeps_an_edited_seed_row(self, tmp_path: Path) -> None:
        # An operator may customise a built-in row (the row keeps its file
        # source but gains inline prose, or edits the label).  Downgrade must
        # not throw that work away.
        db_path = tmp_path / "voice-edited.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "084")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text("UPDATE personas SET base_prompt = 'Mine now.' WHERE name = 'voice'")
                )
        finally:
            engine.dispose()

        command.downgrade(cfg, "083")
        assert "voice" in _persona_names(db_path)
