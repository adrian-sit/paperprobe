from app.core.config import get_settings
from app.schemas.extraction import AbstractExtraction


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
