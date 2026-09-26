import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.db.models import ExtractedField, Paper, PaperSection, Question
from app.db.session import get_db_session
from app.schemas.extraction import AbstractExtraction
from app.schemas.papers import (
    OpenReviewIngestRequest,
    PaperDetail,
    QuestionGenerationRequest,
    QuestionGenerationResponse,
    StoredExtractedField,
    StoredQuestion,
    StoredSection,
)
from app.services.gemini import extract_abstract, generate_questions
from app.services.openreview import (
    OpenReviewContentError,
    OpenReviewPaper,
    fetch_paper,
    forum_source_uri,
)

router = APIRouter()


def serialize_paper(paper: Paper, *, from_cache: bool = False) -> PaperDetail:
    return PaperDetail(
        id=paper.id,
        source_type=paper.source_type,
        source_uri=paper.source_uri,
        title=paper.title,
        status=paper.status,
        raw_metadata=paper.raw_metadata,
        created_at=paper.created_at,
        sections=[
            StoredSection(id=section.id, position=section.position, heading=section.heading, content=section.content)
            for section in sorted(paper.sections, key=lambda item: item.position)
        ],
        extracted_fields=[
            StoredExtractedField(
                id=field.id,
                field_type=field.field_type,
                value=field.value,
                extraction_model=field.extraction_model,
                prompt_version=field.prompt_version,
            )
            for field in paper.extracted_fields
        ],
        from_cache=from_cache,
    )


async def load_paper(db: AsyncSession, paper_id: UUID) -> Paper | None:
    statement = (
        select(Paper)
        .options(selectinload(Paper.sections), selectinload(Paper.extracted_fields))
        .where(Paper.id == paper_id)
    )
    return await db.scalar(statement)


def serialize_question(question: Question) -> StoredQuestion:
    return StoredQuestion(
        id=question.id,
        text=question.text,
        status=question.status,
        source=question.source,
        critic_notes=question.critic_notes,
        created_at=question.created_at,
    )


@router.post("/openreview", response_model=PaperDetail)
async def ingest_openreview_paper(
    request: OpenReviewIngestRequest,
    response: Response,
    db: AsyncSession = Depends(get_db_session),
) -> PaperDetail:
    """Return a stored paper when available; otherwise fetch, extract, and persist it."""
    existing_id = await db.scalar(
        select(Paper.id).where(Paper.source_uri == forum_source_uri(request.forum_id))
    )
    if existing_id:
        existing = await load_paper(db, existing_id)
        if not existing:  # pragma: no cover - protects against a concurrent deletion.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stored paper disappeared.")
        response.headers["X-PaperProbe-Cache"] = "hit"
        return serialize_paper(existing, from_cache=True)

    try:
        source: OpenReviewPaper = await asyncio.to_thread(fetch_paper, request.forum_id)
        extraction: AbstractExtraction = await asyncio.to_thread(
            extract_abstract, source.title, source.abstract
        )
    except OpenReviewContentError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "stage": "openreview_content",
                "message": str(exc),
                "missing_fields": exc.missing_fields,
                "available_fields": exc.available_fields,
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    paper = Paper(
        source_type="openreview",
        source_uri=source.source_uri,
        title=source.title,
        status="completed",
        raw_metadata={"forum_id": source.forum_id, "public_review_count": source.review_count},
    )
    abstract_section = PaperSection(position=0, heading="Abstract", content=source.abstract)
    paper.sections.append(abstract_section)
    gemini_model = get_settings().gemini_model
    for field_type, value in extraction.model_dump().items():
        normalized_value = {"text": value} if field_type == "summary" else {"items": value}
        paper.extracted_fields.append(
            ExtractedField(
                section_id=None,
                field_type=field_type,
                value=normalized_value,
                extraction_model=gemini_model,
                prompt_version="abstract-v1",
            )
        )
    db.add(paper)
    await db.commit()
    stored = await load_paper(db, paper.id)
    if not stored:  # pragma: no cover - protects against an unexpected deleted row.
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Paper was not stored.")
    response.status_code = status.HTTP_201_CREATED
    response.headers["X-PaperProbe-Cache"] = "miss"
    return serialize_paper(stored)


@router.get("/{paper_id}", response_model=PaperDetail)
async def get_paper(paper_id: UUID, db: AsyncSession = Depends(get_db_session)) -> PaperDetail:
    """Return one persisted paper with its stored abstract and extracted fields."""
    paper = await load_paper(db, paper_id)
    if not paper:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Paper not found.")
    return serialize_paper(paper)


@router.post("/{paper_id}/questions", response_model=QuestionGenerationResponse)
async def create_questions(
    paper_id: UUID,
    request: QuestionGenerationRequest,
    db: AsyncSession = Depends(get_db_session),
) -> QuestionGenerationResponse:
    """Generate and persist critical questions using only the current paper context."""
    paper = await load_paper(db, paper_id)
    if not paper:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Paper not found.")
    abstract = next((section.content for section in paper.sections if section.heading == "Abstract"), None)
    if not abstract:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="This paper has no stored abstract for question generation.",
        )

    field_context = {field.field_type: field.value for field in paper.extracted_fields}
    try:
        generated = await asyncio.to_thread(
            generate_questions, paper.title or "Untitled paper", abstract, field_context, request.count
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    stored_questions = []
    for item in generated.questions:
        question = Question(
            paper_id=paper.id,
            text=item.question,
            status="draft",
            source="gemini",
            critic_notes=f"Focus: {item.focus}\nRationale: {item.rationale}",
        )
        db.add(question)
        stored_questions.append(question)
    await db.commit()
    for question in stored_questions:
        await db.refresh(question)
    return QuestionGenerationResponse(
        paper_id=paper.id, questions=[serialize_question(question) for question in stored_questions]
    )


@router.get("/{paper_id}/questions", response_model=list[StoredQuestion])
async def get_questions(paper_id: UUID, db: AsyncSession = Depends(get_db_session)) -> list[StoredQuestion]:
    """Return all generated questions currently stored for a paper."""
    exists = await db.scalar(select(Paper.id).where(Paper.id == paper_id))
    if not exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Paper not found.")
    questions = await db.scalars(
        select(Question).where(Question.paper_id == paper_id).order_by(Question.created_at.desc())
    )
    return [serialize_question(question) for question in questions]
