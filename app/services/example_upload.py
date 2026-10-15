"""Store uploaded paper/question examples for paper-conditioned retrieval."""

import asyncio
from pathlib import Path
from typing import Literal
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import ExamplePair
from app.schemas.examples import ExamplePairUploadQuestion, ExamplePairUploadResponse
from app.services.example_retrieval import embed_example_paper_document
from app.services.pdf import extract_title_and_abstract

MAX_EXAMPLE_QUESTIONS = 20
MAX_EMBEDDING_CONTEXT_CHARS = 6000
ExampleSourceType = Literal["own_criticism", "openreview_review"]


async def upload_example_pairs(
    db: AsyncSession,
    *,
    filename: str | None,
    pdf_bytes: bytes,
    source_type: ExampleSourceType,
    questions: list[str],
    rationales: list[str] | None = None,
) -> ExamplePairUploadResponse:
    """Extract one source paper, embed it once, and store each supplied question as a pair."""
    if not 1 <= len(questions) <= MAX_EXAMPLE_QUESTIONS:
        raise ValueError(f"Provide between 1 and {MAX_EXAMPLE_QUESTIONS} questions for this paper.")
    cleaned_questions = [question.strip() for question in questions]
    if any(not question for question in cleaned_questions):
        raise ValueError("Question entries cannot be blank.")
    if rationales is not None and len(rationales) != len(questions):
        raise ValueError("Provide one rationale field per question, or omit all rationales.")
    cleaned_rationales = [value.strip() or None for value in rationales or [""] * len(questions)]

    uploaded = await asyncio.to_thread(extract_title_and_abstract, pdf_bytes)
    title = uploaded.title
    if title == "Untitled paper" and filename:
        title = Path(filename).stem.strip() or title
    paper_context = (uploaded.abstract.strip() or uploaded.full_text.strip())[:MAX_EMBEDDING_CONTEXT_CHARS]
    if not paper_context:
        raise ValueError("The PDF did not contain readable abstract or paper text.")

    representative_text = f"Title: {title}\n\n{paper_context}"
    embedding, embedding_model = await embed_example_paper_document(representative_text)
    source_paper_id = uuid4()
    rows = [
        ExamplePair(
            source_paper_id=source_paper_id,
            source_title=title,
            source_abstract_or_claims=paper_context,
            paper_embedding=embedding,
            question_text=question,
            rationale=rationale,
            source_type=source_type,
            embedding_model=embedding_model,
        )
        for question, rationale in zip(cleaned_questions, cleaned_rationales)
    ]
    db.add_all(rows)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return ExamplePairUploadResponse(
        source_paper_id=source_paper_id,
        source_title=title,
        source_type=source_type,
        question_count=len(rows),
        questions=[ExamplePairUploadQuestion(
            id=row.id,
            question_text=row.question_text,
            rationale=row.rationale,
        ) for row in rows],
    )
