"""LangChain tool adapters for PaperProbe's standalone services.

Import this module only when the optional ``agent`` dependencies are installed.
Database-backed tools are created with an application-provided async session factory;
the session is runtime configuration and is never exposed as an LLM argument.
"""

from typing import Literal
from uuid import UUID

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.extraction import Claim, FinalAgentQuestion, QuestionCritique, QuestionGeneration
from app.schemas.examples import SimilarExampleQuestion, SimilarExampleQuestions
from app.schemas.papers import PaperDetail, PaperReviewContext, PaperReviewContextResponse, QuestionGenerationResponse
from app.services.example_retrieval import retrieve_similar_example_questions
from app.services.gemini import SectionExtractedField, critique_question, propose_questions
from app.services.paper_pipeline import (
    extract_paper_fields,
    get_all_reviews_for_paper,
    get_stored_paper,
    merge_section_extractions,
    parse_paper_text,
    save_final_questions,
)


def _review_context_payload(reviews: list[PaperReviewContext] | None) -> list[dict]:
    return [
        item.model_dump(mode="json") if isinstance(item, PaperReviewContext) else item
        for item in reviews or []
    ]


class ExtractedFieldOutput(BaseModel):
    """One structured field extracted from a source section."""

    field_type: Literal["summary", "claims", "methods", "datasets", "baselines", "limitations"] = Field(
        description="The type of extracted information."
    )
    value: str | list[str] | list[Claim] = Field(
        description="Extracted summary text, claim objects, or a list of extracted strings."
    )
    section_position: int = Field(ge=0, description="Source section position for provenance.")


class ExtractPaperFieldsOutput(BaseModel):
    """Section-level extraction records and normalized values for question generation."""

    fields: list[ExtractedFieldOutput]
    extracted_fields: dict[str, dict] = Field(
        description="Merged field values ready to pass to propose_questions."
    )


class ExtractPaperFieldsInput(BaseModel):
    """Input contract for section-aware extraction from stored paper text."""

    title: str = Field(description="Paper title, used to ground extraction in the correct work.")
    full_text: str = Field(default="", description="Full paper text returned by get_stored_paper, if available.")
    abstract: str = Field(default="", description="Optional abstract fallback from the stored paper.")


class PaperContextInput(BaseModel):
    """Paper context shared by question-generation tools."""

    title: str = Field(description="Paper title.")
    abstract: str = Field(default="", description="Abstract when available.")
    extracted_fields: dict[str, dict] = Field(
        default_factory=dict,
        description="Existing structured fields keyed by field type, reusable without re-extraction.",
    )
    count: int = Field(default=5, ge=3, le=10, description="Number of distinct questions to propose.")
    paper_text: str = Field(default="", description="Optional full text for added grounding.")
    review_context: list[PaperReviewContext] = Field(
        default_factory=list,
        description=(
            "Reviews from every stored paper version, with version/forum provenance. Use these to avoid "
            "duplicating reviewer comments or identify points that remain unresolved across revisions."
        ),
    )
    example_questions: list[SimilarExampleQuestion] = Field(
        default_factory=list,
        description=(
            "Top-k example (source paper, question) pairs from retrieve_similar_example_questions. "
            "Use as few-shot guidance, not as questions to copy."
        ),
    )


class CritiqueQuestionInput(BaseModel):
    """Input contract for evaluating one candidate question."""

    question: str = Field(description="The candidate question to assess.")
    title: str = Field(description="Paper title.")
    abstract: str = Field(default="", description="Abstract when available.")
    extracted_fields: dict[str, dict] = Field(default_factory=dict, description="Available extracted fields.")
    paper_text: str = Field(default="", description="Optional full paper text for grounding.")
    review_context: list[PaperReviewContext] = Field(
        default_factory=list,
        description="Reviews from all versions for duplicate-comment and unresolved-point checks.",
    )


class GetStoredPaperInput(BaseModel):
    """Input contract for looking up an already prepared paper."""

    paper_id: UUID = Field(description="UUID of a paper prepared by the UI.")


