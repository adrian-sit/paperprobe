from fastapi import APIRouter, status

from app.schemas.papers import PaperCreate, PaperIngestionAccepted

router = APIRouter()


@router.post("", response_model=PaperIngestionAccepted, status_code=status.HTTP_202_ACCEPTED)
async def ingest_paper(paper: PaperCreate) -> PaperIngestionAccepted:
    """Accept a source now; replace this stub with a persisted async job next."""
    source = "PDF URL" if paper.pdf_url else "OpenReview forum"
    return PaperIngestionAccepted(
        detail=(
            f"{source} accepted for ingestion. "
            "No background worker is connected yet, so extraction has not started."
        )
    )
