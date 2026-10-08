import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.db.models import ExtractedField, Paper, PaperChunk, PaperSection, PaperVersion, Question, Review
from app.schemas.papers import (
    PaperDetail, PaperVersionSummary, QuestionGenerationResponse,
    StoredExtractedField, StoredQuestion, StoredReview, StoredSection,
)
from app.schemas.extraction import QuestionGeneration
from app.services.gemini import SectionExtractedField, extract_section_fields, generate_questions
from app.services.openreview import OpenReviewPaper, fetch_paper, find_matching_forum
from app.services.pdf import extract_title_and_abstract
from app.services.paper_sections import PaperTextSection, parse_paper_sections, section_field_groups

MAX_PDF_BYTES = 20 * 1024 * 1024
CHUNK_SIZE = 3000
CHUNK_OVERLAP = 300


def parse_paper_text(full_text: str, abstract: str = "") -> list[PaperTextSection]:
    """Parse readable paper text into ordered sections.

    Input: full paper text and an optional abstract fallback.
    Output: ordered ``PaperTextSection`` values with position, heading, category,
    and content. This operation is pure and does not access the database or an LLM.
    """
    return parse_paper_sections(full_text, abstract)


def extract_paper_fields(
    title: str, sections: list[PaperTextSection]
) -> list[SectionExtractedField]:
    """Extract section-scoped structured fields from caller-supplied sections.

    Input: paper title and parsed sections. Output: ``SectionExtractedField``
    values containing field type, value, and source section position.
    """
    return extract_section_fields(title, sections)


def generate_questions_from_context(
    title: str, abstract: str, extracted_fields: dict[str, dict], count: int,
    paper_text: str = "",
) -> QuestionGeneration:
    """Generate typed questions from supplied context, without database access."""
    return generate_questions(title, abstract, extracted_fields, count, paper_text)


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


def _merge_extracted_fields(rows: list[ExtractedField]) -> dict[str, dict]:
    """Combine same-type results from multiple source sections for question generation."""
    merged: dict[str, dict] = {}
    for row in rows:
        value = row.value or {}
        if "items" in value:
            target = merged.setdefault(row.field_type, {"items": []})["items"]
            for item in value.get("items") or []:
                if item not in target:
                    target.append(item)
        elif "text" in value:
            target = merged.setdefault(row.field_type, {"text": ""})
            if not target["text"]:
                target["text"] = value.get("text", "")
    return merged


def _extract_section_fields(title: str, abstract: str, full_text: str):
    sections = parse_paper_text(full_text, abstract)
    return sections, extract_paper_fields(title, sections)


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
    section_headings = {section.id: section.heading for section in latest.sections} if latest else {}
    return PaperDetail(
        id=paper.id, source_type=paper.source_type, source_uri=paper.source_uri,
        title=latest.title if latest else paper.title, forum_id=paper.forum_id,
        authors=list(latest.authors or []) if latest else [],
        latest_version_id=latest.id if latest else None,
        full_text=latest.paper_text if latest else None, status=paper.status,
        raw_metadata=paper.raw_metadata, created_at=paper.created_at,
        versions=[PaperVersionSummary(
            id=v.id, source_forum_id=v.source_forum_id,
            version_key=v.version_key, version_timestamp=v.version_timestamp,
            is_latest=v.is_latest, title=v.title, authors=list(v.authors or []),
            text_characters=len(v.paper_text or ""),
            review_count=len(v.reviews), pdf_error=(v.raw_metadata or {}).get("pdf_error"),
        ) for v in sorted(paper.versions, key=lambda v: v.version_timestamp or 0)],
        sections=[StoredSection(id=s.id, position=s.position, heading=s.heading, content=s.content)
                  for s in sorted(latest.sections, key=lambda s: s.position)] if latest else [],
        extracted_fields=[StoredExtractedField(
            id=f.id, field_type=f.field_type, value=f.value,
            extraction_model=f.extraction_model, prompt_version=f.prompt_version,
            section_id=f.section_id, section_heading=section_headings.get(f.section_id),
        ) for f in latest.extracted_fields] if latest else [],
        reviews=[StoredReview(
            id=r.id, paper_version_id=r.paper_version_id, openreview_note_id=r.openreview_note_id,
            review_text=r.review_text,
            invitation=r.invitation, written_at=r.written_at,
        ) for r in sorted(latest.reviews, key=lambda r: r.written_at or datetime.min.replace(tzinfo=timezone.utc))] if latest else [],
        from_cache=from_cache,
        refreshed=refreshed,
    )


