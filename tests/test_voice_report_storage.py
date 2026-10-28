"""Tests for the voice-report storage CRUD (both backends via the ``db`` fixture).

A voice session never blocks on a child agent; a finished child leaves a report
row behind (``turnstone.core.storage.migrations.versions.dt_voice_reports``)
that survives the session.  These tests pin the row shape, the state filter and
the storage-edge size guards.
"""

from __future__ import annotations

from typing import Any

import pytest

from turnstone.core.storage._utils import (
    VOICE_REPORT_BODY_MAX_BYTES,
    VOICE_REPORT_TITLE_MAX,
)
from turnstone.core.voice_config import VOICE_REPORT_STATES


def _create(db: Any, **overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "coord_ws_id": "coord-1",
        "child_ws_id": "child-1",
        "title": "Research summary",
        "body": "All done.",
    }
    fields.update(overrides)
    return db.create_voice_report(**fields)  # type: ignore[no-any-return]


class TestVoiceReportCreate:
    def test_defaults_and_roundtrip(self, db) -> None:
        created = _create(db)
        assert len(created["report_id"]) == 32
        int(created["report_id"], 16)  # uuid hex
        assert created["state"] == "pending"
        assert created["coord_ws_id"] == "coord-1"
        assert created["child_ws_id"] == "child-1"
        assert created["title"] == "Research summary"
        assert created["body"] == "All done."
        assert created["created"] == created["updated"]

        rows = db.list_voice_reports("coord-1", None)
        assert rows == [created]

    def test_explicit_id_and_state(self, db) -> None:
        created = _create(db, report_id="a" * 32, state="offered")
        assert created["report_id"] == "a" * 32
        assert created["state"] == "offered"

    def test_requires_both_workstream_ids(self, db) -> None:
        with pytest.raises(ValueError, match="coord_ws_id"):
            _create(db, coord_ws_id="")
        with pytest.raises(ValueError, match="child_ws_id"):
            _create(db, child_ws_id="")

    def test_rejects_an_unknown_state(self, db) -> None:
        with pytest.raises(ValueError, match="state"):
            _create(db, state="finished")

    def test_rejects_an_oversized_title(self, db) -> None:
        with pytest.raises(ValueError, match="title"):
            _create(db, title="x" * (VOICE_REPORT_TITLE_MAX + 1))

    def test_rejects_an_oversized_body(self, db) -> None:
        with pytest.raises(ValueError, match="body"):
            _create(db, body="x" * (VOICE_REPORT_BODY_MAX_BYTES + 1))

    def test_body_cap_is_utf8_bytes(self, db) -> None:
        # A multi-byte codepoint must count at its wire size, not one char.
        with pytest.raises(ValueError, match="body"):
            _create(db, body="é" * (VOICE_REPORT_BODY_MAX_BYTES // 2 + 1))
        # Exactly at the cap is admitted.
        created = _create(db, body="é" * (VOICE_REPORT_BODY_MAX_BYTES // 2))
        assert created["body"]


class TestVoiceReportList:
    def test_scoped_to_the_coordinator(self, db) -> None:
        _create(db, coord_ws_id="coord-1")
        _create(db, coord_ws_id="coord-2", child_ws_id="child-2")
        assert [r["child_ws_id"] for r in db.list_voice_reports("coord-1", None)] == ["child-1"]
        assert [r["child_ws_id"] for r in db.list_voice_reports("coord-2", None)] == ["child-2"]

    def test_filters_by_state(self, db) -> None:
        _create(db, report_id="1" * 32, state="pending")
        _create(db, report_id="2" * 32, state="offered")
        _create(db, report_id="3" * 32, state="read")
        _create(db, report_id="4" * 32, state="dismissed")
        assert [
            r["report_id"] for r in db.list_voice_reports("coord-1", ("pending", "offered"))
        ] == [
            "1" * 32,
            "2" * 32,
        ]
        assert [r["report_id"] for r in db.list_voice_reports("coord-1", VOICE_REPORT_STATES)] == [
            "1" * 32,
            "2" * 32,
            "3" * 32,
            "4" * 32,
        ]
        assert db.list_voice_reports("coord-1", ()) == []

    def test_rejects_an_unknown_state_filter(self, db) -> None:
        with pytest.raises(ValueError, match="state"):
            db.list_voice_reports("coord-1", ("pending", "finished"))


class TestVoiceReportUpdateState:
    def test_moves_state_and_refreshes_updated(self, db) -> None:
        created = _create(db)
        assert db.update_voice_report_state(created["report_id"], "offered") is True
        row = db.list_voice_reports("coord-1", ("offered",))[0]
        assert row["state"] == "offered"
        assert row["updated"] >= row["created"]
        assert db.update_voice_report_state(created["report_id"], "read") is True
        assert db.list_voice_reports("coord-1", ("read",))[0]["state"] == "read"
        assert db.update_voice_report_state(created["report_id"], "dismissed") is True
        assert db.list_voice_reports("coord-1", ("dismissed",))[0]["state"] == "dismissed"

    def test_missing_report_returns_false(self, db) -> None:
        assert db.update_voice_report_state("f" * 32, "read") is False

    def test_rejects_an_unknown_state(self, db) -> None:
        created = _create(db)
        with pytest.raises(ValueError, match="state"):
            db.update_voice_report_state(created["report_id"], "finished")


class TestVoiceReportLifecycle:
    def test_deleting_the_coordinator_deletes_its_queue(self, db) -> None:
        _create(db)
        db.register_workstream("coord-1", node_id="console", name="ws-c", user_id="u1")
        assert db.delete_workstream("coord-1") is True
        assert db.list_voice_reports("coord-1", None) == []

    def test_deleting_a_child_keeps_its_report(self, db) -> None:
        # The coordinator still owes the user a mention of the finished turn,
        # even if the child row is gone by the time they next speak.
        created = _create(db)
        db.register_workstream("child-1", node_id="n1", name="ws-k", user_id="u1")
        assert db.delete_workstream("child-1") is True
        assert db.list_voice_reports("coord-1", None) == [created]
