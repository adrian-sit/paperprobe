from dataclasses import dataclass

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

    @property
    def source_uri(self) -> str:
        return forum_source_uri(self.forum_id)


def forum_source_uri(forum_id: str) -> str:
    """Create the stable storage key before making an OpenReview request."""
    return f"https://openreview.net/forum?id={forum_id}"


def _content_text(note: object, field: str) -> str:
    content = getattr(note, "content", {})
    value = content.get(field, "")
    if isinstance(value, str):
        return value.strip()
    # Some OpenReview configurations wrap a field in an object with a value key.
    if isinstance(value, dict) and isinstance(value.get("value"), str):
        return value["value"].strip()
    return ""


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
