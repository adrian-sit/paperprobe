"""Read and maintain source papers and question pairs in the example library."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ExamplePair
from app.schemas.examples import (
    ExamplePaperLibraryItem,
    ExamplePaperLibraryResponse,
    ExamplePairUploadQuestion,
)
from app.services.example_upload import MAX_EXAMPLE_QUESTIONS


def _virtual_source_id(pair: ExamplePair) -> UUID:
    # Older rows may have no group id. Treat each such row as its own source paper.
    return pair.source_paper_id or pair.id


async def list_example_papers(db: AsyncSession) -> ExamplePaperLibraryResponse:
    """Return all source papers, each with its currently stored questions."""
    pairs = (await db.scalars(
        select(ExamplePair).order_by(ExamplePair.source_title, ExamplePair.created_at, ExamplePair.id)
    )).all()
    grouped: dict[UUID, list[ExamplePair]] = {}
    for pair in pairs:
        grouped.setdefault(_virtual_source_id(pair), []).append(pair)

    return ExamplePaperLibraryResponse(papers=[
        ExamplePaperLibraryItem(
            source_paper_id=source_id,
            source_title=items[0].source_title,
            source_abstract_or_claims=items[0].source_abstract_or_claims,
            source_type=items[0].source_type,
            questions=[ExamplePairUploadQuestion(
                id=item.id, question_text=item.question_text, rationale=item.rationale
            ) for item in items],
        )
        for source_id, items in grouped.items()
    ])


async def _get_source_rows(db: AsyncSession, source_paper_id: UUID) -> list[ExamplePair]:
    rows = (await db.scalars(
        select(ExamplePair)
        .where(ExamplePair.source_paper_id == source_paper_id)
        .order_by(ExamplePair.created_at, ExamplePair.id)
    )).all()
    if rows:
        return list(rows)

    # Upgrade a legacy single-question row to the current grouped representation
    # the first time a user adds another question to it.
    legacy = await db.get(ExamplePair, source_paper_id)
    return [legacy] if legacy is not None and legacy.source_paper_id is None else []


async def add_question_to_example_paper(
    db: AsyncSession,
    source_paper_id: UUID,
    *,
    question_text: str,
    rationale: str | None,
) -> ExamplePairUploadQuestion:
    """Add one question to an existing source paper, reusing its embedding."""
    cleaned_question = question_text.strip()
    if not cleaned_question:
        raise ValueError("Question text cannot be blank.")
    rows = await _get_source_rows(db, source_paper_id)
    if not rows:
        raise LookupError("Example source paper was not found.")
    if len(rows) >= MAX_EXAMPLE_QUESTIONS:
        raise ValueError(f"A source paper can have no more than {MAX_EXAMPLE_QUESTIONS} questions.")

    source = rows[0]
    new_pair = ExamplePair(
        source_paper_id=source_paper_id,
        source_title=source.source_title,
        source_abstract_or_claims=source.source_abstract_or_claims,
        paper_embedding=source.paper_embedding,
        question_text=cleaned_question,
        rationale=(rationale or "").strip() or None,
        source_type=source.source_type,
        embedding_model=source.embedding_model,
    )
    db.add(new_pair)
    await db.commit()
    await db.refresh(new_pair)
    return ExamplePairUploadQuestion(
        id=new_pair.id, question_text=new_pair.question_text, rationale=new_pair.rationale
    )


async def update_example_question(
    db: AsyncSession,
    question_id: UUID,
    *,
    question_text: str,
    rationale: str | None,
) -> ExamplePairUploadQuestion:
    """Update the text and rationale of one existing example question."""
    cleaned_question = question_text.strip()
    if not cleaned_question:
        raise ValueError("Question text cannot be blank.")
    pair = await db.get(ExamplePair, question_id)
    if pair is None:
        raise LookupError("Example question was not found.")
    pair.question_text = cleaned_question
    pair.rationale = (rationale or "").strip() or None
    await db.commit()
    return ExamplePairUploadQuestion(
        id=pair.id, question_text=pair.question_text, rationale=pair.rationale
    )


async def delete_example_question(db: AsyncSession, question_id: UUID) -> None:
    """Remove one question example while retaining the paper's other questions."""
    pair = await db.get(ExamplePair, question_id)
    if pair is None:
        raise LookupError("Example question was not found.")
    await db.delete(pair)
    await db.commit()
