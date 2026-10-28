"""Join upstream's migration chain and this deployment's fork migrations.

The fork's migrations (``dt_*``) carry their own revision ids so upstream can
keep numbering 084, 085, ... without colliding with them; both chains descend
from 083.  Upstream runs ``alembic upgrade head``, which needs one head, so
this empty revision merges the two.  When upstream adds a migration on top of
its own head, the next port adds another merge revision the same way.

Revision ID: dt_merge_084
Revises: 084, dt_voice_persona
Create Date: 2026-10-28
"""

from __future__ import annotations

revision = "dt_merge_084"
down_revision = ("084", "dt_voice_persona")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
