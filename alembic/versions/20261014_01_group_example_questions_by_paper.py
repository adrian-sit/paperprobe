"""group example questions by uploaded source paper

Revision ID: 20261014_01
Revises: 20261008_01
"""

import sqlalchemy as sa
from alembic import op

revision = "20261014_01"
down_revision = "20261008_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("example_pairs", sa.Column("source_paper_id", sa.Uuid(), nullable=True))
    op.create_index("ix_example_pairs_source_paper_id", "example_pairs", ["source_paper_id"])


def downgrade() -> None:
    op.drop_index("ix_example_pairs_source_paper_id", table_name="example_pairs")
    op.drop_column("example_pairs", "source_paper_id")
