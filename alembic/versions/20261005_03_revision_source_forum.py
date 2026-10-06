"""track the source forum for every paper revision

Revision ID: 20261005_03
Revises: 20261005_02
"""

import sqlalchemy as sa
from alembic import op

revision = "20261005_03"
down_revision = "20261005_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("paper_versions", sa.Column("source_forum_id", sa.String(255), nullable=True))
    op.create_index("ix_paper_versions_source_forum_id", "paper_versions", ["source_forum_id"])
    op.execute(
        "UPDATE paper_versions SET source_forum_id = raw_metadata ->> 'forum_id' "
        "WHERE source_forum_id IS NULL AND raw_metadata ? 'forum_id'"
    )


def downgrade() -> None:
    op.drop_index("ix_paper_versions_source_forum_id", table_name="paper_versions")
    op.drop_column("paper_versions", "source_forum_id")
