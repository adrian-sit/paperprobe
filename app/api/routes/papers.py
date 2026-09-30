import asyncio
import hashlib
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from app.api.progress import ProgressReporter, progress_response
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.db.models import ExtractedField, Paper, PaperSection, Question
from app.db.session import get_db_session, get_session_factory
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
    find_matching_forum,
)
from app.services.pdf import extract_title_and_abstract

router = APIRouter()
MAX_PDF_BYTES = 20 * 1024 * 1024


def database_progress_response(operation):
    async def run(report: ProgressReporter):
        async with get_session_factory()() as db:
            return await operation(report, db)
    return progress_response(run)


async def persist_paper(db: AsyncSession, title: str, abstract: str, extraction: AbstractExtraction,
                        source_type: str, source_uri: str, metadata: dict) -> PaperDetail:
    paper = Paper(source_type=source_type, source_uri=source_uri, title=title,
                  status="completed", raw_metadata=metadata)
    paper.sections.append(PaperSection(position=0, heading="Abstract", content=abstract))
    model = get_settings().gemini_model
    for field_type, value in extraction.model_dump().items():
        normalized = {"text": value} if field_type == "summary" else {"items": value}
        paper.extracted_fields.append(ExtractedField(section_id=None, field_type=field_type,
            value=normalized, extraction_model=model, prompt_version="abstract-v1"))
    db.add(paper)
    await db.commit()
    stored = await load_paper(db, paper.id)
    if not stored:
        raise HTTPException(status_code=500, detail="Paper was not stored.")
    return serialize_paper(stored)


async def refresh_existing_paper(
    db: AsyncSession, paper: Paper, title: str, abstract: str,
    extraction: AbstractExtraction, source_type: str, source_uri: str, metadata: dict,
) -> PaperDetail:
    """Refresh an uploaded row in place after a recheck or OpenReview match."""
    paper.title = title
    paper.source_type = source_type
    paper.source_uri = source_uri
    paper.status = "completed"
    paper.raw_metadata = metadata
    abstract_section = next((section for section in paper.sections if section.heading == "Abstract"), None)
    if abstract_section:
        abstract_section.content = abstract
    else:
        position = max((section.position for section in paper.sections), default=-1) + 1
        paper.sections.append(PaperSection(position=position, heading="Abstract", content=abstract))
    await db.execute(delete(ExtractedField).where(ExtractedField.paper_id == paper.id))
    await db.flush()
    await db.refresh(paper, attribute_names=["extracted_fields"])
    for field_type, value in extraction.model_dump().items():
        normalized = {"text": value} if field_type == "summary" else {"items": value}
        db.add(ExtractedField(paper_id=paper.id, field_type=field_type, value=normalized,
            extraction_model=get_settings().gemini_model, prompt_version="abstract-v1"))
    await db.execute(update(Question).where(Question.paper_id == paper.id).values(status="stale"))
    await db.commit()
    stored = await load_paper(db, paper.id)
    if not stored:
        raise HTTPException(status_code=500, detail="Refreshed paper was not stored.")
    return serialize_paper(stored)


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

    stored = await persist_paper(db, source.title, source.abstract, extraction, "openreview",
        source.source_uri, {"forum_id": source.forum_id, "public_review_count": source.review_count})
    response.status_code = status.HTTP_201_CREATED
    response.headers["X-PaperProbe-Cache"] = "miss"
    return stored


@router.post("/upload", response_model=PaperDetail)
async def ingest_uploaded_paper(
    response: Response,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db_session),
) -> PaperDetail:
    """Match an uploaded PDF to OpenReview, then use the same extraction pipeline."""
    if file.content_type not in {"application/pdf", "application/octet-stream"} and not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=415, detail="Upload a PDF file.")
    data = await file.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDF must be 20 MB or smaller.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=415, detail="The uploaded file is not a valid PDF.")
    digest = hashlib.sha256(data).hexdigest()
    upload_uri = f"paperprobe-upload://sha256/{digest}"
    existing_id = await db.scalar(select(Paper.id).where(Paper.source_uri == upload_uri))
    if existing_id:
        existing = await load_paper(db, existing_id)
        if existing:
            response.headers["X-PaperProbe-Cache"] = "hit"
            return serialize_paper(existing, from_cache=True)
    try:
        uploaded = await asyncio.to_thread(extract_title_and_abstract, data)
        try:
            matched = await asyncio.to_thread(
                find_matching_forum, uploaded.title,
                authors=uploaded.authors, abstract=uploaded.abstract,
            )
        except Exception:
            # OpenReview lookup is opportunistic; the uploaded paper can still be analyzed.
            matched = None
        if matched:
            existing_id = await db.scalar(select(Paper.id).where(Paper.source_uri == matched.source_uri))
            if existing_id:
                existing = await load_paper(db, existing_id)
                if existing:
                    response.headers["X-PaperProbe-Cache"] = "hit"
                    return serialize_paper(existing, from_cache=True)
            title, abstract = matched.title, matched.abstract
            source_type, source_uri = "openreview", matched.source_uri
            metadata = {"forum_id": matched.forum_id, "public_review_count": matched.review_count,
                        "uploaded_pdf_sha256": digest, "title_match_score": matched.match_score}
        else:
            title, abstract = uploaded.title, uploaded.abstract
            source_type, source_uri = "upload", upload_uri
            metadata = {"uploaded_filename": file.filename, "uploaded_pdf_sha256": digest,
                        "openreview_match": False}
        extraction = await asyncio.to_thread(extract_abstract, title, abstract)
        result = await persist_paper(db, title, abstract, extraction, source_type, source_uri, metadata)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    response.status_code = status.HTTP_201_CREATED
    response.headers["X-PaperProbe-Cache"] = "miss"
    return result


