from pydantic import BaseModel, Field


class Claim(BaseModel):
    statement: str
    evidence_from_abstract: str | None = None


class AbstractExtraction(BaseModel):
    """Structured fields inferred only from a paper title and abstract."""

    summary: str = Field(description="One or two sentences based only on the abstract.")
    claims: list[Claim] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    datasets: list[str] = Field(default_factory=list)
    baselines: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
