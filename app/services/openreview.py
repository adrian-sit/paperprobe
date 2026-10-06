import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from app.core.config import get_settings
from app.services.openreview_archive import (
    archive_openreview_documents,
    load_archived_openreview_notes,
)
from app.services.pdf import extract_pdf_text


class OpenReviewContentError(ValueError):
    """A reachable forum does not expose the fields required by this pipeline."""

    def __init__(self, available_fields: list[str], missing_fields: list[str]) -> None:
        self.available_fields = available_fields
        self.missing_fields = missing_fields
        super().__init__("The OpenReview forum did not expose both a title and abstract.")


class OpenReviewPDFError(RuntimeError):
    """The selected OpenReview paper's PDF could not be downloaded and parsed."""


@dataclass(frozen=True)
class OpenReviewReview:
    note_id: str
    version_id: str
    written_at: datetime | None
    invitation: str | None
    text: str
    raw_content: dict[str, Any]
    raw_metadata: dict[str, Any]


@dataclass(frozen=True)
class OpenReviewVersion:
    version_id: str
    version_timestamp: int | None
    title: str
    abstract: str
    authors: tuple[str, ...]
    full_text: str
    is_latest: bool
    forum_id: str | None = None
    reviews: tuple[OpenReviewReview, ...] = ()
    pdf_error: str | None = None
    full_text_source: str = "openreview_pdf"


@dataclass(frozen=True)
class OpenReviewPaper:
    forum_id: str
    title: str
    abstract: str
    review_count: int
    match_score: float | None = None
    author_match_score: float | None = None
    version_date: int | None = None
    raw_note_count: int = 0
    raw_fetched_at: datetime | None = None
    full_text: str = ""
    versions: tuple[OpenReviewVersion, ...] = ()

    @property
    def source_uri(self) -> str:
        return forum_source_uri(self.forum_id)


def forum_source_uri(forum_id: str) -> str:
    """Create the stable storage key before making an OpenReview request."""
    return f"https://openreview.net/forum?id={forum_id}"


def _content_text(note: object, field: str) -> str:
    return " ".join(_content_values(note, field))


def _content_values(note: object, field: str) -> list[str]:
    content = note.get("content", {}) if isinstance(note, dict) else getattr(note, "content", {})
    value = content.get(field, "")
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict) and isinstance(value.get("value"), str):
        return [value["value"].strip()] if value["value"].strip() else []
    # OpenReview author fields commonly use {"value": [...]} or a list of
    # wrapped values. Normalize both forms into text values for comparison.
    if isinstance(value, dict) and isinstance(value.get("value"), list):
        value = value["value"]
    if isinstance(value, list):
        values = []
        for item in value:
            if isinstance(item, str) and item.strip():
                values.append(item.strip())
            elif isinstance(item, dict) and isinstance(item.get("value"), str) and item["value"].strip():
                values.append(item["value"].strip())
        return values
    return []


def _is_review(note: object) -> bool:
    invitations = (
        note.get("invitations", []) if isinstance(note, dict)
        else getattr(note, "invitations", [])
    )
    return any(
        isinstance(invitation, str) and "review" in invitation.lower()
        for invitation in invitations
    )


def _timestamp(note: dict[str, Any]) -> int | None:
    for key in ("tcdate", "cdate", "tmdate", "mdate"):
        try:
            if note.get(key) is not None:
                return int(note[key])
        except (TypeError, ValueError):
            continue
    return None


def _as_datetime(timestamp: int | None) -> datetime | None:
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _latest_note_edit(client: Any, note_id: str) -> dict[str, Any] | None:
    """Fetch only the newest edit record for a submission note."""
    edits_url = f"{client.notes_url.rsplit('/', 1)[0]}/notes/edits"
    response = client.session.get(
        edits_url,
        params={"note.id": note_id, "limit": 1, "offset": 0, "sort": "tcdate:desc"},
        headers=client.headers,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("edits", []), list):
        raise RuntimeError("OpenReview returned an invalid note edit response.")
    edits = [edit for edit in payload.get("edits", []) if isinstance(edit, dict)]
    return max(edits, key=lambda edit: (_timestamp(edit) or 0, str(edit.get("id", "")))) if edits else None


