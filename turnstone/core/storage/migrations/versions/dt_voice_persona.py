"""Seed the voice coordinator persona.

A voice session is a coordinator created with the ``voice`` persona (pinned in
``turnstone.core.voice_config``).  Its spoken rules live in the prompt file, so
the shelf row mirrors ``orchestrator``'s live envelope — unrestricted tools,
MCP and memory on — and differs only in where the BASE text comes from.

This migration writes the slug and the prompt filename out rather than
importing them from live code: a migration is immutable history, so a later
rename in ``voice_config`` or a move of the prompt file must not silently
change what this revision seeded (the same reason 063 freezes its backfill
prompts).  The statements are plain SQLAlchemy text, so the 063 pattern works
unchanged on SQLite and PostgreSQL; the JSON-shaped columns are TEXT.

Idempotent in both directions.  An existing row that already owns the slug or
the built-in id is left untouched — it is an operator's persona, not this
seed.  Downgrade deletes the row only while every seeded column still matches;
once an operator edits or archives it, the row is theirs and survives.

Revision ID: dt_voice_persona
Revises: 083
Create Date: 2026-10-18
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "dt_voice_persona"
down_revision = "083"
branch_labels = None
depends_on = None

_NAME = "voice"
_PERSONA_ID = "builtin-voice"
_DISPLAY_NAME = "Voice"
_DESCRIPTION = "The spoken coordinator: brief answers, one agent per turn, reports on request."
_BASE_PROMPT_FILE = "voice.md"
_APPLIES_TO_KINDS = '["coordinator"]'


def upgrade() -> None:
    conn = op.get_bind()
    existing = conn.execute(
        sa.text("SELECT 1 FROM personas WHERE name = :name OR persona_id = :pid"),
        {"name": _NAME, "pid": _PERSONA_ID},
    ).first()
    if existing is not None:
        return
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        sa.text(
            "INSERT INTO personas (persona_id, name, display_name, description, "
            "base_prompt, base_prompt_file, tool_allowlist, mcp_enabled, memory_enabled, "
            "applies_to_kinds, is_default, enabled, org_id, created_by, created, updated) "
            "VALUES (:pid, :name, :dname, :desc, NULL, :pfile, NULL, 1, 1, :kinds, 0, 1, "
            "'', '', :now, :now)"
        ),
        {
            "pid": _PERSONA_ID,
            "name": _NAME,
            "dname": _DISPLAY_NAME,
            "desc": _DESCRIPTION,
            "pfile": _BASE_PROMPT_FILE,
            "kinds": _APPLIES_TO_KINDS,
            "now": now,
        },
    )


def downgrade() -> None:
    # Delete only the untouched built-in.  Every seeded column is part of the
    # comparison: a changed label, description, prompt override, lever, kind
    # set or enabled flag means an operator has made the row their own, and a
    # downgrade must not discard that work.
    op.get_bind().execute(
        sa.text(
            "DELETE FROM personas "
            "WHERE persona_id = :pid AND name = :name AND display_name = :dname "
            "AND description = :desc AND base_prompt IS NULL AND base_prompt_file = :pfile "
            "AND tool_allowlist IS NULL AND mcp_enabled = 1 AND memory_enabled = 1 "
            "AND applies_to_kinds = :kinds AND is_default = 0 AND enabled = 1 "
            "AND org_id = '' AND created_by = ''"
        ),
        {
            "pid": _PERSONA_ID,
            "name": _NAME,
            "dname": _DISPLAY_NAME,
            "desc": _DESCRIPTION,
            "pfile": _BASE_PROMPT_FILE,
            "kinds": _APPLIES_TO_KINDS,
        },
    )
