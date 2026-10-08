"""LangChain tool adapters for PaperProbe's standalone services.

Import this module only when the optional ``agent`` dependencies are installed.
Database-backed tools are created with an application-provided async session factory;
the session is runtime configuration and is never exposed as an LLM argument.
"""

import base64
import binascii
from typing import Literal
from uuid import UUID

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.extraction import Claim, QuestionCritique, QuestionGeneration
from app.schemas.papers import PaperDetail, QuestionGenerationResponse
from app.services.gemini import SectionExtractedField, critique_question, propose_questions
from app.services.paper_pipeline import (
    MAX_PDF_BYTES,
    extract_paper_fields,
    generate_and_store_questions,
    generate_questions_from_context,
    ingest_forum,
    ingest_upload,
    parse_paper_text,
)
from app.services.paper_sections import PaperTextSection


class PaperTextSectionInput(BaseModel):
    """A parsed paper section used as structured extraction input."""

    position: int = Field(ge=0, description="Zero-based position of the section in the paper.")
    heading: str = Field(description="Display heading detected for the section.")
    category: str = Field(description="Normalized section category, such as method or experiments.")
    content: str = Field(description="Full text content assigned to the section.")


class PaperTextSectionOutput(PaperTextSectionInput):
    """A parsed section returned by the paper parser."""


class ParsePaperOutput(BaseModel):
    """Ordered sections returned by parse_paper_text."""

    sections: list[PaperTextSectionOutput]


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
    """Fields extracted from the supplied parsed sections."""

    fields: list[ExtractedFieldOutput]


class ParsePaperInput(BaseModel):
    """Input contract for deterministic paper section parsing."""

    full_text: str = Field(description="Readable full paper text, such as text extracted from a PDF.")
    abstract: str = Field(default="", description="Optional abstract fallback when full text omits it.")


class ExtractPaperFieldsInput(BaseModel):
    """Input contract for section-aware structured extraction."""

    title: str = Field(description="Paper title, used to ground extraction in the correct work.")
    sections: list[PaperTextSectionInput] = Field(
        min_length=1,
        description="Parsed sections from parse_paper_text; pass only sections needing field extraction.",
    )


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


class CritiqueQuestionInput(BaseModel):
    """Input contract for evaluating one candidate question."""

    question: str = Field(description="The candidate question to assess.")
    title: str = Field(description="Paper title.")
    abstract: str = Field(default="", description="Abstract when available.")
    extracted_fields: dict[str, dict] = Field(default_factory=dict, description="Available extracted fields.")
    paper_text: str = Field(default="", description="Optional full paper text for grounding.")


class IngestForumInput(BaseModel):
    """Input contract for fetching and storing an OpenReview paper."""

    forum_id: str = Field(min_length=1, description="Public OpenReview v2 forum ID.")


class IngestUploadInput(BaseModel):
    """Input contract for ingesting a PDF supplied as base64."""

    filename: str | None = Field(default=None, description="Original filename, if known.")
    pdf_base64: str = Field(description="Base64-encoded PDF bytes. The decoded PDF must be at most 20 MB.")


class GenerateStoredQuestionsInput(BaseModel):
    """Input contract for generating and saving questions from a stored paper."""

    paper_id: UUID = Field(description="UUID of the stored paper whose latest version should be used.")
    count: int = Field(default=5, ge=3, le=10, description="Number of questions to generate and store.")


@tool(
    "parse_paper_text",
    args_schema=ParsePaperInput,
    description=(
        "Split readable paper text into ordered named sections. Use this when the paper has not yet "
        "been parsed or section provenance is needed. It is deterministic and does not call an LLM. "
        "Returns ParsePaperOutput: ordered sections with position, heading, category, and content."
    ),
)
def parse_paper_text_tool(full_text: str, abstract: str = "") -> ParsePaperOutput:
    sections = parse_paper_text(full_text, abstract)
    return ParsePaperOutput(sections=[PaperTextSectionOutput.model_validate(section.__dict__) for section in sections])


@tool(
    "extract_paper_fields",
    args_schema=ExtractPaperFieldsInput,
    description=(
        "Extract summary, claims, methods, datasets, baselines, and limitations from supplied parsed "
        "sections. Call only when suitable extracted fields are missing or need refresh. This calls "
        "Gemini and returns ExtractPaperFieldsOutput with each value's source section position; it does "
        "not write to the database."
    ),
)
def extract_paper_fields_tool(title: str, sections: list[PaperTextSectionInput]) -> ExtractPaperFieldsOutput:
    parsed_sections = [PaperTextSection(**section.model_dump()) for section in sections]
    fields: list[SectionExtractedField] = extract_paper_fields(title, parsed_sections)
    return ExtractPaperFieldsOutput(fields=[
        ExtractedFieldOutput(
            field_type=field.field_type,
            value=field.value,
            section_position=field.section_position,
        )
        for field in fields
    ])


