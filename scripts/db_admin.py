import argparse
import asyncio
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Paper, PaperChunk, PaperSection, PaperVersion, Question, Review, ExtractedField
from app.db.session import get_session_factory


def confirm(prompt: str, phrase: str) -> bool:
    print(prompt)
    return input(f"Type {phrase!r} to continue: ").strip() == phrase


async def run(args):
    async with get_session_factory()() as db:
        if args.command == "status":
            for model in (Paper, PaperVersion, PaperSection, ExtractedField, Review, PaperChunk, Question):
                count = await db.scalar(select(func.count()).select_from(model))
                print(f"{model.__tablename__}: {count}")
            return
        if args.command == "list":
            stmt = select(Paper).order_by(Paper.created_at.desc()).limit(max(1, min(args.limit, 500)))
            if args.query:
                stmt = stmt.where(Paper.title.ilike(f"%{args.query}%") | Paper.id.cast(str).ilike(f"%{args.query}%"))
            for paper in await db.scalars(stmt):
                versions = await db.scalar(select(func.count()).select_from(PaperVersion).where(PaperVersion.paper_id == paper.id))
                print(f"{paper.id} | {paper.title or 'Untitled'} | forum={paper.forum_id or '-'} | revisions={versions} | {paper.source_type}")
            return
        if args.command == "show":
            paper = await db.get(Paper, UUID(args.paper_id))
            if not paper:
                raise ValueError("Paper was not found.")
            print(f"Paper: {paper.id}\nTitle: {paper.title}\nForum: {paper.forum_id}\nSource: {paper.source_uri}")
            versions = await db.scalars(select(PaperVersion).where(PaperVersion.paper_id == paper.id)
                                        .order_by(PaperVersion.version_timestamp))
            for version in versions:
                review_count = await db.scalar(select(func.count()).select_from(Review).where(Review.paper_version_id == version.id))
                chunk_count = await db.scalar(select(func.count()).select_from(PaperChunk).where(PaperChunk.paper_version_id == version.id))
                print(f"  {version.version_key} | latest={version.is_latest} | timestamp={version.version_timestamp} "
                      f"| text chars={len(version.paper_text or '')} | reviews={review_count} | chunks={chunk_count}")
            return
        if args.command == "reconcile":
            from app.api.routes.versioned_papers import _ingest_forum
            from app.services.openreview import find_matching_forum, forum_source_uri
            paper = await db.get(Paper, UUID(args.paper_id))
            if not paper:
                raise ValueError("Paper was not found.")
            forum_id = args.forum_id or paper.forum_id
            if not forum_id:
                matched = await asyncio.to_thread(find_matching_forum, paper.title or "")
                if not matched:
                    raise ValueError("No confident OpenReview match found; supply --forum-id.")
                forum_id = matched.forum_id
            if not confirm(f"Refresh {paper.title!r} from forum {forum_id}? Latest questions will be marked stale.",
                           f"REFRESH {paper.id}"):
                print("Cancelled.")
                return
            refreshed = await _ingest_forum(db, forum_id)
            if paper.forum_id is None and refreshed.id != paper.id:
                temporary = await db.get(Paper, refreshed.id)
                if temporary:
                    await db.execute(update(PaperVersion).where(PaperVersion.paper_id == temporary.id)
                                     .values(paper_id=paper.id))
                    await db.execute(update(PaperSection).where(PaperSection.paper_id == temporary.id)
                                     .values(paper_id=paper.id))
                    await db.execute(update(Question).where(Question.paper_id == temporary.id)
                                     .values(paper_id=paper.id, status="stale"))
                    await db.execute(update(Question).where(Question.paper_id == paper.id)
                                     .values(status="stale"))
                    paper.forum_id = forum_id
                    paper.source_type = "openreview"
                    paper.source_uri = forum_source_uri(forum_id)
                    paper.title = temporary.title
                    paper.raw_metadata = {**(paper.raw_metadata or {}), **(temporary.raw_metadata or {}),
                                          "reconciled_from_upload": True}
                    await db.execute(delete(Paper).where(Paper.id == temporary.id))
                    await db.commit()
            print(f"Refreshed forum {forum_id} with its revisions and reviews.")
            return
        if args.command == "delete":
            paper_id = UUID(args.paper_id)
            paper = await db.get(Paper, paper_id)
            if not paper:
                raise ValueError("Paper was not found.")
            if confirm(f"Delete {paper.title!r}, all revisions, reviews, chunks, and questions?",
                       f"DELETE {paper.id}"):
                await db.execute(delete(Paper).where(Paper.id == paper.id))
                await db.commit()
                print("Deleted.")
            else:
                print("Cancelled.")
            return
        if args.command == "reset":
            counts = {m.__tablename__: await db.scalar(select(func.count()).select_from(m))
                      for m in (Paper, PaperVersion, Review, PaperChunk, Question)}
            print(counts)
            if confirm("Remove all PaperProbe records?", "RESET PAPERPROBE DATABASE"):
                await db.execute(delete(Paper))
                await db.commit()
                print("All papers and database-cascaded records were removed.")


def build_parser():
    parser = argparse.ArgumentParser(description="Inspect and maintain the PaperProbe database.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    listing = commands.add_parser("list")
    listing.add_argument("--query")
    listing.add_argument("--limit", type=int, default=100)
    for name in ("show", "delete", "reconcile"):
        command = commands.add_parser(name)
        command.add_argument("paper_id")
        if name == "reconcile":
            command.add_argument("--forum-id")
    commands.add_parser("reset")
    return parser


def main():
    args = build_parser().parse_args()
    try:
        asyncio.run(run(args))
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
