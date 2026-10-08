"""Paper-conditioned few-shot example retrieval services."""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import ExamplePair
from app.schemas.examples import SimilarExampleQuestion, SimilarExampleQuestions

EMBEDDING_DIMENSIONS = 768
MAX_EXAMPLE_RESULTS = 20


def _embed_query(text: str, *, api_key: str, model: str) -> list[float]:
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError('Gemini client is missing. Install with: pip install -e ".[gemini]"') from exc

    response = genai.Client(api_key=api_key).models.embed_content(
        model=model,
        contents=text,
        config=types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY",
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

    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is required for example retrieval.")
    query_embedding = await asyncio.to_thread(
        _embed_query,
        paper_context,
        api_key=settings.gemini_api_key,
        model=settings.gemini_embedding_model,
    )

    distance = ExamplePair.paper_embedding.cosine_distance(query_embedding)
    rows = (await db.execute(
        select(ExamplePair, distance.label("cosine_distance"))
        .where(ExamplePair.embedding_model == settings.gemini_embedding_model)
        .order_by(distance)
        .limit(k)
    )).all()
    return SimilarExampleQuestions(examples=[
        SimilarExampleQuestion(
            id=pair.id,
            source_title=pair.source_title,
            source_abstract_or_claims=pair.source_abstract_or_claims,
            question_text=pair.question_text,
            rationale=pair.rationale,
            source_type=pair.source_type,
            cosine_similarity=max(-1.0, min(1.0, 1.0 - float(cosine_distance))),
        )
        for pair, cosine_distance in rows
    ])
