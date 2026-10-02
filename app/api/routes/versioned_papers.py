import asyncio
import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.progress import ProgressReporter, progress_response
from app.core.config import get_settings
from app.db.models import ExtractedField, Paper, PaperChunk, PaperSection, PaperVersion, Question, Review
from app.db.session import get_db_session, get_session_factory
from app.schemas.extraction import AbstractExtraction
from app.schemas.papers import (
    OpenReviewIngestRequest, PaperDetail, PaperVersionDetail, PaperVersionSummary, QuestionGenerationRequest,
    QuestionGenerationResponse, StoredExtractedField, StoredQuestion, StoredReview, StoredSection,
)
from app.services.gemini import extract_abstract, generate_questions
from app.services.openreview import (
    OpenReviewContentError, OpenReviewPaper, OpenReviewPDFError, fetch_paper,
    find_matching_forum,
)
from app.services.openreview_archive import RawArchiveError
from app.services.pdf import extract_title_and_abstract

router = APIRouter()
MAX_PDF_BYTES = 20 * 1024 * 1024
CHUNK_SIZE = 3000
CHUNK_OVERLAP = 300


def _chunks(text: str):
    text = text.strip()
    start = 0
    index = 0
    while start < len(text):
        end = min(len(text), start + CHUNK_SIZE)
        if end < len(text):
            boundary = max(text.rfind("\n\n", start, end), text.rfind(". ", start, end), text.rfind(" ", start, end))
            if boundary > start + CHUNK_SIZE // 2:
                end = boundary + (2 if text[boundary:boundary + 2] == ". " else 0)
        part = text[start:end].strip()
        if part:
            yield index, part
            index += 1
        if end >= len(text):
            break
        start = max(start + 1, end - CHUNK_OVERLAP)


def _normalized_field(field_type: str, value):
    return {"text": value} if field_type == "summary" else {"items": value}


async def _load(db: AsyncSession, paper_id: UUID):
    stmt = (select(Paper).options(
        selectinload(Paper.versions).selectinload(PaperVersion.sections),
        selectinload(Paper.versions).selectinload(PaperVersion.extracted_fields),
        selectinload(Paper.versions).selectinload(PaperVersion.reviews),
        selectinload(Paper.versions).selectinload(PaperVersion.chunks),
        selectinload(Paper.questions),
    ).where(Paper.id == paper_id))
    return await db.scalar(stmt)


def _latest(paper: Paper):
    return next((version for version in paper.versions if version.is_latest), None) or (
        max(paper.versions, key=lambda item: item.version_timestamp or 0) if paper.versions else None
    )


def _serialize(paper: Paper, *, from_cache: bool = False, refreshed: bool = False) -> PaperDetail:
    latest = _latest(paper)
    return PaperDetail(
        id=paper.id, source_type=paper.source_type, source_uri=paper.source_uri,
        title=latest.title if latest else paper.title, forum_id=paper.forum_id,
        latest_version_id=latest.id if latest else None,
        full_text=latest.paper_text if latest else None, status=paper.status,
        raw_metadata=paper.raw_metadata, created_at=paper.created_at,
        versions=[PaperVersionSummary(
            id=v.id, version_key=v.version_key, version_timestamp=v.version_timestamp,
            is_latest=v.is_latest, title=v.title, text_characters=len(v.paper_text or ""),
            review_count=len(v.reviews),
        ) for v in sorted(paper.versions, key=lambda v: v.version_timestamp or 0)],
        sections=[StoredSection(id=s.id, position=s.position, heading=s.heading, content=s.content)
                  for s in sorted(latest.sections, key=lambda s: s.position)] if latest else [],
        extracted_fields=[StoredExtractedField(
            id=f.id, field_type=f.field_type, value=f.value,
            extraction_model=f.extraction_model, prompt_version=f.prompt_version,
        ) for f in latest.extracted_fields] if latest else [],
        reviews=[StoredReview(
            id=r.id, openreview_note_id=r.openreview_note_id, review_text=r.review_text,
            invitation=r.invitation, written_at=r.written_at,
        ) for r in sorted(latest.reviews, key=lambda r: r.written_at or datetime.min.replace(tzinfo=timezone.utc))] if latest else [],
        from_cache=from_cache,
        refreshed=refreshed,
    )