class GetAllReviewsForPaperInput(BaseModel):
    """Input contract for retrieving reviews across the paper's stored versions."""

    paper_id: UUID = Field(description="Stored paper UUID whose version-linked reviews should be returned.")


class RetrieveSimilarExampleQuestionsInput(BaseModel):
    """Input contract for retrieving examples conditioned on a target paper."""

    paper_context: str = Field(
        min_length=1,
        description="Representative text from the current paper, preferably its abstract and extracted claims.",
    )
    k: int = Field(default=5, ge=1, le=20, description="Maximum number of similar paper/question pairs.")


class SaveFinalQuestionsInput(BaseModel):
    """Input contract for persisting the agent's finished, critiqued questions."""

    paper_id: UUID = Field(description="Stored paper UUID; questions link to its latest version.")
    questions: list[FinalAgentQuestion] = Field(
        min_length=1,
        max_length=10,
        description="Final questions with a critique attached; every critique verdict must be keep.",
    )


@tool(
    "extract_paper_fields",
    args_schema=ExtractPaperFieldsInput,
    description=(
        "Extract summary, claims, methods, datasets, baselines, and limitations from a prepared paper. "
        "First check get_stored_paper and call this only if suitable extracted fields are missing. "
        "Pass the stored title, full text, and abstract; this tool parses sections internally, calls Gemini, "
        "and returns ExtractPaperFieldsOutput with source positions and merged values. It does not write to the database."
    ),
)
def extract_paper_fields_tool(title: str, full_text: str, abstract: str = "") -> ExtractPaperFieldsOutput:
    parsed_sections = parse_paper_text(full_text, abstract)
    fields: list[SectionExtractedField] = extract_paper_fields(title, parsed_sections)
    return ExtractPaperFieldsOutput(fields=[
        ExtractedFieldOutput(
            field_type=field.field_type,
            value=field.value,
            section_position=field.section_position,
        )
        for field in fields
    ], extracted_fields=merge_section_extractions(fields))


@tool(
    "propose_questions",
    args_schema=PaperContextInput,
    description=(
        "Propose distinct peer-review discussion questions grounded in supplied paper evidence. Use after "
        "choosing which context to provide; pass all-version review_context from get_all_reviews_for_paper "
        "to avoid duplicate comments and build on unresolved reviewer points. Pass example_questions from "
        "retrieve_similar_example_questions as paper-conditioned few-shot guidance; do not copy them verbatim. "
        "Existing extracted fields can be passed directly. Returns each "
        "proposal in QuestionGeneration with focus and reason it is useful. This does not save questions."
    ),
)
def propose_questions_tool(
    title: str, abstract: str = "", extracted_fields: dict[str, dict] | None = None,
    count: int = 5, paper_text: str = "", review_context: list[PaperReviewContext] | None = None,
    example_questions: list[SimilarExampleQuestion] | None = None,
) -> QuestionGeneration:
    return propose_questions(
        title, abstract, extracted_fields or {}, count, paper_text,
        _review_context_payload(review_context),
        [item.model_dump(mode="json") if isinstance(item, SimilarExampleQuestion) else item
         for item in example_questions or []],
    )


@tool(
    "critique_question",
    args_schema=CritiqueQuestionInput,
    description=(
        "Assess one candidate question against the supplied paper context for specificity, grounding, "
        "answerability, and critical value. Pass all-version review_context from get_all_reviews_for_paper "
        "to check whether the question duplicates an existing reviewer comment or addresses an unresolved point. "
        "Use after proposing a question or when a user supplies one. "
        "Returns QuestionCritique: scores, a keep/revise/reject verdict, issues, and a suggested revision "
        "when appropriate."
    ),
)
def critique_question_tool(
    question: str, title: str, abstract: str = "", extracted_fields: dict[str, dict] | None = None,
    paper_text: str = "", review_context: list[PaperReviewContext] | None = None,
) -> QuestionCritique:
    return critique_question(
        question, title, abstract, extracted_fields, paper_text,
        _review_context_payload(review_context),
    )


