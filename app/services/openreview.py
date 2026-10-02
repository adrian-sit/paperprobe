import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from app.core.config import get_settings
from app.services.openreview_archive import archive_openreview_notes


class OpenReviewContentError(ValueError):
    """A reachable forum does not expose the fields required by this pipeline."""

    def __init__(self, available_fields: list[str], missing_fields: list[str]) -> None:
        self.available_fields = available_fields
        self.missing_fields = missing_fields
        super().__init__("The OpenReview forum did not expose both a title and abstract.")


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


def fetch_paper(forum_id: str, *, client=None) -> OpenReviewPaper:
    """Archive raw v2 notes, then normalize the submission for the current pipeline."""
    if client is None:
        client = _make_client()
    submission, replies = _raw_api_notes(client, forum_id)
    fetched_at = datetime.now(timezone.utc)
    raw_notes = [("submission", submission)]
    raw_notes.extend(("reply", note) for note in replies)
    raw_note_count = archive_openreview_notes(
        forum_id,
        fetched_at,
        raw_notes,
    )

    title = _content_text(submission, "title")
    abstract = _content_text(submission, "abstract")
    if not title or not abstract:
        content = submission.get("content", {})
        missing_fields = [
            field for field, value in {"title": title, "abstract": abstract}.items() if not value
        ]
        raise OpenReviewContentError(sorted(content.keys()), missing_fields)
    return OpenReviewPaper(
        forum_id=forum_id,
        title=title,
        abstract=abstract,
        review_count=sum(_is_review(reply) for reply in replies),
        raw_note_count=raw_note_count,
        raw_fetched_at=fetched_at,
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
