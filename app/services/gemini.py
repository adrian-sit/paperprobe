from app.core.config import get_settings
from app.schemas.extraction import AbstractExtraction, QuestionGeneration


def extract_abstract(title: str, abstract: str) -> AbstractExtraction:
    """Generate a schema-constrained extraction using only title and abstract."""
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError('Gemini client is missing. Install with: pip install -e ".[gemini]"') from exc

    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is required for extraction.")

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


def generate_questions(
    title: str, abstract: str, extracted_fields: dict[str, dict], count: int
) -> QuestionGeneration:
    """Generate critical discussion questions from the currently stored paper context."""
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError('Gemini client is missing. Install with: pip install -e ".[gemini]"') from exc

    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is required for question generation.")

    client = genai.Client(api_key=settings.gemini_api_key)
    context = {
        "title": title,
        "abstract": abstract,
        "extracted_fields": extracted_fields,
    }
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=(
            f"Generate exactly {count} distinct, thoughtful peer-review discussion questions. "
            "Each question must be specific, answerable, critical when appropriate, and grounded "
            "only in the supplied paper context. Do not invent study details or cite outside work.\n\n"
            f"Paper context:\n{context}"
        ),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=QuestionGeneration,
            temperature=0.3,
        ),
    )
    if not response.text:
        raise RuntimeError("Gemini returned no text response.")
    result = QuestionGeneration.model_validate_json(response.text)
    if len(result.questions) != count:
        raise RuntimeError(f"Gemini returned {len(result.questions)} questions; expected {count}.")
    return result
