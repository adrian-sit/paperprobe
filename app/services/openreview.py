import re
import unicodedata
from copy import deepcopy
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
    full_text: str
    is_latest: bool
    reviews: tuple[OpenReviewReview, ...] = ()


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


def _note_edits(client: Any, note_id: str) -> list[dict[str, Any]]:
    """Fetch the full edit history for a submission note."""
    edits_url = f"{client.notes_url.rsplit('/', 1)[0]}/notes/edits"
    edits: list[dict[str, Any]] = []
    offset = 0
    page_size = 100
    while True:
        response = client.session.get(
            edits_url,
            params={"note.id": note_id, "limit": page_size, "offset": offset, "sort": "tcdate:asc"},
            headers=client.headers,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("edits", []), list):
            raise RuntimeError("OpenReview returned an invalid note edit response.")
        page = [edit for edit in payload.get("edits", []) if isinstance(edit, dict)]
        if not page:
            break
        edits.extend(page)
        offset += len(page)
        total = payload.get("count")
        if isinstance(total, int) and offset >= total:
            break
        if len(page) < page_size and not isinstance(total, int):
            break
    return sorted(edits, key=lambda edit: (_timestamp(edit) or 0, str(edit.get("id", ""))))


def _apply_content_patch(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(current)
    for key, value in patch.items():
        if isinstance(value, dict) and value.get("delete") is True:
            merged.pop(key, None)
        else:
            merged[key] = deepcopy(value)
    return merged


def _version_snapshots(
    submission: dict[str, Any], edits: list[dict[str, Any]]
) -> list[tuple[str, int | None, dict[str, Any], dict[str, Any] | None]]:
    """Reconstruct note content after each edit, retaining the exact raw Edit."""
    base = deepcopy(submission)
    # Rebuild from the creation Edit forward. The fetched Note is the current
    # state and must not be used as the baseline for older revision snapshots.
    content: dict[str, Any] = {}
    snapshots = []
    for index, edit in enumerate(edits):
        changed_note = edit.get("note") if isinstance(edit.get("note"), dict) else {}
        content_patch = changed_note.get("content")
        if not isinstance(content_patch, dict):
            content_patch = edit.get("content", {})
        if isinstance(content_patch, dict):
            content = (deepcopy(content_patch) if edit.get("replacement") is True
                       else _apply_content_patch(content, content_patch))
        state = deepcopy(base)
        state.update({key: deepcopy(value) for key, value in changed_note.items() if key != "content"})
        state["content"] = deepcopy(content)
        stamp = _timestamp(edit) or _timestamp(changed_note)
        key = str(edit.get("id") or f"{stamp or 0}-{index}")
        snapshots.append((key, stamp, state, edit))

    if not snapshots:
        stamp = _timestamp(submission)
        key = str(stamp or submission.get("id") or "current")
        snapshots.append((key, stamp, base, None))
    else:
        # The note entity is the authoritative currently effective state. Keep
        # the edit's stable ID/timestamp but make its content exact.
        key, stamp, _, edit = snapshots[-1]
        latest = deepcopy(submission)
        snapshots[-1] = (key, stamp or _timestamp(submission), latest, edit)
    return snapshots


def _content_value(note: dict[str, Any], field: str) -> Any:
    value = note.get("content", {}).get(field)
    return value.get("value") if isinstance(value, dict) and "value" in value else value


def _pdf_identifier(note: dict[str, Any]) -> str | None:
    value = _content_value(note, "pdf")
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    parsed = urlparse(value)
    query_id = parse_qs(parsed.query).get("id")
    if query_id:
        value = query_id[0]
    elif "/pdf/" in parsed.path:
        value = parsed.path.split("/pdf/", 1)[1].strip("/")
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


def _openreview_pdf_text(client: Any, note_id: str, pdf_id: str | None = None) -> str:
    """Download and extract all pages from one OpenReview revision's PDF."""
    api_root = client.notes_url.rsplit("/", 1)[0]
    pdf_url = f"{api_root}/pdf/{quote(pdf_id, safe='')}" if pdf_id else f"{api_root}/pdf"
    try:
        params = None if pdf_id else {"id": note_id}
        response = client.session.get(pdf_url, params=params, headers=client.headers, timeout=60)
        response.raise_for_status()
        pdf_bytes = response.content
        if not pdf_bytes.startswith(b"%PDF"):
            raise OpenReviewPDFError(
                f"OpenReview did not return a PDF for submission {note_id}."
            )
        return extract_pdf_text(pdf_bytes)
    except OpenReviewPDFError:
        raise
    except Exception as exc:
        raise OpenReviewPDFError(
            f"Could not download or extract the OpenReview PDF for submission {note_id}: {exc}"
        ) from exc


def fetch_paper(forum_id: str, *, client=None) -> OpenReviewPaper:
    """Archive every submission edit and version-assigned review before normalization."""
    if client is None:
        client = _make_client()
    submission, replies = _raw_api_notes(client, forum_id)
    edits = _note_edits(client, str(submission.get("id") or forum_id))
    snapshots = _version_snapshots(submission, edits)
    latest_version_id = max(
        snapshots, key=lambda item: (item[1] or 0, item[0])
    )[0]

    timestamped_versions = sorted(
        ((timestamp, version_id) for version_id, timestamp, _, _ in snapshots),
        key=lambda item: (item[0] or 0, item[1]),
    )
    review_documents: list[dict[str, Any]] = []
    for reply in replies:
        if not _is_review(reply):
            continue
        review_timestamp = _timestamp(reply)
        previous = [item for item in timestamped_versions if item[0] is not None
                    and review_timestamp is not None and item[0] <= review_timestamp]
        target_version_id = previous[-1][1] if previous else timestamped_versions[0][1]
        review_documents.append({
            "record_type": "review",
            "version_id": target_version_id,
            "version_timestamp": next(
                (stamp for key, stamp, _, _ in snapshots if key == target_version_id), None
            ),
            "raw_note": reply,
            "archive_metadata": {"review_timestamp": review_timestamp},
        })

    fetched_at = datetime.now(timezone.utc)
    version_documents = []
    for version_id, timestamp, state, edit in snapshots:
        version_documents.append({
            "record_type": "submission_edit" if edit else "submission_note",
            "version_id": version_id,
            "version_timestamp": timestamp,
            "raw_note": edit if edit else submission,
        })
    version_documents.extend(review_documents)
    raw_document_count = archive_openreview_documents(forum_id, fetched_at, version_documents)

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
    versions: list[OpenReviewVersion] = []
    for version_id, timestamp, state, _edit in snapshots:
        pdf_id = _pdf_identifier(state)
        pdf_cache_key = pdf_id or str(state.get("id") or forum_id)
        if pdf_id is None and version_id != latest_version_id:
            # The /pdf?id=<note> form resolves the current attachment. Do not
            # mislabel that current PDF as the text of an older edit that had
            # no attachment reference of its own.
            version_text = ""
        elif pdf_cache_key not in text_by_pdf:
            text_by_pdf[pdf_cache_key] = _openreview_pdf_text(
                client, str(state.get("id") or forum_id), pdf_id
            )
            version_text = text_by_pdf[pdf_cache_key]
        else:
            version_text = text_by_pdf[pdf_cache_key]
        versions.append(OpenReviewVersion(
            version_id=version_id,
            version_timestamp=timestamp,
            title=_content_text(state, "title"),
            abstract=_content_text(state, "abstract"),
            full_text=version_text,
            is_latest=version_id == latest_version_id,
            reviews=tuple(archived_reviews.get(version_id, [])),
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
) -> OpenReviewPaper | None:
    """Find the newest OpenReview version with a strong title and author match.

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
    exact_title_ids = [forum_id for forum_id in strong_title_ids
                       if normalize(str(candidates_by_id[forum_id]["title"])) == wanted]
    for forum_id in strong_title_ids:
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

    if exact_title_ids:
        author_confirmed_ids = [forum_id for forum_id in exact_title_ids
                                if (author_scores.get(forum_id) or 0.0) >= author_threshold]
        # Exact title matches always pass the title gate. Use authors to prefer
        # matching exact-title versions when that evidence exists, but do not
        # reject an exact title solely because PDF author extraction was partial.
        version_pool = author_confirmed_ids or exact_title_ids
    else:
        version_pool = [forum_id for forum_id in strong_title_ids
                        if (author_scores.get(forum_id) or 0.0) >= author_threshold]

    eligible = [(author_scores.get(forum_id), version_dates.get(forum_id, 0), forum_id)
                for forum_id in version_pool]

    chosen_forum_id = None
    chosen_title_score = None
    chosen_author_score = None
    chosen_date = None
    if eligible:
        # Exact titles pass without an author gate. Fuzzy titles must also clear
        # the author threshold. Among eligible versions, prefer the newest date.
        chosen_author_score, chosen_date, chosen_forum_id = max(
            eligible, key=lambda item: (item[1], item[0] or 0.0,
                                       score(str(candidates_by_id[item[2]]["title"])))
        )
        chosen_title_score = score(str(candidates_by_id[chosen_forum_id]["title"]))

    if diagnostics is not None:
        best_title_score, best_title_id = ranked[0]
        diagnostics.update(queries=len(queries), candidates=len(candidates_by_id),
                           query_failures=query_failures,
                           best_title=candidates_by_id[best_title_id]["title"],
                           best_score=best_title_score,
                           best_author_score=author_scores.get(best_title_id),
                           selected_author_score=chosen_author_score,
                           eligible_versions=len(eligible),
                           selected_version_date=chosen_date,
                           selected_forum_id=chosen_forum_id)
    if chosen_forum_id is None:
        return None
    return replace(fetch_paper(chosen_forum_id, client=client),
                   match_score=chosen_title_score,
                   author_match_score=chosen_author_score, version_date=chosen_date)
