import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.db.models import ExtractedField, Paper, PaperSection
from app.db.session import get_db_session
from app.schemas.extraction import AbstractExtraction
from app.schemas.papers import (
    OpenReviewIngestRequest,
    PaperDetail,
    StoredExtractedField,
    StoredSection,
)
from app.services.gemini import extract_abstract
from app.services.openreview import OpenReviewContentError, OpenReviewPaper, fetch_paper

router = APIRouter()


def serialize_paper(paper: Paper) -> PaperDetail:
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
    )


async def load_paper(db: AsyncSession, paper_id: UUID) -> Paper | None:
    statement = (
        select(Paper)
        .options(selectinload(Paper.sections), selectinload(Paper.extracted_fields))
        .where(Paper.id == paper_id)
    )
    return await db.scalar(statement)


@router.post("/openreview", response_model=PaperDetail, status_code=status.HTTP_201_CREATED)
async def ingest_openreview_paper(
    request: OpenReviewIngestRequest,
    db: AsyncSession = Depends(get_db_session),
) -> PaperDetail:
    """Fetch, extract, and persist one public OpenReview submission synchronously."""
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

    existing = await db.scalar(select(Paper.id).where(Paper.source_uri == source.source_uri))
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This OpenReview forum has already been processed as paper {existing}.",
        )

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
    return serialize_paper(stored)


@router.get("/{paper_id}", response_model=PaperDetail)
async def get_paper(paper_id: UUID, db: AsyncSession = Depends(get_db_session)) -> PaperDetail:
    """Return one persisted paper with its stored abstract and extracted fields."""
    paper = await load_paper(db, paper_id)
    if not paper:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Paper not found.")
    return serialize_paper(paper)
