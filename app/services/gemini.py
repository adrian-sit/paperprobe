from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings
from app.schemas.extraction import Claim, QuestionGeneration
from app.services.paper_sections import PaperTextSection, section_field_groups


@dataclass(frozen=True)
class SectionExtractedField:
    field_type: str
    value: Any
    section_position: int


def extract_section_fields(title: str, sections: list[PaperTextSection]) -> list[SectionExtractedField]:
    """Make one focused Gemini call for every section assigned extraction fields."""
    try:
        from google import genai
        from google.genai import types
        from pydantic import BaseModel, create_model
    except ImportError as exc:
        raise RuntimeError('Gemini client is missing. Install with: pip install -e ".[gemini]"') from exc

    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is required for extraction.")

    client = genai.Client(api_key=settings.gemini_api_key)
    response_types = {
        "summary": (str, ...),
        "claims": (list[Claim], ...),
        "methods": (list[str], ...),
        "datasets": (list[str], ...),
        "baselines": (list[str], ...),
        "limitations": (list[str], ...),
    }
    instructions = {
        "summary": "a concise summary of the paper's purpose and contribution",
        "claims": "specific claims or contributions stated by the authors",
        "methods": "methods, models, algorithms, or experimental procedures",
        "datasets": "datasets, benchmarks, and evaluation data",
        "baselines": "baseline systems or comparison methods",
        "limitations": "limitations, caveats, threats to validity, or future work",
    }

    results: list[SectionExtractedField] = []
    for section, fields in section_field_groups(sections):
        output_schema = create_model(
            f"{section.heading.replace(' ', '')}Extraction",
            **{name: response_types[name] for name in fields},
        )
        field_instructions = "\n".join(f"- {name}: extract {instructions[name]}." for name in fields)
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=(
                "Extract only information supported by this paper section. Do not fill gaps "
                "from outside knowledge. Return empty lists for list fields and an empty string "
                "for summary when the section does not support the requested information.\n"
                f"Paper title: {title}\n"
                f"Section: {section.heading}\n"
                f"Requested fields:\n{field_instructions}\n\n"
                f"Section text:\n{section.content}"
            ),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=output_schema,
                temperature=0,
            ),
        )
        if not response.text:
            raise RuntimeError(f"Gemini returned no extraction for the {section.heading} section.")
        parsed = output_schema.model_validate_json(response.text)
        for field_type, value in parsed.model_dump().items():
            if isinstance(value, list):
                value = [item.model_dump() if isinstance(item, BaseModel) else item for item in value]
            results.append(SectionExtractedField(field_type, value, section.position))
    return results


def generate_questions(
    title: str, abstract: str, extracted_fields: dict[str, dict], count: int,
    paper_text: str = "",
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
    if paper_text.strip():
        context["paper_text"] = paper_text
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=(
            f"Generate exactly {count} distinct, thoughtful peer-review discussion questions. "
            "Each question must be specific, answerable, critical when appropriate, and grounded "
            "only in the supplied paper context. Use full paper text when present; if absent, "
            "rely on the available title, abstract, and extracted fields. Do not invent study "
            "details or cite outside work.\n\n"
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