async def _store(
    db: AsyncSession, *, source: OpenReviewPaper | None, title: str, abstract: str,
    full_text: str, extraction: AbstractExtraction, source_type: str, source_uri: str,
    metadata: dict, upload_digest: str | None = None,
) -> PaperDetail:
    forum_id = source.forum_id if source else None
    paper = await db.scalar(select(Paper).where(
        (Paper.forum_id == forum_id) if forum_id else (Paper.source_uri == source_uri)
    ))
    was_existing = paper is not None
    if paper is None:
        paper = Paper(forum_id=forum_id, source_type=source_type, source_uri=source_uri)
        db.add(paper)
        await db.flush()
    elif source and paper.source_uri != source_uri:
        paper.source_uri = source_uri

    paper.forum_id = forum_id
    paper.source_type = source_type
    paper.source_uri = source_uri
    paper.title = title
    paper.status = "completed"
    paper.raw_metadata = {
        **(paper.raw_metadata or {}), **metadata,
        "full_text_source": "openreview_pdf" if source else "uploaded_pdf",
        "full_text_characters": len(full_text),
    }
    versions = list(source.versions) if source and source.versions else []
    if not versions:
        version_key = f"upload:{upload_digest}" if upload_digest else "local:current"
        versions = [SimpleNamespace(
            version_id=version_key, version_timestamp=None,
            title=title, abstract=abstract, full_text=full_text, is_latest=True, reviews=(),
        )]

    await db.execute(update(PaperVersion).where(PaperVersion.paper_id == paper.id)
                     .values(is_latest=False))
    await db.flush()
    for item in versions:
        version = await db.scalar(select(PaperVersion).where(
            PaperVersion.paper_id == paper.id, PaperVersion.version_key == item.version_id
        ))
        if version is None:
            version = PaperVersion(paper_id=paper.id, version_key=item.version_id)
            db.add(version)
            await db.flush()
        version.version_timestamp = item.version_timestamp
        version.is_latest = item.is_latest
        version.title = item.title
        version.abstract = item.abstract
        version.paper_text = item.full_text
        version.raw_metadata = {"forum_id": forum_id, "version_id": item.version_id,
                                "full_text_characters": len(item.full_text or "")}
        await db.execute(delete(PaperSection).where(PaperSection.paper_version_id == version.id))
        if item.abstract:
            db.add(PaperSection(
                paper_id=paper.id, paper_version_id=version.id, position=0,
                heading="Abstract", content=item.abstract,
            ))
        await db.execute(delete(Review).where(Review.paper_version_id == version.id))
        for review in item.reviews:
            db.add(Review(
                paper_version_id=version.id, openreview_note_id=review.note_id,
                review_text=review.text, raw_content=review.raw_content,
                invitation=review.invitation, written_at=review.written_at,
                raw_metadata=review.raw_metadata,
            ))
        await db.execute(delete(PaperChunk).where(PaperChunk.paper_version_id == version.id))
        for chunk_index, chunk_text in _chunks(item.full_text or ""):
            db.add(PaperChunk(
                paper_version_id=version.id, chunk_index=chunk_index, chunk_text=chunk_text,
                token_count=None, embedding=None,
            ))
        if item.is_latest:
            await db.execute(delete(ExtractedField).where(ExtractedField.paper_version_id == version.id))
            for field_type, value in extraction.model_dump().items():
                db.add(ExtractedField(
                    paper_version_id=version.id, section_id=None, field_type=field_type,
                    value=_normalized_field(field_type, value),
                    extraction_model=get_settings().gemini_model, prompt_version="abstract-v1",
                ))
    await db.flush()
    await db.execute(update(Question).where(Question.paper_id == paper.id).values(status="stale"))
    await db.commit()
    stored = await _load(db, paper.id)
    if not stored:
        raise HTTPException(status_code=500, detail="Paper was not stored.")
    return _serialize(stored, refreshed=was_existing)


def database_progress_response(operation):
    async def run(report: ProgressReporter):
        async with get_session_factory()() as db:
            return await operation(report, db)
    return progress_response(run)