async def _store(
    db: AsyncSession, *, source: OpenReviewPaper | None, title: str, abstract: str,
    full_text: str, extraction: list[SectionExtractedField], source_type: str, source_uri: str,
    metadata: dict, upload_digest: str | None = None,
) -> PaperDetail:
    forum_id = source.forum_id if source else None
    source_forum_ids = sorted({
        value for value in ([forum_id] + [getattr(item, "forum_id", None)
                                         for item in (source.versions if source else ())])
        if value
    })
    if source_forum_ids:
        existing_by_forum = select(PaperVersion.paper_id).where(
            PaperVersion.source_forum_id.in_(source_forum_ids)
        )
        paper = await db.scalar(select(Paper).where(or_(
            Paper.forum_id.in_(source_forum_ids), Paper.id.in_(existing_by_forum)
        )).order_by(Paper.created_at).limit(1))
    else:
        paper = await db.scalar(select(Paper).where(Paper.source_uri == source_uri))
    was_existing = paper is not None
    if paper is None:
        paper = Paper(forum_id=forum_id, source_type=source_type, source_uri=source_uri)
        db.add(paper)
        await db.flush()
    elif source and paper.forum_id is None:
        paper.forum_id = forum_id
        paper.source_uri = source_uri

    if paper.forum_id is None:
        paper.forum_id = forum_id
    paper.source_type = source_type
    if not paper.source_uri:
        paper.source_uri = source_uri
    paper.title = title
    paper.status = "completed"
    previous_forums = (paper.raw_metadata or {}).get("related_forum_ids", [])
    if isinstance(previous_forums, str):
        previous_forums = [previous_forums]
    paper.raw_metadata = {
        **(paper.raw_metadata or {}), **metadata,
        "related_forum_ids": sorted(set(previous_forums or []) | set(source_forum_ids)),
        "full_text_source": (
            next((version.full_text_source for version in source.versions if version.is_latest),
                 "openreview_pdf") if source else "uploaded_pdf"
        ),
        "full_text_characters": len(full_text),
        "pdf_text_missing_versions": [
            version.version_id for version in source.versions if version.pdf_error and not version.full_text
        ] if source else [],
    }
    versions = list(source.versions) if source and source.versions else []
    if not versions:
        version_key = f"upload:{upload_digest}" if upload_digest else "local:current"
        versions = [SimpleNamespace(
            version_id=version_key, version_timestamp=None,
            title=title, authors=(), abstract=abstract, full_text=full_text, is_latest=True,
            forum_id=None, reviews=(),
            pdf_error=None, full_text_source="uploaded_pdf",
        )]

    await db.execute(update(PaperVersion).where(PaperVersion.paper_id == paper.id)
                     .values(is_latest=False))
    await db.flush()
    retained_version_ids = []
    for item in versions:
        source_forum_id = getattr(item, "forum_id", None) or forum_id
        version_key = f"{source_forum_id}:{item.version_id}" if source_forum_id else item.version_id
        version = await db.scalar(select(PaperVersion).where(
            PaperVersion.paper_id == paper.id,
            PaperVersion.version_key.in_([version_key, item.version_id]),
        ))
        if version is None:
            version = PaperVersion(paper_id=paper.id, version_key=version_key)
            db.add(version)
            await db.flush()
        else:
            version.version_key = version_key
        retained_version_ids.append(version.id)
        version.source_forum_id = source_forum_id
        version.version_timestamp = item.version_timestamp
        version.is_latest = item.is_latest
        version.title = item.title
        version.authors = list(getattr(item, "authors", ()) or ())
        version.abstract = item.abstract
        version.paper_text = item.full_text or None
        version.raw_metadata = {"forum_id": source_forum_id, "version_id": item.version_id,
                                "full_text_characters": len(item.full_text or ""),
                                "pdf_error": item.pdf_error,
                                "full_text_source": item.full_text_source}
        # Provenance points at section rows, so remove stale field rows before
        # replacing the section set. Only the latest revision is re-extracted.
        await db.execute(delete(ExtractedField).where(ExtractedField.paper_version_id == version.id))
        await db.execute(delete(PaperSection).where(PaperSection.paper_version_id == version.id))
        section_rows = []
        parsed_sections = parse_paper_text(item.full_text or "", item.abstract or "")
        for section in parsed_sections:
            section_row = PaperSection(
                paper_id=paper.id, paper_version_id=version.id, position=section.position,
                heading=section.heading, content=section.content,
            )
            db.add(section_row)
            section_rows.append(section_row)
        await db.flush()
        section_ids = {section.position: section.id for section in section_rows}
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
            for extracted in extraction:
                db.add(ExtractedField(
                    paper_version_id=version.id,
                    section_id=section_ids.get(extracted.section_position),
                    field_type=extracted.field_type,
                    value=_normalized_field(extracted.field_type, extracted.value),
                    extraction_model=get_settings().gemini_model, prompt_version="section-v1",
                ))
    if source_forum_ids and retained_version_ids:
        await db.execute(delete(PaperVersion).where(
            PaperVersion.paper_id == paper.id,
            PaperVersion.source_forum_id.in_(source_forum_ids),
            PaperVersion.id.not_in(retained_version_ids),
        ))
    await db.flush()
    await db.execute(update(Question).where(Question.paper_id == paper.id).values(status="stale"))
    await db.commit()
    stored = await _load(db, paper.id)
    if not stored:
        raise RuntimeError("Paper was not stored.")
    return _serialize(stored, refreshed=was_existing)


