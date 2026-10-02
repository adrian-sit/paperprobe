from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class OpenReviewIngestRequest(BaseModel):
    forum_id: str = Field(min_length=1, description="Public OpenReview v2 forum ID.")


class StoredSection(BaseModel):
    id: UUID
    position: int
    heading: str | None
    content: str


class StoredExtractedField(BaseModel):
    id: UUID
    field_type: str
    value: dict
    extraction_model: str | None
    prompt_version: str | None


class PaperVersionSummary(BaseModel):
    id: UUID
    version_key: str
    version_timestamp: int | None
    is_latest: bool
    title: str | None
    text_characters: int
    review_count: int


class StoredReview(BaseModel):
    id: UUID
    openreview_note_id: str
    review_text: str
    invitation: str | None
    written_at: datetime | None


class PaperVersionDetail(BaseModel):
    id: UUID
    paper_id: UUID
    version_key: str
    version_timestamp: int | None
    is_latest: bool
    title: str | None
    abstract: str | None
    paper_text: str | None
    extracted_fields: list[StoredExtractedField]
    reviews: list[StoredReview]


class PaperDetail(BaseModel):
    id: UUID
    source_type: str
    source_uri: str
    title: str | None
    forum_id: str | None = None
    latest_version_id: UUID | None = None
    versions: list[PaperVersionSummary] = Field(default_factory=list)
    reviews: list[StoredReview] = Field(default_factory=list)
    full_text: str | None = None
    status: str
    raw_metadata: dict | None
    created_at: datetime
    sections: list[StoredSection]
    extracted_fields: list[StoredExtractedField]
    from_cache: bool = False
    refreshed: bool = False


class QuestionGenerationRequest(BaseModel):
    count: int = Field(default=5, ge=3, le=10)


class StoredQuestion(BaseModel):
    id: UUID
    paper_version_id: UUID | None = None
    text: str
    status: str
    source: str
    critic_notes: str | None
    created_at: datetime


class QuestionGenerationResponse(BaseModel):
    paper_id: UUID
    questions: list[StoredQuestion]
