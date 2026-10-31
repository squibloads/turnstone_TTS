"""Join upstream's 085-086 migrations with this deployment's fork migrations.

Upstream 1.9.2 adds 085 (mcp_oauth_pending_binding) and 086
(files_read_permission) on top of 084, so with ``dt_merge_084`` the graph has
two heads again: upstream's 086 and the fork's merge.  ``alembic upgrade
head`` needs one, so this empty revision merges the two, as ``dt_merge_084``
did for 084.

From here on, a new fork migration revises the single head of the branch it
lands on (this revision, until something revises it).  Never change the
``down_revision`` of a revision a database may already be at: re-pointing
``dt_merge_084``'s second parent was safe only while the stacked voice
branches were unreleased.  On a database at ``dt_merge_084`` or here, a
re-parented migration is already an ancestor of the applied revision, so
``alembic upgrade head`` skips it silently while a fresh database still
upgrades cleanly.  When the cascade leaves two heads on a later branch, or
upstream adds a migration on top of 086, add another empty ``dt_merge_*``
revision instead.

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
