"""Tests for alembic migration 086 (retire the voice memory scope).

Drives ``command.upgrade`` from a programmatic Alembic config against an
isolated SQLite database per test (the 061/085 harness pattern), then asserts:

* ``scope='voice'`` rows move to the global namespace (``scope='global'``,
  ``scope_id=''``) with their bodies intact;
* a ``(name, global)`` collision keeps the newer ``updated`` row and deletes
  the other, with ``memory_id`` as the deterministic tiebreak — in both
  directions, so a newer voice row replaces an older global one;
* two voice rows for the same name under different users collapse to the
  newest, because global has one namespace;
* non-voice scopes are untouched;
* frozen memory-index snapshots for voice workstreams are dropped so the next
  turn recaptures the interactive envelope, while other workstreams keep
  theirs;
* downgrade is a documented no-op and leaves the moved rows in place.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

_MIGRATIONS_DIR = str(
    Path(__file__).resolve().parent.parent / "turnstone" / "core" / "storage" / "migrations"
)


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _seed_memory(
    conn: sa.Connection,
    memory_id: str,
    name: str,
    scope: str,
    scope_id: str,
    updated: str = "2026-06-01T00:00:00",
    content: str | None = None,
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO structured_memories "
            "(memory_id, name, scope, scope_id, content, created, updated) "
            "VALUES (:memory_id, :name, :scope, :scope_id, :content, :created, :updated)"
        ),
        {
            "memory_id": memory_id,
            "name": name,
            "scope": scope,
            "scope_id": scope_id,
            "content": content if content is not None else f"content of {memory_id}",
            "created": "2026-06-01T00:00:00",
            "updated": updated,
        },
    )


def _seed_workstream(conn: sa.Connection, ws_id: str, persona: str | None) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO workstreams (ws_id, user_id, kind, persona, created, updated) "
            "VALUES (:ws_id, 'u1', 'coordinator', :persona, "
            "'2026-06-01T00:00:00', '2026-06-01T00:00:00')"
        ),
        {"ws_id": ws_id, "persona": persona},
    )


def _seed_snapshot(conn: sa.Connection, ws_id: str) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO memory_index_snapshots "
            "(ws_id, visibility_key, content, format_version, entry_count, char_count, "
            "captured_at) VALUES (:ws_id, :key, :content, 1, 1, 100, "
            "'2026-06-01T00:00:00')"
        ),
        {
            "ws_id": ws_id,
            "key": '[["voice", "u1"]]',
            "content": '<memory-index format="1" entries="1" project_id="">x</memory-index>',
        },
    )


def _all_memories(engine: sa.Engine) -> dict[str, tuple[str, str, str, str]]:
    """memory_id -> (name, scope, scope_id, content) for every surviving row."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT memory_id, name, scope, scope_id, content FROM structured_memories")
        ).fetchall()
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}


