"""index section-linked extraction provenance

Revision ID: 20261005_01
Revises: 20261002_02
"""

from alembic import op

revision = "20261005_01"
down_revision = "20261002_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_extracted_fields_section_id", "extracted_fields", ["section_id"])


def downgrade() -> None:
    op.drop_index("ix_extracted_fields_section_id", table_name="extracted_fields")