async def _ingest_forum(db: AsyncSession, forum_id: str, report=None) -> PaperDetail:
    if report:
        stored_id = await db.scalar(select(Paper.id).where(Paper.forum_id == forum_id))
        message = (f"Found stored forum record {stored_id}; refreshing revisions and reviews."
                   if stored_id else "No stored forum record; fetching it for the first time.")
        await report("lookup", "Check stored papers", "success", message)
        await report("lookup", "Fetch from OpenReview", "running",
                     "Fetching all submission edits and forum notes; raw notes are archived in MongoDB.")
    source = await asyncio.to_thread(fetch_paper, forum_id)
    if report:
        await report("lookup", "Fetch from OpenReview", "success",
                     f"Archived versioned notes; loaded {len(source.versions)} revisions and "
                     f"{sum(len(v.reviews) for v in source.versions)} reviews.")
        await report("extraction", "Extract paper fields", "running",
                     "Gemini is extracting structured fields from the latest revision abstract.")
    extraction = await asyncio.to_thread(extract_abstract, source.title, source.abstract)
    if report:
        await report("extraction", "Extract paper fields", "success", "Latest-version extraction completed.")
        await report("save", "Save paper", "running",
                     "Saving forum, revisions, reviews, full text, and text chunks to PostgreSQL.")
    result = await _store(
        db, source=source, title=source.title, abstract=source.abstract,
        full_text=source.full_text, extraction=extraction, source_type="openreview",
        source_uri=source.source_uri,
        metadata={"forum_id": source.forum_id, "public_review_count": source.review_count,
                  "raw_note_count": source.raw_note_count,
                  "raw_fetched_at": source.raw_fetched_at.isoformat() if source.raw_fetched_at else None},
    )
    if report:
        await report("save", "Save paper", "success",
                     "Forum revisions, version-linked reviews, extracted fields, and chunks saved.")
    return result


@router.post("/openreview/progress")
async def ingest_openreview_with_progress(request: OpenReviewIngestRequest):
    async def operation(report: ProgressReporter, db: AsyncSession):
        await report("lookup", "Check stored papers", "running", "Checking the forum identity.")
        try:
            return await _ingest_forum(db, request.forum_id, report)
        except Exception as exc:
            await report("error", "Ingest OpenReview paper", "error", str(exc))
            raise
    return database_progress_response(operation)


@router.post("/openreview", response_model=PaperDetail)
async def ingest_openreview_paper(request: OpenReviewIngestRequest, response: Response,
                                  db: AsyncSession = Depends(get_db_session)):
    try:
        existing = await db.scalar(select(Paper.id).where(Paper.forum_id == request.forum_id))
        result = await _ingest_forum(db, request.forum_id)
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


async def _ingest_upload(db: AsyncSession, file: UploadFile, data: bytes, report=None):
    digest = hashlib.sha256(data).hexdigest()
    if report:
        await report("upload", "Receive PDF", "success", f"Received {file.filename or 'PDF'} ({len(data)//1024} KB).")
        await report("pdf", "Extract PDF text", "running", "Reading text and identifying title, abstract, and authors.")
    uploaded = await asyncio.to_thread(extract_title_and_abstract, data)
    if report:
        await report("pdf", "Extract PDF text", "success",
                     f"Extracted “{uploaded.title}”, {len(uploaded.full_text):,} body characters.",
                     extracted_title=uploaded.title, extracted_authors=list(uploaded.authors))
        await report("match", "Search OpenReview", "running", "Matching title and author evidence to a forum.")
    diagnostics = {}
    try:
        matched = await asyncio.to_thread(find_matching_forum, uploaded.title, diagnostics, authors=uploaded.authors)
    except (RawArchiveError, OpenReviewPDFError):
        raise
    except Exception:
        matched = None
    if matched:
        if report:
            await report("match", "Search OpenReview", "success",
                         f"Matched forum {matched.forum_id} ({matched.match_score or 0:.0%} title similarity); "
                         f"loaded {len(matched.versions)} revisions and {sum(len(v.reviews) for v in matched.versions)} reviews.")
        source, title, abstract, full_text = matched, matched.title, matched.abstract, matched.full_text
        source_uri, source_type = matched.source_uri, "openreview"
        metadata = {"forum_id": matched.forum_id, "uploaded_pdf_sha256": digest,
                    "title_match_score": matched.match_score, "author_match_score": matched.author_match_score}
    else:
        if report:
            await report("match", "Search OpenReview", "warning",
                         "No safe forum match was found; continuing with the uploaded paper.",
                         best_candidate=diagnostics.get("best_title"), best_score=diagnostics.get("best_score"))
        source, title, abstract, full_text = None, uploaded.title, uploaded.abstract, uploaded.full_text
        source_uri, source_type = f"paperprobe-upload://sha256/{digest}", "upload"
        metadata = {"uploaded_filename": file.filename, "uploaded_pdf_sha256": digest, "openreview_match": False}
    if report:
        await report("extraction", "Extract paper fields", "running", "Gemini is extracting from the latest abstract.")
    extraction = await asyncio.to_thread(extract_abstract, title, abstract)
    if report:
        await report("extraction", "Extract paper fields", "success", "Structured extraction completed.")
        await report("save", "Save paper", "running", "Saving version-scoped text, reviews, and chunks.")
    result = await _store(db, source=source, title=title, abstract=abstract, full_text=full_text,
                          extraction=extraction, source_type=source_type, source_uri=source_uri,
                          metadata=metadata, upload_digest=digest)
    if report:
        await report("save", "Save paper", "success", "Paper data saved.")
    return result


