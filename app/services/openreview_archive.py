"""MongoDB persistence for unmodified OpenReview API note payloads."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from app.core.config import get_settings


class RawArchiveError(RuntimeError):
    """The raw OpenReview response could not be preserved in MongoDB."""


def archive_openreview_documents(
    forum_id: str,
    fetched_at: datetime,
    documents: list[dict[str, Any]],
    *,
    retain_version_ids: set[str] | None = None,
) -> int:
    """Store exact version/edit/review JSON with version-scoped composite keys."""
    settings = get_settings()
    if not settings.mongodb_uri:
        raise RawArchiveError("MONGODB_URI is required to archive OpenReview responses.")
    if not documents:
        raise RawArchiveError(f"OpenReview returned no raw documents for forum {forum_id}.")

    try:
        from pymongo import ASCENDING, DESCENDING, MongoClient

        with MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5000) as client:
            client.admin.command("ping")
            collection = client[settings.mongodb_database][settings.mongodb_raw_collection]
            collection.create_index([
                ("forum_id", ASCENDING), ("version_id", ASCENDING), ("fetched_at", DESCENDING)
            ])
            collection.create_index([
                ("forum_id", ASCENDING), ("version_id", ASCENDING), ("note_id", ASCENDING)
            ])

            for item in documents:
                record_type = str(item["record_type"])
                version_id = str(item["version_id"])
                version_timestamp = item.get("version_timestamp")
                raw_note = item["raw_note"]
                note_id = str(raw_note.get("id") or "unknown")
                payload_bytes = json.dumps(
                    raw_note, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")
                payload_hash = hashlib.sha256(payload_bytes).hexdigest()
                document_id = hashlib.sha256(
                    f"{forum_id}\0{version_id}\0{record_type}\0{note_id}\0{payload_hash}".encode("utf-8")
                ).hexdigest()
                document = {
                    "_id": document_id,
                    "schema_version": 2,
                    "forum_id": forum_id,
                    "version_id": version_id,
                    "version_timestamp": version_timestamp,
                    "note_id": note_id,
                    "record_type": record_type,
                    "payload_sha256": payload_hash,
                    "fetched_at": fetched_at,
                    "raw_note": raw_note,
                }
                document.update(item.get("archive_metadata", {}))
                collection.update_one({"_id": document_id}, {"$setOnInsert": document}, upsert=True)
            if retain_version_ids is not None:
                collection.delete_many({
                    "forum_id": forum_id,
                    "version_id": {"$nin": sorted(retain_version_ids)},
                })
    except Exception as exc:
        if isinstance(exc, RawArchiveError):
            raise
        raise RawArchiveError(f"Could not archive OpenReview data in MongoDB: {exc}") from exc
    return len(documents)


def load_archived_openreview_notes(
    forum_id: str, version_id: str | None = None
) -> list[dict[str, Any]]:
    """Read raw forum documents, optionally narrowed to one revision."""
    settings = get_settings()
    if not settings.mongodb_uri:
        raise RawArchiveError("MONGODB_URI is required to read archived OpenReview responses.")
    try:
        from pymongo import ASCENDING, MongoClient

        with MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5000) as client:
            collection = client[settings.mongodb_database][settings.mongodb_raw_collection]
            query: dict[str, Any] = {"forum_id": forum_id}
            if version_id is not None:
                query["version_id"] = version_id
            return list(collection.find(query).sort([
                ("version_timestamp", ASCENDING), ("note_id", ASCENDING), ("fetched_at", ASCENDING)
            ]))
    except Exception as exc:
        raise RawArchiveError(f"Could not read archived OpenReview data from MongoDB: {exc}") from exc
