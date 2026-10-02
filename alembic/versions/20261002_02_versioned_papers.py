"""store forum revisions, reviews, and version-scoped text

Revision ID: 20261002_02
Revises: 20261002_01
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261002_02"
down_revision = "20261002_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column("papers", sa.Column("forum_id", sa.String(255), nullable=True))
    op.execute("UPDATE papers SET forum_id = raw_metadata ->> 'forum_id' WHERE raw_metadata ? 'forum_id'")
    op.create_index("ix_papers_forum_id", "papers", ["forum_id"], unique=True)

    op.create_table(
        "paper_versions",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("paper_id", sa.Uuid(), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_key", sa.String(255), nullable=False),
        sa.Column("version_timestamp", sa.BigInteger(), nullable=True),
        sa.Column("is_latest", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("abstract", sa.Text(), nullable=True),
        sa.Column("paper_text", sa.Text(), nullable=True),
        sa.Column("raw_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("paper_id", "version_key", name="uq_paper_versions_paper_key"),
    )
    op.execute("""
        INSERT INTO paper_versions (paper_id, version_key, is_latest, title, abstract, paper_text, raw_metadata)
        SELECT p.id, 'legacy', true, p.title,
               (SELECT s.content FROM paper_sections s WHERE s.paper_id = p.id AND lower(s.heading) = 'abstract'
                ORDER BY s.position LIMIT 1),
               p.full_text, p.raw_metadata
        FROM papers p
    """)
    op.create_index("uq_paper_versions_one_latest", "paper_versions", ["paper_id"], unique=True,
                    postgresql_where=sa.text("is_latest IS TRUE"))

    op.add_column("paper_sections", sa.Column("paper_version_id", sa.Uuid(), nullable=True))
    op.execute("""
        UPDATE paper_sections s SET paper_version_id = v.id
        FROM paper_versions v WHERE v.paper_id = s.paper_id AND v.version_key = 'legacy'
    """)
    op.alter_column("paper_sections", "paper_version_id", nullable=False)
    op.create_foreign_key("fk_paper_sections_version", "paper_sections", "paper_versions",
                          ["paper_version_id"], ["id"], ondelete="CASCADE")
    op.drop_constraint("uq_paper_sections_paper_position", "paper_sections", type_="unique")
    op.create_unique_constraint("uq_paper_sections_version_position", "paper_sections",
                                ["paper_version_id", "position"])

    op.add_column("extracted_fields", sa.Column("paper_version_id", sa.Uuid(), nullable=True))
    op.execute("""
        UPDATE extracted_fields e SET paper_version_id = v.id
        FROM paper_versions v WHERE v.paper_id = e.paper_id AND v.version_key = 'legacy'
    """)
    op.alter_column("extracted_fields", "paper_version_id", nullable=False)
    op.create_foreign_key("fk_extracted_fields_version", "extracted_fields", "paper_versions",
                          ["paper_version_id"], ["id"], ondelete="CASCADE")
    op.drop_constraint("extracted_fields_paper_id_fkey", "extracted_fields", type_="foreignkey")
    op.drop_column("extracted_fields", "paper_id")

    op.add_column("questions", sa.Column("paper_version_id", sa.Uuid(), nullable=True))
    op.execute("""
        UPDATE questions q SET paper_version_id = v.id
        FROM paper_versions v WHERE v.paper_id = q.paper_id AND v.version_key = 'legacy'
    """)
    op.alter_column("questions", "paper_version_id", nullable=False)
    op.create_foreign_key("fk_questions_version", "questions", "paper_versions",
                          ["paper_version_id"], ["id"], ondelete="CASCADE")
    op.drop_column("papers", "full_text")

    op.create_table(
        "reviews",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("paper_version_id", sa.Uuid(), sa.ForeignKey("paper_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("openreview_note_id", sa.String(255), nullable=False),
        sa.Column("review_text", sa.Text(), nullable=False),
        sa.Column("raw_content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("invitation", sa.Text(), nullable=True),
        sa.Column("written_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("paper_version_id", "openreview_note_id", name="uq_reviews_version_note"),
    )
    op.create_index("ix_reviews_version", "reviews", ["paper_version_id"])
    op.create_table(
        "paper_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("paper_version_id", sa.Uuid(), sa.ForeignKey("paper_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("embedding", postgresql.ARRAY(sa.Float()), nullable=True),
        sa.Column("embedding_model", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("paper_version_id", "chunk_index", name="uq_paper_chunks_version_index"),
    )
    op.execute("ALTER TABLE paper_chunks ALTER COLUMN embedding TYPE vector(768) USING NULL::vector(768)")
    op.create_index("ix_paper_chunks_version", "paper_chunks", ["paper_version_id"])


def downgrade() -> None:
    op.add_column("papers", sa.Column("full_text", sa.Text(), nullable=True))
    op.execute("""
        UPDATE papers p SET full_text = v.paper_text
        FROM paper_versions v WHERE v.paper_id = p.id AND v.is_latest
    """)
    op.drop_index("ix_paper_chunks_version", table_name="paper_chunks")
    op.drop_table("paper_chunks")
    op.drop_index("ix_reviews_version", table_name="reviews")
    op.drop_table("reviews")
    op.drop_constraint("fk_questions_version", "questions", type_="foreignkey")
    op.drop_column("questions", "paper_version_id")
    op.add_column("extracted_fields", sa.Column("paper_id", sa.Uuid(), nullable=True))
    op.execute("""
        UPDATE extracted_fields e SET paper_id = v.paper_id
        FROM paper_versions v WHERE v.id = e.paper_version_id
    """)
    op.alter_column("extracted_fields", "paper_id", nullable=False)
    op.create_foreign_key("extracted_fields_paper_id_fkey", "extracted_fields", "papers",
                          ["paper_id"], ["id"], ondelete="CASCADE")
    op.drop_constraint("fk_extracted_fields_version", "extracted_fields", type_="foreignkey")
    op.drop_column("extracted_fields", "paper_version_id")
    op.drop_constraint("uq_paper_sections_version_position", "paper_sections", type_="unique")
    op.create_unique_constraint("uq_paper_sections_paper_position", "paper_sections",
                                ["paper_id", "position"])
    op.drop_constraint("fk_paper_sections_version", "paper_sections", type_="foreignkey")
    op.drop_column("paper_sections", "paper_version_id")
    op.drop_table("paper_versions")
    op.drop_index("ix_papers_forum_id", table_name="papers")
    op.drop_column("papers", "forum_id")
