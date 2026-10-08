from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class SimilarExampleQuestion(BaseModel):
    """A question example paired with the similar source paper it came from."""

    id: UUID
    source_title: str
    source_abstract_or_claims: str
    question_text: str
    rationale: str | None = None
    source_type: Literal["own_criticism", "openreview_review"]
    cosine_similarity: float = Field(ge=-1.0, le=1.0)


class SimilarExampleQuestions(BaseModel):
    """Top matching paper-conditioned question examples."""

    examples: list[SimilarExampleQuestion]
