from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.progress import ProgressReporter, progress_response
from app.db.models import Paper, PaperVersion, Question
from app.db.session import get_db_session, get_session_factory
from app.schemas.papers import (
    OpenReviewIngestRequest, PaperDetail, PaperVersionDetail, QuestionGenerationRequest,
    QuestionGenerationResponse, StoredExtractedField, StoredQuestion, StoredReview, StoredSection,
)
from app.services.openreview import OpenReviewContentError
from app.services.paper_pipeline import (
    MAX_PDF_BYTES, _load, _question, _serialize, generate_and_store_questions,
    ingest_forum, ingest_upload,
)

router = APIRouter()


def database_progress_response(operation):
    async def run(report: ProgressReporter):
        async with get_session_factory()() as db:
            return await operation(report, db)
    return progress_response(run)


@router.post("/openreview/progress")
async def ingest_openreview_with_progress(request: OpenReviewIngestRequest):
    async def operation(report: ProgressReporter, db: AsyncSession):
        await report("lookup", "Check stored papers", "running", "Checking the forum identity.")
        try:
            return await ingest_forum(db, request.forum_id, report)
        except Exception as exc:
            await report("error", "Ingest OpenReview paper", "error", str(exc))
            raise
    return database_progress_response(operation)


@router.post("/openreview", response_model=PaperDetail)
async def ingest_openreview_paper(request: OpenReviewIngestRequest, response: Response,
                                  db: AsyncSession = Depends(get_db_session)):
    try:
        existing = await db.scalar(select(Paper.id).where(Paper.forum_id == request.forum_id))
        result = await ingest_forum(db, request.forum_id)
    except OpenReviewContentError as exc:
        raise HTTPException(422, detail={"message": str(exc), "missing_fields": exc.missing_fields,
                                         "available_fields": exc.available_fields}) from exc
    except Exception as exc:
        raise HTTPException(502, detail=str(exc)) from exc
    response.status_code = 200 if existing else 201
    response.headers["X-PaperProbe-Cache"] = "refreshed" if existing else "miss"
    return result


def _validate_pdf(file: UploadFile, data: bytes):
    if file.content_type not in {"application/pdf", "application/octet-stream"} and not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(415, detail="Upload a PDF file.")
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(413, detail="PDF must be 20 MB or smaller.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(415, detail="The uploaded file is not a valid PDF.")


@router.post("/upload/progress")
async def upload_paper_with_progress(file: UploadFile = File(...)):
    data = await file.read(MAX_PDF_BYTES + 1)
    _validate_pdf(file, data)
    async def operation(report: ProgressReporter, db: AsyncSession):
        try:
            return await ingest_upload(db, file.filename, data, report)
        except Exception as exc:
            await report("error", "Process uploaded paper", "error", str(exc))
            raise
    return database_progress_response(operation)


@router.post("/upload", response_model=PaperDetail)
async def ingest_uploaded_paper(response: Response, file: UploadFile = File(...),
                                db: AsyncSession = Depends(get_db_session)):
    data = await file.read(MAX_PDF_BYTES + 1)
    _validate_pdf(file, data)
    try:
        existing = await ingest_upload(db, file.filename, data)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, detail=str(exc)) from exc
    response.status_code = 200 if existing.refreshed else 201
    return existing


@router.post("/{paper_id}/questions/progress")
async def create_questions_with_progress(paper_id: UUID, request: QuestionGenerationRequest):
    async def operation(report: ProgressReporter, db: AsyncSession):
        await report("load", "Load paper context", "running", "Loading the latest revision.")
        try:
            return await generate_and_store_questions(db, paper_id, request.count, report)
        except Exception as exc:
            await report("error", "Generate questions", "error", str(exc))
            raise
    return database_progress_response(operation)


@router.get("/{paper_id}", response_model=PaperDetail)
async def get_paper(paper_id: UUID, db: AsyncSession = Depends(get_db_session)):
    paper = await _load(db, paper_id)
    if not paper:
        raise HTTPException(404, detail="Paper not found.")
    return _serialize(paper)


@router.get("/{paper_id}/versions/{version_id}", response_model=PaperVersionDetail)
async def get_paper_version(paper_id: UUID, version_id: UUID,
                            db: AsyncSession = Depends(get_db_session)):
    version = await db.scalar(select(PaperVersion).options(
        selectinload(PaperVersion.sections), selectinload(PaperVersion.extracted_fields),
        selectinload(PaperVersion.reviews)
    ).where(PaperVersion.id == version_id, PaperVersion.paper_id == paper_id))
    if not version:
        raise HTTPException(404, detail="Paper version not found.")
    return PaperVersionDetail(
        id=version.id, paper_id=version.paper_id, source_forum_id=version.source_forum_id,
        version_key=version.version_key,
        version_timestamp=version.version_timestamp, is_latest=version.is_latest,
        title=version.title, authors=list(version.authors or []),
        abstract=version.abstract, paper_text=version.paper_text,
        pdf_error=(version.raw_metadata or {}).get("pdf_error"),
        sections=[StoredSection(id=s.id, position=s.position, heading=s.heading, content=s.content)
                  for s in sorted(version.sections, key=lambda s: s.position)],
        extracted_fields=[StoredExtractedField(
            id=f.id, field_type=f.field_type, value=f.value,
            extraction_model=f.extraction_model, prompt_version=f.prompt_version,
            section_id=f.section_id,
            section_heading=next((s.heading for s in version.sections if s.id == f.section_id), None),
        ) for f in version.extracted_fields],
        reviews=[StoredReview(
            id=r.id, paper_version_id=r.paper_version_id, openreview_note_id=r.openreview_note_id,
            review_text=r.review_text,
            invitation=r.invitation, written_at=r.written_at,
        ) for r in version.reviews],
    )


@router.post("/{paper_id}/questions", response_model=QuestionGenerationResponse)
async def create_questions(paper_id: UUID, request: QuestionGenerationRequest,
                           db: AsyncSession = Depends(get_db_session)):
    try:
        return await generate_and_store_questions(db, paper_id, request.count)
    except ValueError as exc:
        raise HTTPException(404 if str(exc) == "Paper not found." else 422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, detail=str(exc)) from exc


@router.get("/{paper_id}/questions", response_model=list[StoredQuestion])
async def get_questions(paper_id: UUID, db: AsyncSession = Depends(get_db_session)):
    if not await db.scalar(select(Paper.id).where(Paper.id == paper_id)):
        raise HTTPException(404, detail="Paper not found.")
    rows = await db.scalars(select(Question).where(Question.paper_id == paper_id)
                             .order_by(Question.created_at.desc()))
    return [_question(question) for question in rows]
