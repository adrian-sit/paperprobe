"""Local PaperProbe database inspection and maintenance commands."""

import argparse
import asyncio
import json
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import String, cast, delete, func, or_, select, update
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.db.models import ExtractedField, Paper, PaperSection, Question
from app.db.session import get_session_factory
from app.schemas.extraction import AbstractExtraction
from app.services.gemini import extract_abstract
from app.services.openreview import OpenReviewPaper, fetch_paper, find_matching_forum


def confirm(prompt: str, expected: str) -> bool:
    try:
        answer = input(f"{prompt}\nType {expected!r} to continue: ")
    except EOFError:
        return False
    return answer == expected


def field_value(field_type: str, value: object) -> dict:
    return {"text": value} if field_type == "summary" else {"items": value}


async def get_paper(db, paper_id: UUID) -> Paper | None:
    return await db.scalar(
        select(Paper)
        .options(selectinload(Paper.sections), selectinload(Paper.extracted_fields), selectinload(Paper.questions))
        .where(Paper.id == paper_id)
    )


async def run(args: argparse.Namespace) -> None:
    if args.command == "list" and not 1 <= args.limit <= 500:
        raise ValueError("--limit must be between 1 and 500.")
    async with get_session_factory()() as db:
        if args.command == "status":
            counts = {}
            for model in (Paper, PaperSection, ExtractedField, Question):
                counts[model.__tablename__] = await db.scalar(select(func.count()).select_from(model))
            print(json.dumps(counts, indent=2))
            return

        if args.command == "list":
            statement = select(Paper).order_by(Paper.created_at.desc())
            if args.query:
                statement = statement.where(or_(
                    Paper.title.ilike(f"%{args.query}%"),
                    cast(Paper.id, String).ilike(f"{args.query}%"),
                ))
            papers = await db.scalars(statement.limit(args.limit))
            for paper in papers:
                identity = str(paper.id)
                print(f"{identity}\t{paper.source_type}\t{paper.status}\t{paper.title or '(untitled)'}")
            return

        if args.command == "show":
            paper = await get_paper(db, UUID(args.paper_id))
            if not paper:
                raise ValueError(f"Paper {args.paper_id} was not found.")
            print(json.dumps({
                "id": str(paper.id), "source_type": paper.source_type,
                "source_uri": paper.source_uri, "title": paper.title,
                "status": paper.status, "raw_metadata": paper.raw_metadata,
                "created_at": paper.created_at, "updated_at": paper.updated_at,
                "sections": [{"heading": s.heading, "position": s.position, "content": s.content}
                             for s in sorted(paper.sections, key=lambda item: item.position)],
                "extracted_fields": [{"field_type": f.field_type, "value": f.value,
                                      "model": f.extraction_model, "prompt_version": f.prompt_version}
                                     for f in paper.extracted_fields],
                "questions": [{"id": str(q.id), "status": q.status, "text": q.text,
                               "rating": q.rating} for q in paper.questions],
            }, indent=2, default=str))
            return

        if args.command == "reconcile":
            paper = await get_paper(db, UUID(args.paper_id))
            if not paper:
                raise ValueError(f"Paper {args.paper_id} was not found.")
            if args.forum_id:
                source = await asyncio.to_thread(fetch_paper, args.forum_id)
                match_score = None
            else:
                if not paper.title:
                    raise ValueError("This record has no title to search. Supply --forum-id.")
                source = await asyncio.to_thread(find_matching_forum, paper.title)
                if source is None:
                    raise ValueError("No confident OpenReview match found. Retry with --forum-id.")
                match_score = source.match_score
            print(f"Current: {paper.title}\nOpenReview: {source.title}\nForum ID: {source.forum_id}")
            if match_score is not None:
                print(f"Title similarity: {match_score:.0%}")
            duplicate_id = await db.scalar(
                select(Paper.id).where(Paper.source_uri == source.source_uri, Paper.id != paper.id)
            )
            if duplicate_id:
                raise ValueError(f"That forum is already stored as paper {duplicate_id}; no changes made.")
            if not confirm("Refresh this record from OpenReview? Existing generated questions will be marked stale.",
                           f"LINK {paper.id}"):
                print("Cancelled; no database changes made.")
                return
            extraction: AbstractExtraction = await asyncio.to_thread(
                extract_abstract, source.title, source.abstract
            )
            old_source_uri = paper.source_uri
            paper.source_type = "openreview"
            paper.source_uri = source.source_uri
            paper.title = source.title
            paper.raw_metadata = {
                **(paper.raw_metadata or {}),
                "forum_id": source.forum_id,
                "public_review_count": source.review_count,
                "reconciled_from": old_source_uri,
                "reconciled_at": datetime.now(timezone.utc).isoformat(),
                "title_match_score": match_score,
            }
            abstract_section = next(
                (section for section in paper.sections if section.heading == "Abstract"), None
            )
            if abstract_section:
                abstract_section.content = source.abstract
            else:
                paper.sections.append(PaperSection(position=0, heading="Abstract", content=source.abstract))
            await db.execute(delete(ExtractedField).where(ExtractedField.paper_id == paper.id))
            await db.flush()
            await db.refresh(paper, attribute_names=["extracted_fields"])
            settings = get_settings()
            for field_type, value in extraction.model_dump().items():
                db.add(ExtractedField(
                    paper_id=paper.id, section_id=None, field_type=field_type,
                    value=field_value(field_type, value), extraction_model=settings.gemini_model,
                    prompt_version="abstract-v1",
                ))
            await db.execute(
                update(Question).where(Question.paper_id == paper.id).values(status="stale")
            )
            await db.commit()
            print(f"Updated paper {paper.id} from OpenReview forum {source.forum_id}; questions marked stale.")
            return

        if args.command == "delete":
            paper = await get_paper(db, UUID(args.paper_id))
            if not paper:
                raise ValueError(f"Paper {args.paper_id} was not found.")
            if not confirm(f"Delete {paper.title!r} and all related sections, fields, and questions?",
                           f"DELETE {paper.id}"):
                print("Cancelled; no database changes made.")
                return
            for model in (Question, ExtractedField, PaperSection):
                await db.execute(delete(model).where(model.paper_id == paper.id))
            await db.execute(delete(Paper).where(Paper.id == paper.id))
            await db.commit()
            print(f"Deleted paper {paper.id} and its related records.")
            return

        if args.command == "reset":
            counts = {}
            for model in (Paper, PaperSection, ExtractedField, Question):
                counts[model.__tablename__] = await db.scalar(select(func.count()).select_from(model))
            print(f"This removes all PaperProbe rows: {counts}")
            if not confirm("Reset the PaperProbe tables? This cannot be undone.", "RESET PAPERPROBE DATABASE"):
                print("Cancelled; no database changes made.")
                return
            for model in (Question, ExtractedField, PaperSection, Paper):
                await db.execute(delete(model))
            await db.commit()
            print("PaperProbe tables reset. The database and Alembic schema remain in place.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect and maintain the local PaperProbe database.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Show row counts for PaperProbe tables.")
    list_parser = commands.add_parser("list", help="List papers by newest first.")
    list_parser.add_argument("--query", help="Filter by title or paper UUID.")
    list_parser.add_argument("--limit", type=int, default=100, help="Maximum results (1-500).")
    show_parser = commands.add_parser("show", help="Show one complete stored record.")
    show_parser.add_argument("paper_id")
    reconcile_parser = commands.add_parser(
        "reconcile", help="Refresh a stored record from its OpenReview forum."
    )
    reconcile_parser.add_argument("paper_id")
    reconcile_parser.add_argument("--forum-id", help="Use a known forum ID instead of title matching.")
    delete_parser = commands.add_parser("delete", help="Delete one paper and its related records.")
    delete_parser.add_argument("paper_id")
    commands.add_parser("reset", help="Delete all PaperProbe rows while keeping the schema.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(run(args))
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