async def ingest_forum(db: AsyncSession, forum_id: str, report=None) -> PaperDetail:
    if report:
        stored_id = await db.scalar(select(Paper.id).outerjoin(PaperVersion).where(or_(
            Paper.forum_id == forum_id, PaperVersion.source_forum_id == forum_id
        )).limit(1))
        message = (f"Found stored forum record {stored_id}; refreshing revisions and reviews."
                   if stored_id else "No stored forum record; fetching it for the first time.")
        await report("lookup", "Check stored papers", "success", message)
        await report("lookup", "Fetch from OpenReview", "running",
                     "Fetching all submission edits and forum notes; raw notes are archived in MongoDB.")
    seed = await asyncio.to_thread(fetch_paper, forum_id)
    latest_seed = max(seed.versions,
                      key=lambda item: (item.version_timestamp or 0, item.version_id),
                      default=None)
    source = await asyncio.to_thread(
        find_matching_forum, seed.title, {},
        authors=latest_seed.authors if latest_seed else (),
        include_forum_id=forum_id, seed_paper=seed,
    )
    source = source or seed
    if report:
        missing_pdf_count = sum(bool(version.pdf_error) for version in source.versions)
        fetch_state = "warning" if missing_pdf_count else "success"
        source_forum_ids = sorted({version.forum_id for version in source.versions if version.forum_id})
        fetch_detail = (f"Archived {len(source_forum_ids)} matching forum(s) "
                       f"({', '.join(source_forum_ids)}); loaded {len(source.versions)} "
                       f"revisions and {sum(len(v.reviews) for v in source.versions)} reviews.")
        if missing_pdf_count:
            fetch_detail += (f" PDF text was unavailable for {missing_pdf_count} revision(s); "
                             "continuing with OpenReview title, abstract, and reviews.")
        await report("lookup", "Fetch from OpenReview", fetch_state,
                     fetch_detail)
        await report("extraction", "Extract paper fields", "running",
                     "Parsing the latest revision into named sections, then extracting fields section by section.")
    parsed_sections, extraction = await asyncio.to_thread(
        _extract_section_fields, source.title, source.abstract, source.full_text
    )
    if report:
        call_sections = section_field_groups(parsed_sections)
        names = ", ".join(section.heading for section, _fields in call_sections) or "none"
        await report("extraction", "Extract paper fields", "success",
                     f"Completed {len(call_sections)} focused Gemini call(s) for: {names}.")
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


