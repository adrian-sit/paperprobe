from typing import Literal

from pydantic import BaseModel, Field


class Claim(BaseModel):
    statement: str
    evidence_from_section: str | None = Field(
        default=None, description="A short supporting passage paraphrase from this section."
    )


class AbstractClaim(BaseModel):
    """Legacy response shape used by the standalone metadata-only smoke command."""

    statement: str
    evidence_from_abstract: str | None = None


class AbstractExtraction(BaseModel):
    """Legacy title/abstract schema used only by the standalone API smoke command."""

    summary: str = Field(description="One or two sentences based only on the abstract.")
    claims: list[AbstractClaim] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    datasets: list[str] = Field(default_factory=list)
    baselines: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class GeneratedQuestion(BaseModel):
    question: str = Field(description="A specific, answerable question for discussing the paper.")
    focus: str = Field(description="Short label such as methodology, evidence, or limitation.")
    rationale: str = Field(description="Why this question is useful given the supplied paper information.")


class QuestionGeneration(BaseModel):
    questions: list[GeneratedQuestion] = Field(min_length=1, max_length=10)


class QuestionCritique(BaseModel):
    """Structured assessment of one candidate question against supplied paper context."""

    verdict: Literal["keep", "revise", "reject"] = Field(
        description="Whether the question is ready, needs revision, or should be discarded."
    )
    specificity_score: int = Field(ge=1, le=5, description="How specific the question is to this paper.")
    grounding_score: int = Field(ge=1, le=5, description="How well the question is supported by supplied context.")
    answerability_score: int = Field(ge=1, le=5, description="Whether the authors could answer it from their work.")
    critical_value_score: int = Field(ge=1, le=5, description="How useful it is for critical discussion.")
    strengths: list[str] = Field(default_factory=list, description="What the candidate question does well.")
    issues: list[str] = Field(default_factory=list, description="Concrete weaknesses or unsupported assumptions.")
    rationale: str = Field(description="Brief explanation for the verdict and scores.")
    revised_question: str | None = Field(
        default=None,
        description="A grounded revision when verdict is revise; otherwise null.",
    )


class FinalAgentQuestion(GeneratedQuestion):
    """A final proposed question paired with the critique that cleared it for saving."""

    critique: QuestionCritique = Field(
        description="The critique result for the final question; saving requires verdict=keep."
    )
