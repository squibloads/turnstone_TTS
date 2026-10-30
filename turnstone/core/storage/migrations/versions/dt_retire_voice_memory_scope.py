"""Retire the ``voice`` memory scope and fold its rows into ``global``.

A voice session is a coordinator created with the ``voice`` persona, but it no
longer has a private memory namespace: it uses the same scopes as an
interactive session (``global``, ``workstream``, ``user``, ``project``) and can
never reach ``coordinator``.  Rows written under the old ``voice`` scope must
therefore move somewhere the new envelope reads.

The destination is the global namespace (``scope='global'``, ``scope_id=''``).
Global is the only shared identity that exists without a workstream, user or
project witness: a voice row's ``scope_id`` was the acting user, and folding
into ``user`` would silently reassign a namespace the user may not have meant
to publish to every workstream.

Collisions follow migration 061's rule: keep the most recently ``updated`` row
(ties broken by ``memory_id`` for determinism) and delete the other.  ``updated``
is a fixed-width ``%Y-%m-%dT%H:%M:%S`` string, so lexicographic order is
chronological.  The rule cuts both ways here — a newer voice row replaces an
older existing global row, because global is the destination identity.

A voice workstream's frozen ``memory_index_snapshots`` row was captured from
the retired envelope.  It is deleted so the next admitted turn recaptures the
interactive envelope; leaving it in place would replay a prompt-visible index
that no longer describes any reachable scope.

``downgrade()`` is a documented no-op.  The data transform cannot be inverted:
the old scope_id was discarded by design, deduped rows are gone, and a global
row cannot say whether it was born global or promoted from voice.  There is no
schema delta to undo either.

Revision ID: dt_retire_voice_memory_scope
Revises: dt_voice_reports
Create Date: 2026-10-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "dt_retire_voice_memory_scope"
down_revision = "dt_voice_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Drop voice rows that lose to an existing global row of the same name.
    #    A newer voice row survives this step; the next step deletes the older
    #    global row it beats.
    conn.execute(
        sa.text(
            "DELETE FROM structured_memories "
            "WHERE scope = 'voice' "
            "AND EXISTS ("
            "  SELECT 1 FROM structured_memories g "
            "  WHERE g.scope = 'global' "
            "    AND g.scope_id = '' "
            "    AND g.name = structured_memories.name "
            "    AND (g.updated > structured_memories.updated "
            "         OR (g.updated = structured_memories.updated "
            "             AND g.memory_id > structured_memories.memory_id))"
            ")"
        )
    )

    # 2. Drop an existing global row when a surviving voice row is newer.  This
    #    makes the collision rule symmetric: the newest row wins regardless of
    #    which side of the move it started on.
    conn.execute(
        sa.text(
            "DELETE FROM structured_memories "
            "WHERE scope = 'global' "
            "  AND scope_id = '' "
            "  AND EXISTS ("
            "  SELECT 1 FROM structured_memories v "
            "  WHERE v.scope = 'voice' "
            "    AND v.name = structured_memories.name "
            "    AND (v.updated > structured_memories.updated "
            "         OR (v.updated = structured_memories.updated "
            "             AND v.memory_id > structured_memories.memory_id))"
            ")"
        )
    )

    # 3. Voice rows from different users collapse onto one global identity;
    #    keep only the newest per name.  Steps 1-2 guarantee the survivors no
    #    longer collide with a global row.
    conn.execute(
        sa.text(
            "DELETE FROM structured_memories "
            "WHERE scope = 'voice' "
            "AND EXISTS ("
            "  SELECT 1 FROM structured_memories v2 "
            "  WHERE v2.scope = 'voice' "
            "    AND v2.name = structured_memories.name "
            "    AND v2.memory_id != structured_memories.memory_id "
            "    AND (v2.updated > structured_memories.updated "
            "         OR (v2.updated = structured_memories.updated "
            "             AND v2.memory_id > structured_memories.memory_id))"
            ")"
        )
    )

    # 4. Re-key the survivors into the global namespace.
    conn.execute(
        sa.text(
            "UPDATE structured_memories SET scope = 'global', scope_id = '' WHERE scope = 'voice'"
        )
    )

    # 5. A voice workstream's frozen index describes the retired envelope;
    #    dropping it lets the next admitted turn capture the interactive one.
    conn.execute(
        sa.text(
            "DELETE FROM memory_index_snapshots "
            "WHERE ws_id IN ("
            "  SELECT ws_id FROM workstreams "
            "  WHERE LOWER(TRIM(COALESCE(persona, ''))) = 'voice'"
            ")"
        )
    )


def downgrade() -> None:
    # Irreversible data transform: the old voice scope ids were folded away by
    # design and collision losers are deleted.  No schema delta exists, and
    # re-splitting promoted rows would invent provenance.  See the module
    # docstring.
    pass
