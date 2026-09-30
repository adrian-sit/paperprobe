import re
import unicodedata
from dataclasses import dataclass, replace
from difflib import SequenceMatcher

from app.core.config import get_settings


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

    @property
    def source_uri(self) -> str:
        return forum_source_uri(self.forum_id)


def forum_source_uri(forum_id: str) -> str:
    """Create the stable storage key before making an OpenReview request."""
    return f"https://openreview.net/forum?id={forum_id}"


def _content_text(note: object, field: str) -> str:
    return " ".join(_content_values(note, field))


def _content_values(note: object, field: str) -> list[str]:
    content = getattr(note, "content", {})
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
    return any("review" in invitation.lower() for invitation in getattr(note, "invitations", []))


def fetch_paper(forum_id: str) -> OpenReviewPaper:
    """Fetch one public OpenReview v2 submission and count its public reviews."""
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
    client = openreview.api.OpenReviewClient(**options)
    submission = client.get_note(forum_id)
    title = _content_text(submission, "title")
    abstract = _content_text(submission, "abstract")
    if not title or not abstract:
        content = getattr(submission, "content", {})
        missing_fields = [field for field, value in {"title": title, "abstract": abstract}.items() if not value]
        raise OpenReviewContentError(sorted(content.keys()), missing_fields)
    reviews = client.get_all_notes(forum=forum_id)
    return OpenReviewPaper(
        forum_id=forum_id,
        title=title,
        abstract=abstract,
        review_count=sum(_is_review(reply) for reply in reviews),
    )


def find_matching_forum(
    title: str,
    diagnostics: dict | None = None,
    *,
    authors: tuple[str, ...] | list[str] = (),
    abstract: str = "",
) -> OpenReviewPaper | None:
    """Find an OpenReview submission by title, broadening indexed searches safely.

    OpenReview's search endpoint is an index, so a long complete-title query can
    miss indexed notes. Several short, overlapping title phrases improve recall;
    the complete title is still used to decide whether a candidate is accepted.
    """
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
    client = openreview.api.OpenReviewClient(**options)
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

    candidates_by_id: dict[str, dict[str, str | tuple[str, ...]]] = {}
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
                    "abstract": _content_text(candidate, "abstract"),
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
    best_score, best_forum_id = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0.0

    def evidence_similarity(left: str, right: str) -> float | None:
        left_normalized, right_normalized = normalize(left), normalize(right)
        if not left_normalized or not right_normalized:
            return None
        if left_normalized == right_normalized:
            return 1.0
        left_tokens, right_tokens = set(left_normalized.split()), set(right_normalized.split())
        common = left_tokens & right_tokens
        precision = len(common) / len(right_tokens)
        recall = len(common) / len(left_tokens)
        token_f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        return 0.4 * SequenceMatcher(None, left_normalized, right_normalized).ratio() + 0.6 * token_f1

    def secondary_score(candidate: dict[str, str | tuple[str, ...]]) -> float | None:
        evidence = []
        candidate_authors = candidate["authors"]
        if authors and candidate_authors:
            evidence.append((0.6, evidence_similarity(" ".join(authors), " ".join(candidate_authors))))
        candidate_abstract = str(candidate["abstract"])
        if abstract and candidate_abstract:
            evidence.append((0.4, evidence_similarity(abstract, candidate_abstract)))
        evidence = [(weight, value) for weight, value in evidence if value is not None]
        if not evidence:
            return None
        return sum(weight * value for weight, value in evidence) / sum(weight for weight, _ in evidence)

    exact_ids = [forum_id for title_score, forum_id in ranked
                 if normalize(str(candidates_by_id[forum_id]["title"])) == wanted]
    exact_title_match = bool(exact_ids)
    title_ties = [forum_id for title_score, forum_id in ranked
                  if best_score - title_score < 0.04]
    # When title scores tie or are nearly tied, compare the extracted author
    # list and abstract against candidate metadata to distinguish same-title
    # submissions. Search results may omit those fields, so fetch only the
    # close candidates that need enrichment.
    comparison_ids = exact_ids if exact_title_match else title_ties
    if len(comparison_ids) > 1 and (authors or abstract):
        for forum_id in comparison_ids:
            candidate = candidates_by_id[forum_id]
            if not candidate["authors"] or not candidate["abstract"]:
                try:
                    note = client.get_note(forum_id)
                    if not candidate["authors"]:
                        candidate["authors"] = tuple(_content_values(note, "authors"))
                    if not candidate["abstract"]:
                        candidate["abstract"] = _content_text(note, "abstract")
                except Exception as exc:
                    query_failures.append(f"candidate {forum_id}: {exc}")

    secondary = {forum_id: secondary_score(candidates_by_id[forum_id]) for forum_id in comparison_ids}
    evidenced = [(value, forum_id) for forum_id, value in secondary.items() if value is not None]
    chosen_forum_id = best_forum_id
    if evidenced:
        # Exact-title candidates all satisfy the title threshold. Let author
        # and abstract evidence select the strongest corresponding forum.
        chosen_forum_id = max(evidenced, key=lambda item: item[0])[1]
    chosen_title_score = score(str(candidates_by_id[chosen_forum_id]["title"]))
    evidence_margin = None
    if len(evidenced) > 1:
        secondary_scores = sorted((value for value, _ in evidenced), reverse=True)
        evidence_margin = secondary_scores[0] - secondary_scores[1]

    if diagnostics is not None:
        diagnostics.update(best_title=candidates_by_id[chosen_forum_id]["title"],
                           best_score=chosen_title_score, second_score=second_score,
                           exact_title_match=exact_title_match,
                           author_abstract_score=secondary.get(chosen_forum_id),
                           evidence_margin=evidence_margin)
    # A 100% normalized title is always above the title threshold. If there
    # are duplicates, secondary evidence picks the best; absent useful evidence,
    # the first result for that exact title is still accepted deterministically.
    if not exact_title_match and chosen_title_score < 0.84:
        return None
    if not exact_title_match and chosen_title_score - second_score < 0.04:
        if evidence_margin is None or evidence_margin < 0.05:
            return None
    return replace(fetch_paper(chosen_forum_id), match_score=chosen_title_score)