@router.post("/openreview/progress")
async def ingest_openreview_with_progress(
    request: OpenReviewIngestRequest,
):
    async def operation(report: ProgressReporter, db: AsyncSession) -> PaperDetail:
        await report("lookup", "Check stored papers", "running", "Looking for a cached forum record.")
        source_uri = forum_source_uri(request.forum_id)
        existing_id = await db.scalar(select(Paper.id).where(Paper.source_uri == source_uri))
        if existing_id:
            existing = await load_paper(db, existing_id)
            if existing:
                await report("lookup", "Check stored papers", "success", "Loaded the cached paper.")
                return serialize_paper(existing, from_cache=True)
        await report("lookup", "Fetch from OpenReview", "running", "Fetching submission and public reviews.")
        source = await asyncio.to_thread(fetch_paper, request.forum_id)
        await report("lookup", "Fetch from OpenReview", "success", f"Fetched “{source.title}”.")
        await report("extraction", "Extract paper fields", "running", "Gemini is extracting structured fields from the abstract.")
        extraction = await asyncio.to_thread(extract_abstract, source.title, source.abstract)
        await report("extraction", "Extract paper fields", "success", "Structured extraction completed.")
        await report("save", "Save paper", "running", "Writing the paper and extraction to PostgreSQL.")
        result = await persist_paper(db, source.title, source.abstract, extraction, "openreview",
            source.source_uri, {"forum_id": source.forum_id, "public_review_count": source.review_count})
        await report("save", "Save paper", "success", "Paper and extracted fields saved.")
        return result
    return database_progress_response(operation)


