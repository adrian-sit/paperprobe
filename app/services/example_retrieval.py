"""Paper-conditioned few-shot example retrieval services."""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import ExamplePair
from app.schemas.examples import SimilarExampleQuestion, SimilarExampleQuestions

EMBEDDING_DIMENSIONS = 768
MAX_EXAMPLE_RESULTS = 20


def _embed_text(text: str, *, api_key: str, model: str, task_type: str) -> list[float]:
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError('Gemini client is missing. Install with: pip install -e ".[gemini]"') from exc

    response = genai.Client(api_key=api_key).models.embed_content(
        model=model,
        contents=text,
        config=types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=EMBEDDING_DIMENSIONS,
        ),
    )
    if not response.embeddings or not response.embeddings[0].values:
        raise RuntimeError("Gemini returned no paper-context embedding.")
    vector = list(response.embeddings[0].values)
    if len(vector) != EMBEDDING_DIMENSIONS:
        raise RuntimeError(
            f"Expected a {EMBEDDING_DIMENSIONS}-dimension embedding, received {len(vector)}."
        )
    return vector


async def _embed_with_config(text: str, *, task_type: str) -> tuple[list[float], str]:
    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is required for example embeddings.")
    vector = await asyncio.to_thread(
        _embed_text,
        text,
        api_key=settings.gemini_api_key,
        model=settings.gemini_embedding_model,
        task_type=task_type,
    )
    return vector, settings.gemini_embedding_model


async def embed_example_paper_document(text: str) -> tuple[list[float], str]:
    """Embed representative source-paper text for storage in the example library."""
    if not text.strip():
        raise ValueError("Source paper text is required to create an example embedding.")
    return await _embed_with_config(text, task_type="RETRIEVAL_DOCUMENT")


async def retrieve_similar_example_questions(
    db: AsyncSession, paper_context: str, k: int = 5
) -> SimilarExampleQuestions:
    """Embed paper abstract/claims and retrieve questions paired with similar papers.

    Input is representative context for the paper being processed, such as its
    abstract and extracted claims. Results are ordered by cosine similarity of
    the source-paper embeddings and retain the paired question and provenance.
    """
    if not paper_context.strip():
        raise ValueError("paper_context must include an abstract, claims, or other representative paper text.")
    if not 1 <= k <= MAX_EXAMPLE_RESULTS:
        raise ValueError(f"k must be between 1 and {MAX_EXAMPLE_RESULTS}.")

    query_embedding, embedding_model = await _embed_with_config(
        paper_context, task_type="RETRIEVAL_QUERY"
    )

    distance = ExamplePair.paper_embedding.cosine_distance(query_embedding)
    rows = (await db.execute(
        select(ExamplePair, distance.label("cosine_distance"))
        .where(ExamplePair.embedding_model == embedding_model)
        .order_by(distance)
        .limit(k)
    )).all()
    return SimilarExampleQuestions(examples=[
        SimilarExampleQuestion(
            id=pair.id,
            source_paper_id=pair.source_paper_id,
            source_title=pair.source_title,
            source_abstract_or_claims=pair.source_abstract_or_claims,
            question_text=pair.question_text,
            rationale=pair.rationale,
            source_type=pair.source_type,
            cosine_similarity=max(-1.0, min(1.0, 1.0 - float(cosine_distance))),
        )
        for pair, cosine_distance in rows
    ])
