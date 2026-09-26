"""Exercise OpenReview and Gemini independently from the PaperProbe web API.

The script makes external requests. It never writes to PostgreSQL or saves the
paper/review text; it prints a small OpenReview sample and Gemini's JSON result.
"""

import argparse
import json
import sys
from typing import Any

from app.core.config import get_settings
from app.schemas.extraction import AbstractExtraction


def note_content(note: Any, field: str, default: str = "") -> str:
    """Read a v2 note field while tolerating sparse or non-string content."""
    value = note.content.get(field, default)
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def is_review(note: Any) -> bool:
    invitations = getattr(note, "invitations", []) or []
    return any("review" in invitation.lower() for invitation in invitations)


def fetch_openreview_forum(forum_id: str) -> tuple[str, str, list[dict[str, Any]]]:
    """Fetch a public forum's submission and up to one public review."""
    try:
        import openreview
    except ImportError as exc:
        raise RuntimeError(
            "OpenReview client is missing. Install it with: "
            'pip install -e ".[openreview]"'
        ) from exc

    settings = get_settings()
    client_options: dict[str, str] = {"baseurl": "https://api2.openreview.net"}
    if settings.openreview_username and settings.openreview_password:
        client_options.update(
            username=settings.openreview_username,
            password=settings.openreview_password,
        )
    client = openreview.api.OpenReviewClient(**client_options)
    submission = client.get_note(forum_id)
    replies = client.get_all_notes(forum=forum_id)
    reviews = [
        {
            "id": reply.id,
            "invitations": reply.invitations,
            "content": reply.content,
        }
        for reply in replies
        if is_review(reply)
    ]
    return (
        note_content(submission, "title", "Untitled submission"),
        note_content(submission, "abstract"),
        reviews[:1],
    )


def extract_abstract(title: str, abstract: str) -> AbstractExtraction:
    """Send only title and abstract to Gemini, requesting schema-constrained JSON."""
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError(
            "Gemini client is missing. Install it with: pip install -e \".[gemini]\""
        ) from exc

    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is required to run the Gemini smoke test.")

    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=(
            "Extract information only if it is supported by this research-paper title "
            "and abstract. Use empty lists for details not stated.\n\n"
            f"Title: {title}\n\nAbstract:\n{abstract}"
        ),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=AbstractExtraction,
            temperature=0,
        ),
    )
    if not response.text:
        raise RuntimeError("Gemini returned no text response.")
    return AbstractExtraction.model_validate_json(response.text)


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--forum-id",
        default=settings.openreview_forum_id,
        help="Public OpenReview forum ID. Defaults to OPENREVIEW_FORUM_ID.",
    )
    parser.add_argument(
        "--run-gemini",
        action="store_true",
        help="Also submit the fetched title and abstract to Gemini (an external model call).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.forum_id:
        print("Set OPENREVIEW_FORUM_ID or pass --forum-id <public-forum-id>.", file=sys.stderr)
        return 2

    title, abstract, reviews = fetch_openreview_forum(args.forum_id)
    print("OPENREVIEW RESULT")
    print(json.dumps({"title": title, "abstract": abstract, "reviews": reviews}, indent=2))

    if args.run_gemini:
        print("\nGEMINI STRUCTURED EXTRACTION")
        extraction = extract_abstract(title, abstract)
        print(extraction.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
