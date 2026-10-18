"""Store finished child agents' reports for voice coordinators.

A voice session is a coordinator created with the ``voice`` persona.  Its turn
is spoken, so it must never block inside ``wait_for_workstream``: a child that
finishes (``idle`` or ``error``) leaves a row here instead, and the report is
offered on the user's next genuine turn.  The table lives in the database, not
in memory, so a report survives a session resume or console restart.

``state`` walks the four values in ``turnstone.core.voice_config``
(``VOICE_REPORT_STATES``); the CHECK spells them out rather than importing the
live tuple, because a migration is immutable history and a later vocabulary
change must not silently rewrite what this revision installed.  The index
serves the offer path's one query: a coordinator's reports by state.

Idempotent in both directions.  Upgrade skips the DDL when an operator already
has the table (a partial/manual install); downgrade only drops what it created.

Revision ID: 085
Revises: 084
Create Date: 2026-10-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "085"
down_revision = "084"
branch_labels = None
depends_on = None

_TABLE = "voice_reports"
_INDEX = "idx_voice_reports_coord_state"


def upgrade() -> None:
    conn = op.get_bind()
    if sa.inspect(conn).has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("report_id", sa.Text(), nullable=False),
        sa.Column("coord_ws_id", sa.Text(), nullable=False),
        sa.Column("child_ws_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("created", sa.Text(), nullable=False),
        sa.Column("updated", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("report_id"),
        sa.CheckConstraint(
            "state IN ('pending', 'offered', 'read', 'dismissed')",
            name="ck_voice_reports_state",
        ),
    )
    op.create_index(_INDEX, _TABLE, ["coord_ws_id", "state"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        return
    # A partial/manual install can carry the table without this revision's
    # index; dropping a named index that does not exist would fail the whole
    # downgrade after the table is already gone from the operator's intent.
    if _INDEX in {ix["name"] for ix in inspector.get_indexes(_TABLE)}:
        op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
