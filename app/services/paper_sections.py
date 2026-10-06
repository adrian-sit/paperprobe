"""Header-based paper section parsing and section-scoped extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PaperTextSection:
    position: int
    heading: str
    category: str
    content: str


_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("abstract", ("abstract", "executive summary")),
    ("introduction", ("introduction", "introduction and motivation", "background and motivation", "motivation")),
    ("related_work", ("related work", "literature review", "prior work")),
    ("background", ("background", "preliminaries", "problem formulation")),
    ("method", ("method", "methods", "methodology", "approach", "proposed method",
                 "model", "materials and methods")),
    ("experiments", ("experiments", "experiment", "experiments and results", "results and experiments",
                      "results and discussion", "experimental setup", "experimental results",
                      "experimental evaluation", "evaluation", "results", "results and analysis",
                      "empirical evaluation", "datasets", "benchmark datasets", "baselines")),
    ("limitations", ("limitations", "limitation", "limitations and future work",
                      "threats to validity", "ethical considerations")),
    ("discussion", ("discussion", "analysis")),
    ("conclusion", ("conclusion", "conclusions", "concluding remarks", "future work")),
    ("acknowledgments", ("acknowledgments", "acknowledgements")),
    ("references", ("references", "bibliography")),
    ("appendix", ("appendix", "appendices", "supplementary material")),
)

_CATEGORY_LABELS = {
    "abstract": "Abstract",
    "introduction": "Introduction",
    "related_work": "Related Work",
    "background": "Background",
    "method": "Method",
    "experiments": "Experiments",
    "limitations": "Limitations",
    "discussion": "Discussion",
    "conclusion": "Conclusion",
    "acknowledgments": "Acknowledgments",
    "references": "References",
    "appendix": "Appendix",
}

_NUMBER_PREFIX = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?\s+)?")
_TRAILING_MARKS = re.compile(r"[\s:：.]+$")


def _match_heading(line: str) -> tuple[str, str] | None:
    candidate = _NUMBER_PREFIX.sub("", line, count=1)
    candidate = _TRAILING_MARKS.sub("", candidate).strip()
    candidate = re.sub(r"\s+\(continued\)$", "", candidate, flags=re.IGNORECASE)
    if not candidate or len(candidate) > 90 or len(candidate.split()) > 10:
        return None
    normalized = re.sub(r"[^a-z0-9]+", " ", candidate.casefold()).strip()
    for category, aliases in _ALIASES:
        if normalized in aliases:
            return _CATEGORY_LABELS[category], category
        # Handle common expanded headings while avoiding broad substring matches.
        if category == "method" and re.fullmatch(r"(?:the )?(?:proposed )?(?:approach|method|methodology)", normalized):
            return _CATEGORY_LABELS[category], category
        if category == "experiments" and re.fullmatch(
            r"(?:experimental )?(?:setup|evaluation|results)(?: and (?:analysis|discussion))?", normalized
        ):
            return _CATEGORY_LABELS[category], category
    return None


def parse_paper_sections(text: str, abstract_fallback: str = "") -> list[PaperTextSection]:
    """Split readable paper text at recognized standalone section headers.

    The parser intentionally favors a modest set of common headings. Unrecognized
    papers remain usable as a single Full Text section, and OpenReview's abstract
    is retained as a section when the PDF text omits an Abstract heading.
    """
    lines = (text or "").replace("\r", "\n").split("\n")
    markers: list[tuple[int, str, str]] = []
    for index, raw_line in enumerate(lines):
        line = " ".join(raw_line.split())
        if not line:
            continue
        matched = _match_heading(line)
        if matched:
            markers.append((index, matched[0], matched[1]))

    if not markers:
        content = (text or "").strip()
        if abstract_fallback.strip():
            sections = [PaperTextSection(0, "Abstract", "abstract", abstract_fallback.strip())]
            if content:
                sections.append(PaperTextSection(1, "Full Text", "full_text", content))
            return sections
        return [PaperTextSection(0, "Full Text", "full_text", content)] if content else []

    result: list[PaperTextSection] = []
    first_header_line = markers[0][0]
    front_matter = "\n".join(lines[:first_header_line]).strip()
    if front_matter:
        result.append(PaperTextSection(len(result), "Front Matter", "front_matter", front_matter))

    for marker_index, (line_index, heading, category) in enumerate(markers):
        next_line = markers[marker_index + 1][0] if marker_index + 1 < len(markers) else len(lines)
        content = "\n".join(lines[line_index + 1:next_line]).strip()
        if category == "abstract" and abstract_fallback.strip():
            content = abstract_fallback.strip()
        if content:
            result.append(PaperTextSection(len(result), heading, category, content))

    if abstract_fallback.strip() and not any(section.category == "abstract" for section in result):
        result.insert(0, PaperTextSection(0, "Abstract", "abstract", abstract_fallback.strip()))

    return [PaperTextSection(index, section.heading, section.category, section.content)
            for index, section in enumerate(result)]


def section_field_groups(sections: list[PaperTextSection]) -> list[tuple[PaperTextSection, tuple[str, ...]]]:
    """Assign requested extraction fields to the sections most likely to state them."""
    if not sections:
        return []

    by_category: dict[str, list[PaperTextSection]] = {}
    for section in sections:
        by_category.setdefault(section.category, []).append(section)

    assignments: dict[int, list[str]] = {section.position: [] for section in sections}

    def assign(field_name: str, preferred: tuple[str, ...]) -> None:
        targets = [section for category in preferred for section in by_category.get(category, [])]
        if not targets:
            usable = [section for section in sections
                      if section.category not in {"front_matter", "references", "acknowledgments"}]
            targets = [next((section for section in usable if section.category == "full_text"),
                            usable[0] if usable else sections[0])]
        for section in targets:
            if field_name not in assignments[section.position]:
                assignments[section.position].append(field_name)

    assign("summary", ("abstract",))
    assign("claims", ("abstract", "introduction", "related_work", "background"))
    assign("methods", ("method",))
    assign("datasets", ("experiments",))
    assign("baselines", ("experiments",))
    assign("limitations", ("limitations", "conclusion", "discussion"))

    return [(section, tuple(assignments[section.position])) for section in sections
            if assignments[section.position]]
