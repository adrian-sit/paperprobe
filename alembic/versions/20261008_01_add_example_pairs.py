"""add paper-conditioned question examples

Revision ID: 20261008_01
Revises: 20261005_03
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "20261008_01"
down_revision = "20261005_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "example_pairs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_title", sa.Text(), nullable=False),
        sa.Column("source_abstract_or_claims", sa.Text(), nullable=False),
        sa.Column("paper_embedding", Vector(768), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("embedding_model", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('own_criticism', 'openreview_review')",
            name="ck_example_pairs_source_type",
        ),
    )
    op.create_index("ix_example_pairs_source_type", "example_pairs", ["source_type"])


def downgrade() -> None:
    op.drop_index("ix_example_pairs_source_type", table_name="example_pairs")
    op.drop_table("example_pairs")
