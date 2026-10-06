"""store authors on each paper revision

Revision ID: 20261005_02
Revises: 20261005_01
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261005_02"
down_revision = "20261005_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "paper_versions",
        sa.Column("authors", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("paper_versions", "authors")