@tool(
    "generate_questions_from_context",
    args_schema=PaperContextInput,
    description=(
        "Generate exactly count grounded discussion questions from supplied paper context. Use this "
        "when existing extracted fields are already good enough, so parsing and extraction can be skipped. "
        "Returns QuestionGeneration with validated questions, focus, and rationale; it does not save them."
    ),
)
def generate_questions_from_context_tool(
    title: str, abstract: str = "", extracted_fields: dict[str, dict] | None = None,
    count: int = 5, paper_text: str = "",
) -> QuestionGeneration:
    return generate_questions_from_context(title, abstract, extracted_fields or {}, count, paper_text)


@tool(
    "propose_questions",
    args_schema=PaperContextInput,
    description=(
        "Propose distinct peer-review discussion questions grounded in supplied paper evidence. Use after "
        "choosing which context to provide; existing extracted fields can be passed directly. Returns each "
        "proposal in QuestionGeneration with focus and reason it is useful. This does not save questions."
    ),
)
def propose_questions_tool(
    title: str, abstract: str = "", extracted_fields: dict[str, dict] | None = None,
    count: int = 5, paper_text: str = "",
) -> QuestionGeneration:
    return propose_questions(title, abstract, extracted_fields or {}, count, paper_text)


@tool(
    "critique_question",
    args_schema=CritiqueQuestionInput,
    description=(
        "Assess one candidate question against the supplied paper context for specificity, grounding, "
        "answerability, and critical value. Use after proposing a question or when a user supplies one. "
        "Returns QuestionCritique: scores, a keep/revise/reject verdict, issues, and a suggested revision "
        "when appropriate."
    ),
)
def critique_question_tool(
    question: str, title: str, abstract: str = "", extracted_fields: dict[str, dict] | None = None,
    paper_text: str = "",
) -> QuestionCritique:
    return critique_question(question, title, abstract, extracted_fields, paper_text)


def create_database_tools(session_factory: async_sessionmaker[AsyncSession]) -> list[BaseTool]:
    """Create tools that need a database; session infrastructure stays out of model schemas."""

    async def ingest_openreview_tool_impl(forum_id: str) -> PaperDetail:
        async with session_factory() as db:
            return await ingest_forum(db, forum_id)

    async def ingest_pdf_tool_impl(filename: str | None, pdf_base64: str) -> PaperDetail:
        try:
            data = base64.b64decode(pdf_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("pdf_base64 must contain valid base64 data.") from exc
        if len(data) > MAX_PDF_BYTES:
            raise ValueError("PDF must be 20 MB or smaller.")
        if not data.startswith(b"%PDF"):
            raise ValueError("The uploaded bytes are not a valid PDF.")
        async with session_factory() as db:
            return await ingest_upload(db, filename, data)

    async def generate_stored_questions_tool_impl(paper_id: UUID, count: int = 5) -> QuestionGenerationResponse:
        async with session_factory() as db:
            return await generate_and_store_questions(db, UUID(paper_id), count)

    return [
        tool(
            "ingest_openreview_paper",
            args_schema=IngestForumInput,
            description=(
                "Fetch an OpenReview forum and store its current paper context. This is a database-backed "
                "ingestion convenience operation that follows the app's current parse/extract pipeline; "
                "use only when the paper is not already stored or needs refreshing. Returns PaperDetail."
            ),
        )(ingest_openreview_tool_impl),
        tool(
            "ingest_pdf",
            args_schema=IngestUploadInput,
            description=(
                "Ingest and store a PDF from base64 bytes. This follows the app's current upload, optional "
                "OpenReview matching, parse, and extraction flow. Use only when a PDF source must be added. "
                "Returns PaperDetail."
            ),
        )(ingest_pdf_tool_impl),
        tool(
            "generate_and_store_questions",
            args_schema=GenerateStoredQuestionsInput,
            description=(
                "Generate questions from a stored paper's latest abstract and merged extracted fields, then "
                "save drafts linked to that version. Prefer generate_questions_from_context or propose_questions "
                "when the agent has already selected the context and does not want this database write. "
                "Returns QuestionGenerationResponse."
            ),
        )(generate_stored_questions_tool_impl),
    ]


PAPER_TOOLS: list[BaseTool] = [
    parse_paper_text_tool,
    extract_paper_fields_tool,
    generate_questions_from_context_tool,
    propose_questions_tool,
    critique_question_tool,
]

PAPER_TOOL_OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "parse_paper_text": ParsePaperOutput,
    "extract_paper_fields": ExtractPaperFieldsOutput,
    "generate_questions_from_context": QuestionGeneration,
    "propose_questions": QuestionGeneration,
    "critique_question": QuestionCritique,
    "ingest_openreview_paper": PaperDetail,
    "ingest_pdf": PaperDetail,
    "generate_and_store_questions": QuestionGenerationResponse,
}


def create_paper_tools(session_factory: async_sessionmaker[AsyncSession]) -> list[BaseTool]:
    """Return all stateless and database-backed tools for a LangChain/LangGraph agent."""
    return [*PAPER_TOOLS, *create_database_tools(session_factory)]
