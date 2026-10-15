from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class SimilarExampleQuestion(BaseModel):
    """A question example paired with the similar source paper it came from."""

    id: UUID
    source_paper_id: UUID | None = None
    source_title: str
    source_abstract_or_claims: str
    question_text: str
    rationale: str | None = None
    source_type: Literal["own_criticism", "openreview_review"]
    cosine_similarity: float = Field(ge=-1.0, le=1.0)


class SimilarExampleQuestions(BaseModel):
    """Top matching paper-conditioned question examples."""

    examples: list[SimilarExampleQuestion]


class ExamplePairUploadQuestion(BaseModel):
    """One question saved for an uploaded example paper."""

    id: UUID
    question_text: str
    rationale: str | None = None


class ExamplePairUploadResponse(BaseModel):
    """Result of saving multiple example questions for one uploaded paper."""

    source_paper_id: UUID
    source_title: str
    source_type: Literal["own_criticism", "openreview_review"]
    question_count: int
    questions: list[ExamplePairUploadQuestion]


class ExamplePaperLibraryItem(BaseModel):
    """An uploaded source paper and its stored question examples."""

    source_paper_id: UUID
    source_title: str
    source_abstract_or_claims: str
    source_type: Literal["own_criticism", "openreview_review"]
    questions: list[ExamplePairUploadQuestion]


class ExamplePaperLibraryResponse(BaseModel):
    """All source papers and their questions available in the example library."""

    papers: list[ExamplePaperLibraryItem]


class ExampleQuestionUpdate(BaseModel):
    """Replacement question text and optional rationale for one stored example."""

    question_text: str = Field(min_length=1, max_length=10000)
    rationale: str | None = Field(default=None, max_length=10000)


class ExampleQuestionCreate(BaseModel):
    """A new question to attach to an existing source paper."""

    question_text: str = Field(min_length=1, max_length=10000)
    rationale: str | None = Field(default=None, max_length=10000)
