"""Tests for alembic migration ``dt_voice_reports``.

Drives ``command.upgrade``/``downgrade`` against an isolated SQLite database
per test (the 083/084 harness pattern), then asserts the table, its state
CHECK and its coordinator/state index, that the live ``_schema`` definition
agrees with what the migration created, and that the pair is idempotent in
both directions.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from turnstone.core.storage._schema import voice_reports as voice_reports_table

_MIGRATIONS_DIR = str(
    Path(__file__).resolve().parent.parent / "turnstone" / "core" / "storage" / "migrations"
)


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _insert(engine: sa.Engine, report_id: str = "1" * 32, state: str = "pending") -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO voice_reports "
                "(report_id, coord_ws_id, child_ws_id, title, body, state, created, updated) "
                "VALUES (:rid, 'coord-1', 'child-1', 'Research', 'Done.', :state, 'now', 'now')"
            ),
            {"rid": report_id, "state": state},
        )


class TestMigration085:
    def test_creates_the_table_and_index(self, tmp_path: Path) -> None:
        db_path = tmp_path / "085-up.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "dt_voice_reports")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            inspector = sa.inspect(engine)
            assert "voice_reports" in inspector.get_table_names()
            columns = {c["name"] for c in inspector.get_columns("voice_reports")}
            assert columns == {
                "report_id",
                "coord_ws_id",
                "child_ws_id",
                "title",
                "body",
                "state",
                "created",
                "updated",
            }
            indexes = {
                ix["name"]: ix["column_names"] for ix in inspector.get_indexes("voice_reports")
            }
            assert indexes["idx_voice_reports_coord_state"] == ["coord_ws_id", "state"]
            _insert(engine)
            with engine.connect() as conn:
                assert (
                    conn.execute(sa.text("SELECT state FROM voice_reports")).scalar() == "pending"
                )
        finally:
            engine.dispose()

    def test_table_matches_the_live_schema(self, tmp_path: Path) -> None:
        # No generated parity test exists for new tables; this pins the two
        # hand-maintained definitions together at the point they are added.
        db_path = tmp_path / "085-schema.db"
        command.upgrade(_alembic_cfg(db_path), "dt_voice_reports")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            migrated = {c["name"] for c in sa.inspect(engine).get_columns("voice_reports")}
        finally:
            engine.dispose()
        assert migrated == {c.name for c in voice_reports_table.columns}

    def test_rejects_an_unknown_state(self, tmp_path: Path) -> None:
        db_path = tmp_path / "085-check.db"
        command.upgrade(_alembic_cfg(db_path), "dt_voice_reports")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with pytest.raises(sa.exc.IntegrityError):
                _insert(engine, state="finished")
        finally:
            engine.dispose()

    def test_is_idempotent_in_both_directions(self, tmp_path: Path) -> None:
        db_path = tmp_path / "085-idem.db"
        cfg = _alembic_cfg(db_path)
        command.upgrade(cfg, "dt_voice_persona")
        # A concurrent/manual install may already own the table; upgrading must
        # not fail on the duplicate DDL.
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "CREATE TABLE voice_reports (report_id TEXT PRIMARY KEY, "
                        "coord_ws_id TEXT NOT NULL, child_ws_id TEXT NOT NULL, "
                        "title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '', "
                        "state TEXT NOT NULL DEFAULT 'pending', "
                        "created TEXT NOT NULL, updated TEXT NOT NULL)"
                    )
                )
        finally:
            engine.dispose()
        command.upgrade(cfg, "dt_voice_reports")

        command.downgrade(cfg, "dt_voice_persona")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            assert "voice_reports" not in sa.inspect(engine).get_table_names()
        finally:
            engine.dispose()

        command.upgrade(cfg, "dt_voice_reports")
        command.upgrade(cfg, "dt_voice_reports")  # already applied — a no-op
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            assert "voice_reports" in sa.inspect(engine).get_table_names()
        finally:
            engine.dispose()