def _content_value(note: dict[str, Any], field: str) -> Any:
    value = note.get("content", {}).get(field)
    return value.get("value") if isinstance(value, dict) and "value" in value else value


def _pdf_identifier(note: dict[str, Any]) -> str | None:
    value = _content_value(note, "pdf")
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    parsed = urlparse(value)
    path = parsed.path.rstrip("/")
    query_id = parse_qs(parsed.query).get("id")
    if path.endswith("/pdf") or path.endswith("/attachment"):
        # These routes take a Note id, not the PDF field's opaque file id.
        # Leave resolution to the note-ID attachment fallbacks below.
        return None
    if "/pdf/" in path:
        value = path.split("/pdf/", 1)[1]
    elif query_id:
        # Do not turn /attachment?id=<note-id> into /pdf/<note-id>.
        return None
    return value or None


def _review_text(content: dict[str, Any]) -> str:
    lines = []
    for field, raw_value in content.items():
        value = raw_value.get("value") if isinstance(raw_value, dict) else raw_value
        if isinstance(value, str) and value.strip():
            lines.append(f"{field}: {value.strip()}")
        elif isinstance(value, list):
            text_values = [item.strip() for item in value if isinstance(item, str) and item.strip()]
            if text_values:
                lines.append(f"{field}: {'; '.join(text_values)}")
    return "\n\n".join(lines)


def _make_client():
    try:
        import openreview
    except ImportError as exc:
        raise RuntimeError(
            'OpenReview client is missing. Install with: pip install -e ".[openreview]"'
        ) from exc

    settings = get_settings()
    options = {"baseurl": "https://api2.openreview.net"}
    if settings.openreview_username and settings.openreview_password:
        options.update(username=settings.openreview_username, password=settings.openreview_password)
    return openreview.api.OpenReviewClient(**options)