async def ingest_upload(
    db: AsyncSession, filename: str | None, data: bytes, report=None
) -> PaperDetail:
    digest = hashlib.sha256(data).hexdigest()
    if report:
        await report("upload", "Receive PDF", "success", f"Received {filename or 'PDF'} ({len(data)//1024} KB).")
        await report("pdf", "Extract PDF text", "running", "Reading text and identifying title, abstract, and authors.")
    uploaded = await asyncio.to_thread(extract_title_and_abstract, data)
    if report:
        await report("pdf", "Extract PDF text", "success",
                     f"Extracted “{uploaded.title}”, {len(uploaded.full_text):,} body characters.",
                     extracted_title=uploaded.title, extracted_authors=list(uploaded.authors))
        await report("match", "Search OpenReview", "running", "Matching title and author evidence to a forum.")
    diagnostics = {}
    match_error = None
    try:
        matched = await asyncio.to_thread(find_matching_forum, uploaded.title, diagnostics, authors=uploaded.authors)
    except Exception as exc:
        # A failed OpenReview lookup must not discard text already extracted
        # from the user's PDF. Continue as a local upload in that case.
        match_error = str(exc)
        matched = None
    if matched:
        # The upload is the guaranteed source of readable text in this flow.
        # Keep that text on the matched latest revision even if OpenReview's
        # PDF endpoint succeeds but returns a different/unusable payload.
        versions = tuple(
            replace(version, full_text=uploaded.full_text, full_text_source="uploaded_pdf")
            if version.is_latest else version
            for version in matched.versions
        )
        matched = replace(matched, full_text=uploaded.full_text, versions=versions)
        if report:
            missing_pdf_count = sum(bool(version.pdf_error and not version.full_text)
                                    for version in matched.versions)
            detail = (f"Matched forum {matched.forum_id} ({matched.match_score or 0:.0%} title similarity); "
                      f"combined {len({v.forum_id for v in matched.versions if v.forum_id})} matching forum(s), "
                      f"loaded {len(matched.versions)} revisions and "
                      f"{sum(len(v.reviews) for v in matched.versions)} reviews.")
            if matched.versions and next(v for v in matched.versions if v.is_latest).full_text_source == "uploaded_pdf":
                detail += " Using the uploaded PDF as the latest revision's full-text source."
            if missing_pdf_count:
                detail += (f" PDF text was unavailable for {missing_pdf_count} revision(s); "
                           "continuing with title, abstract, and reviews.")
            await report("match", "Search OpenReview", "warning" if missing_pdf_count else "success", detail)
        source, title, abstract, full_text = matched, matched.title, matched.abstract, matched.full_text
        source_uri, source_type = matched.source_uri, "openreview"
        metadata = {"forum_id": matched.forum_id, "uploaded_pdf_sha256": digest,
                    "title_match_score": matched.match_score, "author_match_score": matched.author_match_score}
    else:
        if report:
            detail = "No safe forum match was found; continuing with the uploaded paper."
            if match_error:
                detail = f"OpenReview lookup failed ({match_error}); continuing with the uploaded paper."
            await report("match", "Search OpenReview", "warning", detail,
                         best_candidate=diagnostics.get("best_title"), best_score=diagnostics.get("best_score"))
        source, title, abstract, full_text = None, uploaded.title, uploaded.abstract, uploaded.full_text
        source_uri, source_type = f"paperprobe-upload://sha256/{digest}", "upload"
        metadata = {"uploaded_filename": filename, "uploaded_pdf_sha256": digest, "openreview_match": False}
    if report:
        await report("extraction", "Extract paper fields", "running",
                     "Parsing uploaded full text into named sections, then extracting fields section by section.")
    parsed_sections, extraction = await asyncio.to_thread(
        _extract_section_fields, title, abstract, full_text
    )
    if report:
        call_sections = section_field_groups(parsed_sections)
        names = ", ".join(section.heading for section, _fields in call_sections) or "none"
        await report("extraction", "Extract paper fields", "success",
                     f"Completed {len(call_sections)} focused Gemini call(s) for: {names}.")
        await report("save", "Save paper", "running", "Saving version-scoped text, reviews, and chunks.")
    result = await _store(db, source=source, title=title, abstract=abstract, full_text=full_text,
                          extraction=extraction, source_type=source_type, source_uri=source_uri,
                          metadata=metadata, upload_digest=digest)
    if report:
        await report("save", "Save paper", "success", "Paper data saved.")
    return result


def _question(question: Question):
    return StoredQuestion(id=question.id, paper_version_id=question.paper_version_id,
                          text=question.text, status=question.status,
                          source=question.source, critic_notes=question.critic_notes,
                          created_at=question.created_at)


async def generate_and_store_questions(db: AsyncSession, paper_id: UUID, count: int, report=None):
    paper = await _load(db, paper_id)
    if not paper:
        raise ValueError("Paper not found.")
    version = _latest(paper)
    if not version or (not version.abstract and not version.extracted_fields):
        raise ValueError("This paper has no abstract or extracted fields for question generation.")
    if report:
        await report("load", "Load paper context", "success",
                     f"Loaded latest revision {version.version_key} and its extracted fields.")
        await report("generate", "Generate discussion questions", "running", f"Drafting {count} questions.")
    fields = _merge_extracted_fields(version.extracted_fields)
    generated = await asyncio.to_thread(
        generate_questions_from_context, version.title or paper.title or "Untitled",
        version.abstract or "", fields, count,
    )
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




