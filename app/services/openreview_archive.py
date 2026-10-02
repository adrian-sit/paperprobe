"""MongoDB persistence for unmodified OpenReview API note payloads."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from app.core.config import get_settings


class RawArchiveError(RuntimeError):
    """The raw OpenReview response could not be preserved in MongoDB."""


def archive_openreview_notes(
    forum_id: str,
    fetched_at: datetime,
    notes: list[tuple[str, dict[str, Any]]],
) -> int:
    """Store exact Note JSON objects idempotently, preserving changed versions."""
    settings = get_settings()
    if not settings.mongodb_uri:
        raise RawArchiveError("MONGODB_URI is required to archive OpenReview responses.")
    if not notes:
        raise RawArchiveError(f"OpenReview returned no raw notes for forum {forum_id}.")

    try:
        from pymongo import ASCENDING, DESCENDING, MongoClient

        with MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5000) as client:
            client.admin.command("ping")
            collection = client[settings.mongodb_database][settings.mongodb_raw_collection]
            collection.create_index([("forum_id", ASCENDING), ("fetched_at", DESCENDING)])
            collection.create_index([("forum_id", ASCENDING), ("note_id", ASCENDING)])

            for record_type, raw_note in notes:
                note_id = str(raw_note.get("id") or "unknown")
                payload_bytes = json.dumps(
                    raw_note, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")
                payload_hash = hashlib.sha256(payload_bytes).hexdigest()
                document_id = hashlib.sha256(
                    f"{forum_id}\0{record_type}\0{note_id}\0{payload_hash}".encode("utf-8")
                ).hexdigest()
                document = {
                    "_id": document_id,
                    "schema_version": 1,
                    "forum_id": forum_id,
                    "note_id": note_id,
                    "record_type": record_type,
                    "payload_sha256": payload_hash,
                    "fetched_at": fetched_at,
                    "raw_note": raw_note,
                }
                collection.update_one({"_id": document_id}, {"$setOnInsert": document}, upsert=True)
    except Exception as exc:
        if isinstance(exc, RawArchiveError):
            raise
        raise RawArchiveError(f"Could not archive OpenReview data in MongoDB: {exc}") from exc
    return len(notes)


def load_archived_openreview_notes(forum_id: str) -> list[dict[str, Any]]:
    """Read archived raw note documents for future extraction/reprocessing jobs."""
    settings = get_settings()
    if not settings.mongodb_uri:
        raise RawArchiveError("MONGODB_URI is required to read archived OpenReview responses.")
    try:
        from pymongo import ASCENDING, MongoClient

        with MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5000) as client:
            collection = client[settings.mongodb_database][settings.mongodb_raw_collection]
            return list(collection.find({"forum_id": forum_id}).sort(
                [("note_id", ASCENDING), ("fetched_at", ASCENDING)]
            ))
    except Exception as exc:
        raise RawArchiveError(f"Could not read archived OpenReview data from MongoDB: {exc}") from exc
