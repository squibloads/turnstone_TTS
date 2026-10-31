"""Join upstream's 085-086 migrations with this deployment's fork migrations.

Upstream 1.9.2 adds 085 (mcp_oauth_pending_binding) and 086
(files_read_permission) on top of 084, so with ``dt_merge_084`` the graph has
two heads again: upstream's 086 and the fork's merge.  ``alembic upgrade
head`` needs one, so this empty revision merges the two, as ``dt_merge_084``
did for 084.  Later fork migrations keep re-pointing ``dt_merge_084``'s second
parent at their newest ``dt_*`` revision, so this one stays the single head;
when upstream adds a migration on top of 086, the next port adds another
merge revision the same way.

Revision ID: dt_merge_086
Revises: 086, dt_merge_084
Create Date: 2026-10-30
"""

from __future__ import annotations

revision = "dt_merge_086"
down_revision = ("086", "dt_merge_084")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
