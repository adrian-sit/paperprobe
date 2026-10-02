"""store complete extracted paper text

Revision ID: 20261002_01
Revises: 20260925_01
Create Date: 2026-10-02
"""

import sqlalchemy as sa

from alembic import op

revision = "20261002_01"
down_revision = "20260925_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("papers", sa.Column("full_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("papers", "full_text")
