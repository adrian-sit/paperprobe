import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Paper(Base):
    """Forum-level identity shared by every revision and its peer reviews."""

    __tablename__ = "papers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    forum_id: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    source_type: Mapped[str] = mapped_column(String(32))
    source_uri: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="queued", server_default="queued")
    raw_metadata: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    versions: Mapped[list["PaperVersion"]] = relationship(
        back_populates="paper", cascade="all, delete-orphan", order_by="PaperVersion.version_timestamp"
    )
    sections: Mapped[list["PaperSection"]] = relationship(back_populates="paper")
    questions: Mapped[list["Question"]] = relationship(back_populates="paper")


class PaperVersion(Base):
    """One revision snapshot of a forum submission."""

    __tablename__ = "paper_versions"
    __table_args__ = (
        UniqueConstraint("paper_id", "version_key", name="uq_paper_versions_paper_key"),
        Index("uq_paper_versions_one_latest", "paper_id", unique=True,
              postgresql_where=text("is_latest IS TRUE")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"))
    version_key: Mapped[str] = mapped_column(String(255))
    version_timestamp: Mapped[int | None] = mapped_column(BigInteger)
    is_latest: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    title: Mapped[str | None] = mapped_column(Text)
    abstract: Mapped[str | None] = mapped_column(Text)
    paper_text: Mapped[str | None] = mapped_column(Text)
    raw_metadata: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    paper: Mapped[Paper] = relationship(back_populates="versions")
    sections: Mapped[list["PaperSection"]] = relationship(back_populates="paper_version")
    extracted_fields: Mapped[list["ExtractedField"]] = relationship(
        back_populates="paper_version", cascade="all, delete-orphan"
    )
    reviews: Mapped[list["Review"]] = relationship(
        back_populates="paper_version", cascade="all, delete-orphan"
    )
    chunks: Mapped[list["PaperChunk"]] = relationship(
        back_populates="paper_version", cascade="all, delete-orphan"
    )
    questions: Mapped[list["Question"]] = relationship(back_populates="paper_version")


class PaperSection(Base):
    __tablename__ = "paper_sections"
    __table_args__ = (
        UniqueConstraint("paper_version_id", "position", name="uq_paper_sections_version_position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"))
    paper_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_versions.id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    paper: Mapped[Paper] = relationship(back_populates="sections")
    paper_version: Mapped[PaperVersion] = relationship(back_populates="sections")


class ExtractedField(Base):
    __tablename__ = "extracted_fields"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    paper_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_versions.id", ondelete="CASCADE")
    )
    section_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("paper_sections.id", ondelete="SET NULL")
    )
    field_type: Mapped[str] = mapped_column(String(64))
    value: Mapped[dict] = mapped_column(JSONB)
    extraction_model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    paper_version: Mapped[PaperVersion] = relationship(back_populates="extracted_fields")


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("paper_version_id", "openreview_note_id", name="uq_reviews_version_note"),
        Index("ix_reviews_version", "paper_version_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    paper_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_versions.id", ondelete="CASCADE")
    )
    openreview_note_id: Mapped[str] = mapped_column(String(255))
    review_text: Mapped[str] = mapped_column(Text)
    raw_content: Mapped[dict] = mapped_column(JSONB)
    invitation: Mapped[str | None] = mapped_column(Text)
    written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_metadata: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    paper_version: Mapped[PaperVersion] = relationship(back_populates="reviews")


class PaperChunk(Base):
    __tablename__ = "paper_chunks"
    __table_args__ = (
        UniqueConstraint("paper_version_id", "chunk_index", name="uq_paper_chunks_version_index"),
        Index("ix_paper_chunks_version", "paper_version_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    paper_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_versions.id", ondelete="CASCADE")
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    chunk_text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int | None] = mapped_column(Integer)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(768))
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    paper_version: Mapped[PaperVersion] = relationship(back_populates="chunks")


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"))
    paper_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_versions.id", ondelete="CASCADE")
    )
    section_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("paper_sections.id", ondelete="SET NULL")
    )
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="draft", server_default="draft")
    source: Mapped[str] = mapped_column(String(32), default="generated", server_default="generated")
    critic_notes: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    paper: Mapped[Paper] = relationship(back_populates="questions")
    paper_version: Mapped[PaperVersion] = relationship(back_populates="questions")
