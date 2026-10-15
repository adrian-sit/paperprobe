import argparse
import asyncio
import json
from uuid import UUID

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base
from app.db.models import ExamplePair, Paper, PaperChunk, PaperSection, PaperVersion, Question, Review, ExtractedField
from app.db.session import get_session_factory


def confirm(prompt: str, phrase: str) -> bool:
    print(prompt)
    return input(f"Type {phrase!r} to continue: ").strip() == phrase


def _table_by_name(name: str):
    table = Base.metadata.tables.get(name)
    if table is None:
        available = ", ".join(sorted(Base.metadata.tables))
        raise ValueError(f"Unknown PaperProbe table {name!r}. Available tables: {available}")
    return table


def _display_row(row) -> None:
    values = dict(row)
    for key, value in values.items():
        if "embedding" in key and value is not None:
            values[key] = f"<vector dimensions={len(value)}>"
    print(json.dumps(values, ensure_ascii=False, default=str))


async def _example_group_rows(db: AsyncSession, source_paper_id: UUID):
    rows = list(await db.scalars(select(ExamplePair).where(
        ExamplePair.source_paper_id == source_paper_id
    ).order_by(ExamplePair.created_at, ExamplePair.id)))
    if not rows:
        # Rows created before source_paper_id was added can still be managed individually.
        rows = list(await db.scalars(select(ExamplePair).where(
            ExamplePair.id == source_paper_id,
            ExamplePair.source_paper_id.is_(None),
        )))
    return rows


async def run(args):
    async with get_session_factory()() as db:
        if args.command == "status":
            for table in Base.metadata.sorted_tables:
                count = await db.scalar(select(func.count()).select_from(table))
                print(f"{table.name}: {count}")
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
        if args.command == "examples":
            if args.example_command == "list":
                source_id = func.coalesce(ExamplePair.source_paper_id, ExamplePair.id).label("source_paper_id")
                created = func.max(ExamplePair.created_at).label("created_at")
                stmt = select(
                    source_id,
                    func.max(ExamplePair.source_title).label("source_title"),
                    func.max(ExamplePair.source_type).label("source_type"),
                    func.count(ExamplePair.id).label("questions"),
                    created,
                ).group_by(source_id).order_by(created.desc()).limit(max(1, min(args.limit, 500)))
                if args.query:
                    stmt = stmt.where(
                        ExamplePair.source_title.ilike(f"%{args.query}%")
                        | ExamplePair.question_text.ilike(f"%{args.query}%")
                    )
                if args.source_type:
                    stmt = stmt.where(ExamplePair.source_type == args.source_type)
                for row in (await db.execute(stmt)).all():
                    print(f"{row.source_paper_id} | {row.source_title} | source={row.source_type} "
                          f"| questions={row.questions} | updated={row.created_at}")
                return
            source_paper_id = UUID(args.source_paper_id)
            rows = await _example_group_rows(db, source_paper_id)
            if not rows:
                raise ValueError("Example paper was not found.")
            if args.example_command == "show":
                first = rows[0]
                print(f"Example paper: {first.source_paper_id or first.id}\nTitle: {first.source_title}"
                      f"\nSource: {first.source_type}\nEmbedding model: {first.embedding_model}"
                      f"\nPaper context:\n{first.source_abstract_or_claims}\nQuestions:")
                for index, row in enumerate(rows, start=1):
                    print(f"  {index}. {row.question_text}")
                    if row.rationale:
                        print(f"     Rationale: {row.rationale}")
                return
            if args.example_command == "delete":
                group_id = rows[0].source_paper_id or rows[0].id
                if confirm(f"Delete {len(rows)} example question(s) for {rows[0].source_title!r}?",
                           f"DELETE EXAMPLES {group_id}"):
                    for row in rows:
                        await db.delete(row)
                    await db.commit()
                    print(f"Deleted {len(rows)} example question(s).")
                else:
                    print("Cancelled.")
                return
        if args.command == "rows":
            table = _table_by_name(args.table)
            primary_keys = list(table.primary_key.columns)
            stmt = select(table).limit(max(1, min(args.limit, 500)))
            if args.record_id:
                if len(primary_keys) != 1:
                    raise ValueError(f"Table {args.table!r} does not have a single-column primary key.")
                column = primary_keys[0]
                try:
                    key = UUID(args.record_id) if column.type.python_type is UUID else column.type.python_type(args.record_id)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Invalid key for table {args.table!r}: {args.record_id}") from exc
                stmt = select(table).where(column == key).limit(1)
            rows = (await db.execute(stmt)).mappings()
            for row in rows:
                _display_row(row)
            return
        if args.command == "delete-row":
            table = _table_by_name(args.table)
            primary_keys = list(table.primary_key.columns)
            if len(primary_keys) != 1:
                raise ValueError(f"Table {args.table!r} does not have a single-column primary key.")
            column = primary_keys[0]
            try:
                key = UUID(args.record_id) if column.type.python_type is UUID else column.type.python_type(args.record_id)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid key for table {args.table!r}: {args.record_id}") from exc
            if not await db.scalar(select(func.count()).select_from(table).where(column == key)):
                raise ValueError(f"No {args.table} record found with key {args.record_id}.")
            if confirm(f"Delete record {args.record_id} from {args.table!r}? Related rows may cascade.",
                       f"DELETE {args.table} {args.record_id}"):
                result = await db.execute(delete(table).where(column == key))
                await db.commit()
                print(f"Deleted {result.rowcount} record(s).")
            else:
                print("Cancelled.")
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
            tables = Base.metadata.sorted_tables
            counts = {table.name: await db.scalar(select(func.count()).select_from(table))
                      for table in tables}
            print(counts)
            if confirm("Remove all PaperProbe records?", "RESET PAPERPROBE DATABASE"):
                preparer = db.get_bind().dialect.identifier_preparer
                quoted_tables = ", ".join(preparer.quote(table.name) for table in tables)
                await db.execute(text(f"TRUNCATE TABLE {quoted_tables} RESTART IDENTITY CASCADE"))
                await db.commit()
                print("All PaperProbe table records were removed; the schema was preserved.")


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
    examples = commands.add_parser("examples", help="Inspect or delete uploaded paper/question examples.")
    example_commands = examples.add_subparsers(dest="example_command", required=True)
    example_list = example_commands.add_parser("list")
    example_list.add_argument("--query")
    example_list.add_argument("--source-type", choices=("own_criticism", "openreview_review"))
    example_list.add_argument("--limit", type=int, default=100)
    for name in ("show", "delete"):
        command = example_commands.add_parser(name)
        command.add_argument("source_paper_id")
    rows = commands.add_parser("rows", help="Inspect rows from any mapped PaperProbe table.")
    rows.add_argument("table")
    rows.add_argument("--record-id")
    rows.add_argument("--limit", type=int, default=20)
    delete_row = commands.add_parser("delete-row", help="Delete one record from any mapped table by primary key.")
    delete_row.add_argument("table")
    delete_row.add_argument("record_id")
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