def create_database_tools(session_factory: async_sessionmaker[AsyncSession]) -> list[BaseTool]:
    """Create read-only context and terminal-save tools around an application session factory."""

    async def get_stored_paper_tool_impl(paper_id: UUID) -> PaperDetail:
        async with session_factory() as db:
            return await get_stored_paper(db, paper_id)

    async def get_all_reviews_for_paper_tool_impl(paper_id: UUID) -> PaperReviewContextResponse:
        async with session_factory() as db:
            return await get_all_reviews_for_paper(db, paper_id)

    async def retrieve_similar_example_questions_tool_impl(
        paper_context: str, k: int = 5
    ) -> SimilarExampleQuestions:
        async with session_factory() as db:
            return await retrieve_similar_example_questions(db, paper_context, k)

    async def save_final_questions_tool_impl(
        paper_id: UUID, questions: list[FinalAgentQuestion]
    ) -> QuestionGenerationResponse:
        async with session_factory() as db:
            return await save_final_questions(db, paper_id, questions)

    return [
        tool(
            "get_stored_paper",
            args_schema=GetStoredPaperInput,
            description=(
                "Read an already prepared paper before deciding which agent tools it needs. Returns its latest "
                "version, full text, parsed sections, and existing extracted fields. This tool is read-only; "
                "do not call ingestion or parsing tools during the agent run. Returns PaperDetail."
            ),
        )(get_stored_paper_tool_impl),
        tool(
            "get_all_reviews_for_paper",
            args_schema=GetAllReviewsForPaperInput,
            description=(
                "Read every review stored under this paper across all versions, including earlier revisions. "
                "Returns each review with its version key, source forum, and paper title so the agent can use "
                "historical reviewer context, spot unresolved points, and avoid duplicate questions. Read-only; "
                "returns PaperReviewContextResponse. Call during context gathering before proposing questions."
            ),
        )(get_all_reviews_for_paper_tool_impl),
        tool(
            "retrieve_similar_example_questions",
            args_schema=RetrieveSimilarExampleQuestionsInput,
            description=(
                "Find few-shot examples by embedding the current paper's abstract/claims and searching "
                "example_pairs.paper_embedding. Returns up to k source-paper/question pairs, each with its "
                "question, rationale, source type, and similarity score. Use before propose_questions and pass "
                "the returned pairs as example_questions. This reads the database and calls the configured "
                "Gemini embedding model; it does not write data. Returns SimilarExampleQuestions."
            ),
        )(retrieve_similar_example_questions_tool_impl),
        tool(
            "save_final_questions",
            args_schema=SaveFinalQuestionsInput,
            description=(
                "Persist the agent's finished question list without generating new questions. Every question "
                "must include its focus, rationale, and critique, and each critique must have verdict=keep. "
                "Saves the questions as final, links them to the paper's latest version, and stores critique "
                "scores and details with each question. Returns QuestionGenerationResponse. Call this once "
                "the propose/critique loop is complete."
            ),
        )(save_final_questions_tool_impl),
    ]


PAPER_TOOLS: list[BaseTool] = [
    extract_paper_fields_tool,
    propose_questions_tool,
    critique_question_tool,
]

PAPER_TOOL_OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "extract_paper_fields": ExtractPaperFieldsOutput,
    "propose_questions": QuestionGeneration,
    "critique_question": QuestionCritique,
    "get_stored_paper": PaperDetail,
    "get_all_reviews_for_paper": PaperReviewContextResponse,
    "retrieve_similar_example_questions": SimilarExampleQuestions,
    "save_final_questions": QuestionGenerationResponse,
}


def create_paper_tools(session_factory: async_sessionmaker[AsyncSession]) -> list[BaseTool]:
    """Return all stateless and database-backed tools for a LangChain/LangGraph agent."""
    return [*PAPER_TOOLS, *create_database_tools(session_factory)]
