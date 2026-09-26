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


class PaperDetail(BaseModel):
    id: UUID
    source_type: str
    source_uri: str
    title: str | None
    status: str
    raw_metadata: dict | None
    created_at: datetime
    sections: list[StoredSection]
    extracted_fields: list[StoredExtractedField]
    from_cache: bool = False


class QuestionGenerationRequest(BaseModel):
    count: int = Field(default=5, ge=3, le=10)


class StoredQuestion(BaseModel):
    id: UUID
    text: str
    status: str
    source: str
    critic_notes: str | None
    created_at: datetime


class QuestionGenerationResponse(BaseModel):
    paper_id: UUID
    questions: list[StoredQuestion]