class TestMigration086:
    def test_moves_voice_rows_to_global_and_leaves_other_scopes_alone(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "086-move.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "085")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_memory(conn, "m-voice", "kettle", "voice", "u1")
                _seed_memory(conn, "m-user", "kettle", "user", "u1")
                _seed_memory(conn, "m-ws", "kettle", "workstream", "ws1")
                _seed_memory(conn, "m-coord", "kettle", "coordinator", "u1")
                _seed_memory(conn, "m-global", "other", "global", "")

            command.upgrade(cfg, "086")

            mems = _all_memories(engine)
            assert mems["m-voice"] == ("kettle", "global", "", "content of m-voice")
            assert mems["m-user"] == ("kettle", "user", "u1", "content of m-user")
            assert mems["m-ws"] == ("kettle", "workstream", "ws1", "content of m-ws")
            assert mems["m-coord"] == ("kettle", "coordinator", "u1", "content of m-coord")
            assert mems["m-global"] == ("other", "global", "", "content of m-global")
        finally:
            engine.dispose()

    def test_older_voice_row_yields_to_an_existing_global_row(self, tmp_path: Path) -> None:
        db_path = tmp_path / "086-global-wins.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "085")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_memory(
                    conn,
                    "m-voice",
                    "plan",
                    "voice",
                    "u1",
                    updated="2026-06-01T00:00:00",
                    content="stale voice plan",
                )
                _seed_memory(
                    conn,
                    "m-global",
                    "plan",
                    "global",
                    "",
                    updated="2026-06-02T00:00:00",
                    content="newer global plan",
                )

            command.upgrade(cfg, "086")

            mems = _all_memories(engine)
            assert "m-voice" not in mems
            assert mems["m-global"] == ("plan", "global", "", "newer global plan")
        finally:
            engine.dispose()

    def test_newer_voice_row_replaces_an_existing_global_row(self, tmp_path: Path) -> None:
        db_path = tmp_path / "086-voice-wins.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "085")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_memory(
                    conn,
                    "m-voice",
                    "plan",
                    "voice",
                    "u1",
                    updated="2026-06-02T00:00:00",
                    content="newer voice plan",
                )
                _seed_memory(
                    conn,
                    "m-global",
                    "plan",
                    "global",
                    "",
                    updated="2026-06-01T00:00:00",
                    content="stale global plan",
                )

            command.upgrade(cfg, "086")

            mems = _all_memories(engine)
            assert "m-global" not in mems
            assert mems["m-voice"] == ("plan", "global", "", "newer voice plan")
        finally:
            engine.dispose()

    def test_equal_updated_breaks_the_tie_on_memory_id(self, tmp_path: Path) -> None:
        db_path = tmp_path / "086-tie.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "085")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_memory(
                    conn,
                    "m-a",
                    "pinned",
                    "voice",
                    "u1",
                    updated="2026-06-03T00:00:00",
                )
                _seed_memory(
                    conn,
                    "m-b",
                    "pinned",
                    "global",
                    "",
                    updated="2026-06-03T00:00:00",
                )

            command.upgrade(cfg, "086")

            mems = _all_memories(engine)
            assert "m-a" not in mems
            assert mems["m-b"] == ("pinned", "global", "", "content of m-b")
        finally:
            engine.dispose()

    def test_same_name_across_voice_users_keeps_the_newest(self, tmp_path: Path) -> None:
        db_path = tmp_path / "086-two-users.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "085")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_memory(
                    conn,
                    "m-old",
                    "plan",
                    "voice",
                    "u1",
                    updated="2026-06-01T00:00:00",
                )
                _seed_memory(
                    conn,
                    "m-new",
                    "plan",
                    "voice",
                    "u2",
                    updated="2026-06-02T00:00:00",
                )
                _seed_memory(
                    conn,
                    "m-other",
                    "notes",
                    "voice",
                    "u1",
                    updated="2026-06-01T00:00:00",
                )

            command.upgrade(cfg, "086")

            mems = _all_memories(engine)
            assert "m-old" not in mems
            assert mems["m-new"] == ("plan", "global", "", "content of m-new")
            assert mems["m-other"] == ("notes", "global", "", "content of m-other")
        finally:
            engine.dispose()

    def test_drops_voice_workstream_index_snapshots_only(self, tmp_path: Path) -> None:
        db_path = tmp_path / "086-snapshots.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "085")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_workstream(conn, "voice-consumer", "voice")
                _seed_workstream(conn, "plain-coordinator", None)
                _seed_snapshot(conn, "voice-consumer")
                _seed_snapshot(conn, "plain-coordinator")

            command.upgrade(cfg, "086")

            with engine.connect() as conn:
                remaining = {
                    row[0]
                    for row in conn.execute(
                        sa.text("SELECT ws_id FROM memory_index_snapshots")
                    ).fetchall()
                }
            assert remaining == {"plain-coordinator"}
        finally:
            engine.dispose()

    def test_downgrade_is_a_noop(self, tmp_path: Path) -> None:
        db_path = tmp_path / "086-downgrade.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "085")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                _seed_memory(conn, "m-voice", "kettle", "voice", "u1")

            command.upgrade(cfg, "086")
            command.downgrade(cfg, "085")

            assert _all_memories(engine)["m-voice"] == (
                "kettle",
                "global",
                "",
                "content of m-voice",
            )
        finally:
            engine.dispose()