@router.post("/upload/progress")
async def upload_paper_with_progress(file: UploadFile = File(...)):
    data = await file.read(MAX_PDF_BYTES + 1)
    _validate_pdf(file, data)
    async def operation(report: ProgressReporter, db: AsyncSession):
        try:
            return await _ingest_upload(db, file, data, report)
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
        existing = await _ingest_upload(db, file, data)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, detail=str(exc)) from exc
    response.status_code = 200 if existing.refreshed else 201
    return existing


def _question(question: Question):
    return StoredQuestion(id=question.id, paper_version_id=question.paper_version_id,
                          text=question.text, status=question.status,
                          source=question.source, critic_notes=question.critic_notes,
                          created_at=question.created_at)


async def _generate(db: AsyncSession, paper_id: UUID, count: int, report=None):
    paper = await _load(db, paper_id)
    if not paper:
        raise ValueError("Paper not found.")
    version = _latest(paper)
    if not version or not version.abstract:
        raise ValueError("This paper has no stored abstract for question generation.")
    if report:
        await report("load", "Load paper context", "success",
                     f"Loaded latest revision {version.version_key} and its extracted fields.")
        await report("generate", "Generate discussion questions", "running", f"Drafting {count} questions.")
    fields = {field.field_type: field.value for field in version.extracted_fields}
    generated = await asyncio.to_thread(generate_questions, version.title or paper.title or "Untitled",
                                        version.abstract, fields, count)
    if report:
        await report("generate", "Generate discussion questions", "success",
                     f"Generated {len(generated.questions)} questions.")
        await report("save", "Save questions", "running", "Saving questions against the latest revision.")
    questions = []
    for item in generated.questions:
        question = Question(paper_id=paper.id, paper_version_id=version.id, text=item.question,
                            status="draft", source="gemini",
                            critic_notes=f"Focus: {item.focus}\nRationale: {item.rationale}")
        db.add(question)
        questions.append(question)
    await db.commit()
    for question in questions:
        await db.refresh(question)
    if report:
        await report("save", "Save questions", "success", "Questions saved.")
    return QuestionGenerationResponse(paper_id=paper.id, questions=[_question(q) for q in questions])


@router.post("/{paper_id}/questions/progress")
async def create_questions_with_progress(paper_id: UUID, request: QuestionGenerationRequest):
    async def operation(report: ProgressReporter, db: AsyncSession):
        await report("load", "Load paper context", "running", "Loading the latest revision.")
        try:
            return await _generate(db, paper_id, request.count, report)
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
        selectinload(PaperVersion.extracted_fields), selectinload(PaperVersion.reviews)
    ).where(PaperVersion.id == version_id, PaperVersion.paper_id == paper_id))
    if not version:
        raise HTTPException(404, detail="Paper version not found.")
    return PaperVersionDetail(
        id=version.id, paper_id=version.paper_id, version_key=version.version_key,
        version_timestamp=version.version_timestamp, is_latest=version.is_latest,
        title=version.title, abstract=version.abstract, paper_text=version.paper_text,
        extracted_fields=[StoredExtractedField(
            id=f.id, field_type=f.field_type, value=f.value,
            extraction_model=f.extraction_model, prompt_version=f.prompt_version,
        ) for f in version.extracted_fields],
        reviews=[StoredReview(
            id=r.id, openreview_note_id=r.openreview_note_id, review_text=r.review_text,
            invitation=r.invitation, written_at=r.written_at,
        ) for r in version.reviews],
    )


@router.post("/{paper_id}/questions", response_model=QuestionGenerationResponse)
async def create_questions(paper_id: UUID, request: QuestionGenerationRequest,
                           db: AsyncSession = Depends(get_db_session)):
    try:
        return await _generate(db, paper_id, request.count)
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