@router.post("/upload/progress")
async def upload_paper_with_progress(
    file: UploadFile = File(...),
):
    if file.content_type not in {"application/pdf", "application/octet-stream"} and not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=415, detail="Upload a PDF file.")
    data = await file.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDF must be 20 MB or smaller.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=415, detail="The uploaded file is not a valid PDF.")
    digest = hashlib.sha256(data).hexdigest()
    upload_uri = f"paperprobe-upload://sha256/{digest}"

    async def operation(report: ProgressReporter, db: AsyncSession) -> PaperDetail:
        await report("upload", "Receive PDF", "success", f"Received {file.filename or 'PDF'} ({len(data) // 1024} KB).")
        await report("pdf", "Extract PDF text", "running", "Reading the paper title and abstract.")
        uploaded = await asyncio.to_thread(extract_title_and_abstract, data)
        author_detail = (f" Likely authors: {', '.join(uploaded.authors[:8])}."
                         if uploaded.authors else " No author names were identified.")
        await report("pdf", "Extract PDF text", "success", f"Title and abstract extracted.{author_detail}",
                     extracted_title=uploaded.title,
                     extracted_authors=list(uploaded.authors),
                     extracted_abstract_characters=len(uploaded.abstract))
        await report("cache", "Check stored papers", "running", "Checking for a previous record of this PDF.")
        existing_id = await db.scalar(select(Paper.id).where(Paper.source_uri == upload_uri))
        cached_upload = None
        has_existing_record = False
        if existing_id:
            existing = await load_paper(db, existing_id)
            if existing and existing.source_type == "openreview":
                await report("cache", "Check stored papers", "success", "This exact PDF was already processed.")
                return serialize_paper(existing, from_cache=True)
            if existing:
                has_existing_record = True
                cached_upload = existing
                await report("cache", "Check stored papers", "success",
                             "Found a PDF-only record; rechecking its title and OpenReview match.")
        if not has_existing_record:
            await report("cache", "Check stored papers", "success", "No previous PDF record found; continuing with this upload.")
        await report("match", "Search OpenReview", "running", "Checking for a forum with the same title.")
        match_diagnostics: dict = {}
        try:
            matched = await asyncio.to_thread(
                find_matching_forum, uploaded.title, match_diagnostics,
                authors=uploaded.authors, abstract=uploaded.abstract,
            )
        except Exception as exc:
            matched = None
            await report("match", "Search OpenReview", "warning", f"Lookup unavailable; continuing with PDF text ({exc}).")
        if matched:
            confidence = f" ({matched.match_score:.0%} title similarity)" if matched.match_score is not None else ""
            evidence = match_diagnostics.get("author_abstract_score")
            evidence_note = f" Author/abstract comparison: {evidence:.0%}." if evidence is not None else ""
            await report("match", "Search OpenReview", "success",
                         f"Matched forum {matched.forum_id}{confidence};{evidence_note} using its stored submission.")
            existing_id = await db.scalar(select(Paper.id).where(Paper.source_uri == matched.source_uri))
            if existing_id:
                existing = await load_paper(db, existing_id)
                if existing:
                    return serialize_paper(existing, from_cache=True)
            title, abstract = matched.title, matched.abstract
            source_type, source_uri = "openreview", matched.source_uri
            metadata = {"forum_id": matched.forum_id, "public_review_count": matched.review_count,
                        "uploaded_pdf_sha256": digest, "title_match_score": matched.match_score}
        else:
            detail = (f"Searched {match_diagnostics.get('queries', 0)} title phrases; "
                      f"OpenReview returned {match_diagnostics.get('candidates', 0)} candidate(s).")
            if match_diagnostics.get("best_title"):
                detail += (f" Best candidate: “{match_diagnostics['best_title']}” "
                           f"({match_diagnostics['best_score']:.0%} title similarity).")
            if match_diagnostics.get("author_abstract_score") is not None:
                detail += (f" Author/abstract comparison: "
                           f"{match_diagnostics['author_abstract_score']:.0%}.")
            if match_diagnostics.get("query_failures"):
                detail += f" {len(match_diagnostics['query_failures'])} search request(s) failed."
            detail += " No candidate met the safe matching threshold; using the uploaded paper."
            await report("match", "Search OpenReview", "warning", detail,
                         best_candidate=match_diagnostics.get("best_title"),
                         best_score=match_diagnostics.get("best_score"),
                         candidate_count=match_diagnostics.get("candidates", 0))
            title, abstract = uploaded.title, uploaded.abstract
            source_type, source_uri = "upload", upload_uri
            metadata = {"uploaded_filename": file.filename, "uploaded_pdf_sha256": digest,
                        "openreview_match": False}
        await report("extraction", "Extract paper fields", "running", "Gemini is extracting structured fields.")
        extraction = await asyncio.to_thread(extract_abstract, title, abstract)
        await report("extraction", "Extract paper fields", "success", "Structured extraction completed.")
        await report("save", "Save paper", "running", "Writing the paper and extraction to PostgreSQL.")
        if cached_upload:
            result = await refresh_existing_paper(
                db, cached_upload, title, abstract, extraction, source_type, source_uri, metadata
            )
        else:
            result = await persist_paper(db, title, abstract, extraction, source_type, source_uri, metadata)
        await report("save", "Save paper", "success", "Paper and extracted fields saved.")
        return result
    return database_progress_response(operation)


@router.post("/{paper_id}/questions/progress")
async def create_questions_with_progress(
    paper_id: UUID,
    request: QuestionGenerationRequest,
):
    async def operation(report: ProgressReporter, db: AsyncSession) -> QuestionGenerationResponse:
        await report("load", "Load paper context", "running", "Loading the saved abstract and extracted fields.")
        paper = await load_paper(db, paper_id)
        if not paper:
            raise ValueError("Paper not found.")
        abstract = next((section.content for section in paper.sections if section.heading == "Abstract"), None)
        if not abstract:
            raise ValueError("This paper has no stored abstract for question generation.")
        field_context = {field.field_type: field.value for field in paper.extracted_fields}
        await report("load", "Load paper context", "success", "Paper context is ready.")
        await report("generate", "Generate discussion questions", "running", f"Gemini is drafting {request.count} questions.")
        generated = await asyncio.to_thread(
            generate_questions, paper.title or "Untitled paper", abstract, field_context, request.count
        )
        await report("generate", "Generate discussion questions", "success", f"Generated {len(generated.questions)} questions.")
        await report("save", "Save questions", "running", "Saving the generated questions to PostgreSQL.")
        stored_questions = []
        for item in generated.questions:
            question = Question(paper_id=paper.id, text=item.question, status="draft", source="gemini",
                critic_notes=f"Focus: {item.focus}\nRationale: {item.rationale}")
            db.add(question)
            stored_questions.append(question)
        await db.commit()
        for question in stored_questions:
            await db.refresh(question)
        await report("save", "Save questions", "success", "Questions saved.")
        return QuestionGenerationResponse(
            paper_id=paper.id, questions=[serialize_question(question) for question in stored_questions]
        )
    return database_progress_response(operation)


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