def _raw_api_notes(client: Any, forum_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fetch the original JSON objects through the v2 client session."""

    def get_page(params: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
        response = client.session.get(
            client.notes_url, params=params, headers=client.headers, timeout=30
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("OpenReview returned an invalid notes response.")
        notes = payload.get("notes", [])
        if not isinstance(notes, list):
            raise RuntimeError("OpenReview returned an invalid notes response.")
        count = payload.get("count")
        return (
            [note for note in notes if isinstance(note, dict)],
            count if isinstance(count, int) else None,
        )

    submissions, _ = get_page({"id": forum_id, "limit": 1})
    if not submissions:
        raise LookupError(f"OpenReview returned no submission for forum {forum_id}.")
    submission = submissions[0]

    replies: list[dict[str, Any]] = []
    offset = 0
    page_size = 100
    while True:
        page, total = get_page({"forum": forum_id, "limit": page_size, "offset": offset})
        if not page:
            break
        replies.extend(page)
        offset += len(page)
        if total is not None and offset >= total:
            break
        if len(page) < page_size and total is None:
            break
    submission_id = submission.get("id")
    replies = [note for note in replies if note.get("id") != submission_id]
    return submission, replies


def _openreview_pdf_text(
    client: Any, note_id: str, pdf_id: str | None = None, *, allow_note_fallback: bool = True
) -> str:
    """Try OpenReview's PDF and attachment routes, returning extracted text."""
    api_root = client.notes_url.rsplit("/", 1)[0]
    attempts = []
    if pdf_id:
        pdf_url = f"{api_root}/pdf/{quote(pdf_id, safe='')}"
        attempts.append((f"PDF field id ({pdf_url})", lambda: client.session.get(
            pdf_url, headers=client.headers, timeout=60
        )))
        get_pdf = getattr(client, "get_pdf", None)
        if callable(get_pdf):
            # Some venues expose the PDF field as an OpenReview revision
            # reference. openreview-py's reference mode resolves that through
            # its revision-aware endpoint rather than /pdf/{field-id}.
            attempts.append(("PDF field reference (get_pdf, is_reference=True)",
                             lambda: get_pdf(id=pdf_id, is_reference=True)))

    if allow_note_fallback:
        get_attachment = getattr(client, "get_attachment", None)
        if callable(get_attachment):
            attempts.append(("note attachment (get_attachment)", lambda: get_attachment(
                field_name="pdf", id=note_id
            )))
        else:
            attachment_url = f"{api_root}/attachment"
            attempts.append((f"note attachment ({attachment_url})", lambda: client.session.get(
                attachment_url, params={"id": note_id, "name": "pdf"},
                headers=client.headers, timeout=60,
            )))

        get_pdf = getattr(client, "get_pdf", None)
        if callable(get_pdf):
            attempts.append(("note PDF (get_pdf)", lambda: get_pdf(id=note_id)))
        else:
            pdf_url = f"{api_root}/pdf"
            attempts.append((f"note PDF ({pdf_url}?id=...)", lambda: client.session.get(
                pdf_url, params={"id": note_id}, headers=client.headers, timeout=60
            )))

    failures = []
    for label, download in attempts:
        try:
            result = download()
            if hasattr(result, "raise_for_status"):
                result.raise_for_status()
                pdf_bytes = result.content
            else:
                pdf_bytes = result
            if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF"):
                raise ValueError("response did not contain PDF bytes")
            text = extract_pdf_text(pdf_bytes)
            if not text.strip():
                raise ValueError("PDF contained no extractable text")
            return text
        except Exception as exc:
            failures.append(f"{label}: {exc}")

    raise OpenReviewPDFError(
        f"Could not download or extract PDF for OpenReview note {note_id}; "
        f"tried {len(attempts)} method(s): {'; '.join(failures)}"
    )


def fetch_paper(forum_id: str, *, client=None) -> OpenReviewPaper:
    """Fetch the newest submission version and its reviews for one forum."""
    if client is None:
        client = _make_client()
    submission, replies = _raw_api_notes(client, forum_id)
    try:
        latest_edit = _latest_note_edit(client, str(submission.get("id") or forum_id))
    except Exception:
        # The fetched Note is itself the current effective state. Use it if the
        # optional edit lookup is unavailable rather than blocking ingestion.
        latest_edit = None
    latest_timestamp = _timestamp(latest_edit or {}) or _timestamp(submission)
    latest_version_id = str((latest_edit or {}).get("id") or latest_timestamp
                            or submission.get("id") or "current")
    latest_snapshot = (latest_version_id, latest_timestamp, submission, latest_edit)
    snapshots = [latest_snapshot]
    review_documents: list[dict[str, Any]] = []
    for reply in replies:
        if not _is_review(reply):
            continue
        review_timestamp = _timestamp(reply)
        review_documents.append({
            "record_type": "review",
            "version_id": latest_version_id,
            "version_timestamp": latest_snapshot[1],
            "raw_note": reply,
            "archive_metadata": {"review_timestamp": review_timestamp},
        })

    fetched_at = datetime.now(timezone.utc)
    version_documents = [{
        "record_type": "submission_note",
        "version_id": latest_version_id,
        "version_timestamp": latest_timestamp,
        "raw_note": submission,
    }]
    if latest_edit:
        version_documents.append({
            "record_type": "submission_edit",
            "version_id": latest_version_id,
            "version_timestamp": latest_timestamp,
            "raw_note": latest_edit,
        })
    version_documents.extend(review_documents)
    raw_document_count = archive_openreview_documents(
        forum_id, fetched_at, version_documents, retain_version_ids={latest_version_id}
    )

    archived = load_archived_openreview_notes(forum_id)
    archived_reviews: dict[str, list[OpenReviewReview]] = {item[0]: [] for item in snapshots}
    newest_review_documents: dict[tuple[str, str], dict[str, Any]] = {}
    for document in archived:
        if document.get("schema_version") != 2 or document.get("record_type") != "review":
            continue
        version_id = str(document.get("version_id", ""))
        if version_id not in archived_reviews:
            continue
        note = document.get("raw_note", {})
        note_id = str(note.get("id") or document.get("note_id") or "unknown")
        key = (version_id, note_id)
        previous = newest_review_documents.get(key)
        if previous is None or document.get("fetched_at") >= previous.get("fetched_at"):
            newest_review_documents[key] = document

    for (version_id, _note_id), document in newest_review_documents.items():
        note = document.get("raw_note", {})
        content = note.get("content", {}) if isinstance(note, dict) else {}
        if not isinstance(content, dict):
            content = {}
        invitation_values = note.get("invitations", []) if isinstance(note, dict) else []
        invitation = next((item for item in invitation_values
                           if isinstance(item, str) and "review" in item.lower()), None)
        archived_reviews[version_id].append(OpenReviewReview(
            note_id=str(note.get("id") or document.get("note_id") or "unknown"),
            version_id=version_id,
            written_at=_as_datetime(document.get("review_timestamp")),
            invitation=invitation,
            text=_review_text(content),
            raw_content=content,
            raw_metadata={key: value for key, value in note.items()
                          if key not in {"content", "id"}},
        ))

    text_by_pdf: dict[str, str] = {}
    pdf_errors: dict[str, str] = {}
    versions: list[OpenReviewVersion] = []
    for version_id, timestamp, state, _edit in snapshots:
        pdf_id = _pdf_identifier(state)
        pdf_cache_key = pdf_id or str(state.get("id") or forum_id)
        pdf_error = None
        if pdf_cache_key not in text_by_pdf:
            try:
                text_by_pdf[pdf_cache_key] = _openreview_pdf_text(
                    client, str(state.get("id") or forum_id), pdf_id, allow_note_fallback=True
                )
            except OpenReviewPDFError as exc:
                pdf_error = str(exc)
                pdf_errors[pdf_cache_key] = pdf_error
                text_by_pdf[pdf_cache_key] = ""
        elif not text_by_pdf[pdf_cache_key]:
            pdf_error = pdf_errors.get(pdf_cache_key, "The referenced PDF could not be extracted.")
        version_text = text_by_pdf[pdf_cache_key]
        versions.append(OpenReviewVersion(
            version_id=version_id,
            version_timestamp=timestamp,
            title=_content_text(state, "title"),
            abstract=_content_text(state, "abstract"),
            authors=tuple(_content_values(state, "authors")),
            full_text=version_text,
            is_latest=version_id == latest_version_id,
            forum_id=forum_id,
            reviews=tuple(archived_reviews.get(version_id, [])),
            pdf_error=pdf_error,
        ))

    latest = next(version for version in versions if version.is_latest)
    if not latest.title or not latest.abstract:
        content = next(
            (state.get("content", {}) for key, _, state, _ in snapshots if key == latest.version_id), {}
        )
        missing_fields = [field for field, value in
                          {"title": latest.title, "abstract": latest.abstract}.items() if not value]
        raise OpenReviewContentError(sorted(content.keys()), missing_fields)

    return OpenReviewPaper(
        forum_id=forum_id,
        title=latest.title,
        abstract=latest.abstract,
        review_count=len(latest.reviews),
        full_text=latest.full_text,
        raw_note_count=raw_document_count,
        raw_fetched_at=fetched_at,
        versions=tuple(versions),
    )


def find_matching_forum(
    title: str,
    diagnostics: dict | None = None,
    *,
    authors: tuple[str, ...] | list[str] = (),
    include_forum_id: str | None = None,
    seed_paper: OpenReviewPaper | None = None,
) -> OpenReviewPaper | None:
    """Fetch every strongly matching forum and combine its revisions and reviews.

    OpenReview's search endpoint is an index, so a long complete-title query can
    miss indexed notes. Several short, overlapping title phrases improve recall;
    title and author names are used to identify the paper. The uploaded abstract
    is deliberately not used as matching evidence.
    """
    client = _make_client()

    def normalize(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).casefold()
        # Join PDF line-wrap hyphenation before punctuation is normalized away.
        value = re.sub(r"(?<=\w)-\s+(?=\w)", "", value)
        value = unicodedata.normalize("NFKD", value)
        value = "".join(char for char in value if not unicodedata.combining(char))
        return " ".join(re.sub(r"[^\w]+", " ", value).split())

    wanted = normalize(title)
    if not wanted:
        return None

    # Searching a complete title can be too restrictive for the indexed endpoint.
    # Query short overlapping phrases across the title (plus the full title),
    # which still keeps searches specific while covering title variants.
    words = wanted.split()
    queries = [title]
    if len(words) >= 4:
        width = 4
        all_starts = list(range(0, len(words) - width + 1, 3))
        all_starts.extend([max(0, len(words) - width), max(0, len(words) // 2 - width // 2)])
        all_starts = sorted(set(all_starts))
        # Cap request count while sampling the whole title, including its end.
        if len(all_starts) > 8:
            starts = [all_starts[round(index * (len(all_starts) - 1) / 7)] for index in range(8)]
        else:
            starts = all_starts
        queries.extend(" ".join(words[start:start + width]) for start in starts)
    elif len(words) >= 2:
        queries.append(" ".join(words))
    queries = list(dict.fromkeys(query for query in queries if query.strip()))[:9]

    candidates_by_id: dict[str, dict[str, object]] = {}
    if include_forum_id and seed_paper:
        latest_seed = max(seed_paper.versions,
                          key=lambda item: (item.version_timestamp or 0, item.version_id),
                          default=None)
        candidates_by_id[include_forum_id] = {
            "title": seed_paper.title,
            "authors": latest_seed.authors if latest_seed else tuple(authors),
            "date": latest_seed.version_timestamp if latest_seed else seed_paper.version_date,
        }
    elif include_forum_id:
        try:
            note = client.get_note(include_forum_id)
            candidates_by_id[include_forum_id] = {
                "title": _content_text(note, "title"),
                "authors": tuple(_content_values(note, "authors")),
                "date": (getattr(note, "tcdate", None) or getattr(note, "cdate", None)
                         or getattr(note, "tmdate", None)),
            }
        except Exception as exc:
            raise LookupError(f"Could not load the requested OpenReview forum {include_forum_id}: {exc}") from exc
    query_failures = []
    for query in dict.fromkeys(queries):
        try:
            found = client.search_notes(term=query, content="title", source="forum", limit=100)
        except Exception as exc:
            query_failures.append(f"{query[:60]}: {exc}")
            continue
        for candidate in found:
            forum_id = getattr(candidate, "id", None) or getattr(candidate, "forum", None)
            candidate_title = _content_text(candidate, "title")
            if forum_id and candidate_title:
                candidates_by_id[forum_id] = {
                    "title": candidate_title,
                    "authors": tuple(_content_values(candidate, "authors")),
                    "date": getattr(candidate, "tcdate", None) or getattr(candidate, "cdate", None),
                }

    def score(candidate_title: str) -> float:
        candidate = normalize(candidate_title)
        if candidate == wanted:
            return 1.0
        wanted_tokens, candidate_tokens = set(words), set(candidate.split())
        if not wanted_tokens or not candidate_tokens:
            return 0.0
        token_precision = len(wanted_tokens & candidate_tokens) / len(candidate_tokens)
        token_recall = len(wanted_tokens & candidate_tokens) / len(wanted_tokens)
        token_f1 = (2 * token_precision * token_recall / (token_precision + token_recall)
                    if token_precision + token_recall else 0.0)
        sequence_score = SequenceMatcher(None, wanted, candidate).ratio()
        # Token overlap is more tolerant of punctuation, word order, and a
        # short subtitle added by the venue; sequence similarity helps reject
        # candidates that merely share generic title words.
        return 0.45 * sequence_score + 0.55 * token_f1

    ranked = sorted(((score(str(candidate["title"])), forum_id)
                     for forum_id, candidate in candidates_by_id.items()),
                    key=lambda item: item[0], reverse=True)
    if diagnostics is not None:
        diagnostics.update(queries=len(queries), candidates=len(candidates_by_id),
                          query_failures=query_failures)
    if not ranked:
        return None
    title_threshold = 0.90
    author_threshold = 0.30
    strong_title_ids = [forum_id for title_score, forum_id in ranked
                        if title_score >= title_threshold]
    def author_similarity(candidate_authors: tuple[str, ...]) -> float | None:
        expected = [normalize(name) for name in authors if normalize(name)]
        actual_names = []
        for name in candidate_authors:
            parts = re.split(r"\s*;\s*|\s+and\s+", name, flags=re.I)
            if len(parts) == 1 and name.count(",") >= 1:
                comma_parts = [part.strip() for part in name.split(",")]
                # Avoid splitting a single "Last, First" name into two pieces.
                if len(comma_parts) >= 2 and all(len(part.split()) >= 2 for part in comma_parts):
                    parts = comma_parts
            actual_names.extend(parts)
        actual = [normalize(name) for name in actual_names if normalize(name)]
        if not expected or not actual:
            return None

        pairs = []
        for expected_index, expected_name in enumerate(expected):
            expected_tokens = set(expected_name.split())
            for actual_index, actual_name in enumerate(actual):
                actual_tokens = set(actual_name.split())
                common = expected_tokens & actual_tokens
                precision = len(common) / len(actual_tokens)
                recall = len(common) / len(expected_tokens)
                token_f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
                pair_score = 0.15 * SequenceMatcher(None, expected_name, actual_name).ratio() + 0.85 * token_f1
                pairs.append((pair_score, expected_index, actual_index))

        matched_expected, matched_actual, matched_scores = set(), set(), []
        for pair_score, expected_index, actual_index in sorted(pairs, reverse=True):
            if pair_score < 0.5 or expected_index in matched_expected or actual_index in matched_actual:
                continue
            matched_expected.add(expected_index)
            matched_actual.add(actual_index)
            matched_scores.append(pair_score)
        precision = len(matched_actual) / len(actual)
        recall = len(matched_expected) / len(expected)
        coverage_f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        pair_average = sum(matched_scores) / len(matched_scores) if matched_scores else 0.0
        return 0.7 * coverage_f1 + 0.3 * pair_average

    author_scores: dict[str, float | None] = {}
    version_dates: dict[str, int] = {}
    initially_strong_ids = list(strong_title_ids)
    for forum_id in initially_strong_ids:
        candidate = candidates_by_id[forum_id]
        try:
            # Fetch authoritative author metadata and creation date before
            # choosing among high-title candidates; search results may omit them.
            note = client.get_note(forum_id)
            candidate["title"] = _content_text(note, "title") or candidate["title"]
            candidate["authors"] = tuple(_content_values(note, "authors")) or candidate["authors"]
            candidate["date"] = (getattr(note, "tcdate", None)
                                 or getattr(note, "cdate", None)
                                 or getattr(note, "tmdate", None)
                                 or candidate["date"])
        except Exception as exc:
            query_failures.append(f"candidate {forum_id}: {exc}")
        author_scores[forum_id] = author_similarity(tuple(candidate["authors"]))
        try:
            version_dates[forum_id] = int(candidate["date"] or 0)
        except (TypeError, ValueError):
            version_dates[forum_id] = 0

    strong_title_ids = [forum_id for forum_id in initially_strong_ids
                        if score(str(candidates_by_id[forum_id]["title"])) >= title_threshold]
    exact_title_ids = [forum_id for forum_id in strong_title_ids
                       if normalize(str(candidates_by_id[forum_id]["title"])) == wanted]
    # Collect every forum that meets the existing safe-match rules. Exact
    # normalized titles pass directly; fuzzy titles require both the title and
    # author thresholds. These forums can represent separate revisions/venues.
    eligible_ids = [forum_id for forum_id in strong_title_ids
                    if (forum_id in exact_title_ids
                        or (author_scores.get(forum_id) or 0.0) >= author_threshold)]
    if include_forum_id and include_forum_id not in eligible_ids:
        eligible_ids.append(include_forum_id)
    eligible = sorted(eligible_ids,
                      key=lambda forum_id: (version_dates.get(forum_id, 0),
                                            author_scores.get(forum_id) or 0.0,
                                            score(str(candidates_by_id[forum_id]["title"]))),
                      reverse=True)
    primary_forum_id = eligible[0] if eligible else None
    primary_title_score = score(str(candidates_by_id[primary_forum_id]["title"])) if primary_forum_id else None
    primary_author_score = author_scores.get(primary_forum_id) if primary_forum_id else None
    primary_date = version_dates.get(primary_forum_id) if primary_forum_id else None

    if diagnostics is not None:
        best_title_score, best_title_id = ranked[0]
        diagnostics.update(queries=len(queries), candidates=len(candidates_by_id),
                           query_failures=query_failures,
                           best_title=candidates_by_id[best_title_id]["title"],
                           best_score=best_title_score,
                           best_author_score=author_scores.get(best_title_id),
                           selected_author_score=primary_author_score,
                           eligible_versions=len(eligible),
                           eligible_forum_ids=eligible,
                           selected_version_date=primary_date,
                           selected_forum_id=primary_forum_id)
    if primary_forum_id is None:
        return None
    fetched = {}
    if seed_paper and include_forum_id:
        fetched[include_forum_id] = seed_paper
    for matched_forum_id in eligible:
        if matched_forum_id not in fetched:
            fetched[matched_forum_id] = fetch_paper(matched_forum_id, client=client)

    all_versions = []
    for matched_forum_id, matched_paper in fetched.items():
        for version in matched_paper.versions:
            all_versions.append(replace(version, forum_id=version.forum_id or matched_forum_id,
                                         is_latest=False))
    latest_version = max(all_versions,
                         key=lambda item: (item.version_timestamp or 0,
                                           item.forum_id or "", item.version_id))
    all_versions = [replace(version, is_latest=(version is latest_version)) for version in all_versions]
    primary_forum_id = latest_version.forum_id or primary_forum_id
    primary_title_score = score(latest_version.title)
    primary_author_score = author_scores.get(primary_forum_id)
    if diagnostics is not None:
        diagnostics.update(selected_forum_id=primary_forum_id,
                           selected_version_date=latest_version.version_timestamp,
                           selected_title_score=primary_title_score,
                           selected_author_score=primary_author_score)
    return OpenReviewPaper(
        forum_id=primary_forum_id,
        title=latest_version.title,
        abstract=latest_version.abstract,
        review_count=sum(len(version.reviews) for version in all_versions),
        match_score=primary_title_score,
        author_match_score=primary_author_score,
        version_date=latest_version.version_timestamp,
        raw_note_count=sum(paper.raw_note_count for paper in fetched.values()),
        raw_fetched_at=max((paper.raw_fetched_at for paper in fetched.values() if paper.raw_fetched_at),
                           default=None),
        full_text=latest_version.full_text,
        versions=tuple(sorted(all_versions,
                              key=lambda item: (item.version_timestamp or 0,
                                                item.forum_id or "", item.version_id))),
    )
