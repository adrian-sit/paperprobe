from uuid import UUID, uuid4

from pydantic import AnyHttpUrl, BaseModel, Field, model_validator


class PaperCreate(BaseModel):
    """A paper source. Exactly one source is required for ingestion."""

    pdf_url: AnyHttpUrl | None = None
    openreview_forum_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def require_one_source(self) -> "PaperCreate":
        if bool(self.pdf_url) == bool(self.openreview_forum_id):
            raise ValueError("Provide exactly one of pdf_url or openreview_forum_id.")
        return self


class PaperIngestionAccepted(BaseModel):
    paper_id: UUID = Field(default_factory=uuid4)
    status: str = "queued"
    detail: str
