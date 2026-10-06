from dataclasses import dataclass
from difflib import SequenceMatcher
from io import BytesIO
import re

from app.services.paper_sections import strip_line_number_artifacts


@dataclass(frozen=True)
class UploadedPaper:
    title: str
    abstract: str
    authors: tuple[str, ...] = ()
    full_text: str = ""


def extract_pdf_text(data: bytes) -> str:
    """Extract all readable page text from a PDF, keeping page order."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF support is missing. Install the project dependencies.") from exc
    reader = PdfReader(BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    full_text = strip_line_number_artifacts("\n\n".join(page.strip() for page in pages if page.strip()))
    if not full_text:
        raise ValueError("Could not extract readable text from this PDF.")
    return full_text


def _clean_title(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.replace("\x00", " ").split()).strip(" \t\r\n-–—:;,.|")


def _looks_like_author_metadata(line: str) -> bool:
    lower = line.casefold()
    if "@" in line or re.search(
        r"\b(university|universität|college|department|dept\.?|institute|laboratory|laboratories|"
        r"company|corporation|school of|faculty of|research center|research centre)\b",
        lower,
    ):
        return True
    if re.match(r"^(authors?|affiliations?)\s*:", lower):
        return True
    comma_parts = [part.strip() for part in line.split(",")]
    return len(comma_parts) >= 3 and all(len(part.split()) <= 4 for part in comma_parts)


def _extract_authors(lines: list[str], title: str) -> tuple[str, ...]:
    """Extract likely author names from the title-to-abstract area of page one."""
    names: list[str] = []
    affiliation_words = re.compile(
        r"\b(university|universität|college|department|dept\.?|institute|laboratory|"
        r"laboratories|company|corporation|school of|faculty of|research center|"
        r"research centre|email|corresponding author|equal contribution)\b", re.I
    )
    markers = re.compile(r"[¹²³⁴⁵⁶⁷⁸⁹⁰*†‡]+")
    wanted_title = re.sub(r"\W+", " ", title).casefold().strip()
    for line in lines:
        line = " ".join(line.split())
        if not line or "@" in line or affiliation_words.search(line):
            continue
        cleaned_line = re.sub(r"\W+", " ", line).casefold().strip()
        if not cleaned_line or cleaned_line == wanted_title:
            continue
        # Author blocks commonly separate names with commas, semicolons, or
        # the word 'and'; split each candidate then remove affiliation marks.
        for part in re.split(r"[,;]|\s+and\s+|\s*&\s*", line, flags=re.I):
            candidate = markers.sub("", part)
            candidate = re.sub(r"\[[^]]*\]|\([^)]*@[^)]*\)", " ", candidate)
            candidate = re.sub(r"\s+\d+\s*$", "", candidate).strip(" ,;:.")
            tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’-]*", candidate)
            if not 2 <= len(tokens) <= 5:
                continue
            if any(token.casefold() in {"abstract", "introduction", "keywords", "arxiv"}
                   for token in tokens):
                continue
            # Reject a repeated or truncated title line instead of recording it
            # as a single author name.
            normalized_candidate = " ".join(token.casefold() for token in tokens)
            if normalized_candidate in wanted_title or wanted_title in normalized_candidate:
                continue
            name = " ".join(tokens)
            if name not in names:
                names.append(name)
    return tuple(names[:30])


def _title_from_layout(page) -> str:
    """Choose a prominent, compact text block from the top of the first page."""
    fragments: list[tuple[float, float, float, str]] = []

    def collect(text, cm, tm, _font_dict, font_size):
        if not text or not text.strip() or not font_size:
            return
        x, y = float(cm[4]), float(cm[5])
        for offset, part in enumerate(text.splitlines()):
            part = " ".join(part.split())
            if part:
                fragments.append((y - offset * float(font_size) * 1.2, x,
                                  float(font_size), part))

    extracted_text = page.extract_text(visitor_text=collect) or ""
    if not fragments:
        return ""

    # Group spans sharing a baseline so titles split across font runs become lines.
    rows: list[dict] = []
    for y, x, size, text in sorted(fragments, key=lambda part: (-part[0], part[1])):
        row = next((item for item in rows if abs(item["y"] - y) <= max(2.5, size * 0.2)), None)
        if row is None:
            row = {"y": y, "spans": []}
            rows.append(row)
        row["spans"].append((x, size, text))
    for row in rows:
        row["spans"].sort(key=lambda span: span[0])
        row["text"] = " ".join(span[2] for span in row["spans"])
        row["size"] = sum(span[1] for span in row["spans"]) / len(row["spans"])

    abstract_y = next((row["y"] for row in rows
                       if re.fullmatch(r"\s*abstract\s*:?\s*", row["text"], re.IGNORECASE)), None)
    page_height = float(page.mediabox.height)
    upper_rows = [row for row in rows
                  if row["y"] / page_height >= 0.42
                  and (abstract_y is None or row["y"] > abstract_y)]
    if not upper_rows:
        return ""
    largest_size = max(row["size"] for row in upper_rows)
    prominent = [row for row in upper_rows if row["size"] >= largest_size * 0.78]

    groups: list[list[dict]] = []
    for row in prominent:
        if groups and groups[-1][-1]["y"] - row["y"] <= max(
            groups[-1][-1]["size"], row["size"]
        ) * 1.9:
            groups[-1].append(row)
        else:
            groups.append([row])

    def group_score(group: list[dict]) -> float:
        content = " ".join(row["text"] for row in group)
        words = len(content.split())
        if words < 3 or words > 35 or _looks_like_author_metadata(content):
            return 0.0
        mean_size = sum(row["size"] for row in group) / len(group)
        return mean_size * (words ** 0.5)

    if not groups:
        return ""
    best = max(groups, key=group_score)
    if group_score(best) <= 0:
        return ""
    return _clean_title(" ".join(row["text"] for row in best))


def extract_title_and_abstract(data: bytes) -> UploadedPaper:
    """Extract a likely title and abstract from a text-based PDF."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF support is missing. Install the project dependencies.") from exc
    reader = PdfReader(BytesIO(data))
    page_texts = [page.extract_text() or "" for page in reader.pages]
    text = strip_line_number_artifacts("\n".join(page_texts[:8]))
    full_text = strip_line_number_artifacts(
        "\n\n".join(page.strip() for page in page_texts if page.strip())
    )
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    abstract_index = next(
        (i for i, line in enumerate(lines) if line.casefold().strip(" :") == "abstract"), None
    )
    layout_title = _title_from_layout(reader.pages[0]) if reader.pages else ""
    metadata_title = _clean_title(getattr(reader.metadata, "title", None))
    if layout_title and metadata_title and SequenceMatcher(
        None, layout_title.casefold(), metadata_title.casefold()
    ).ratio() >= 0.75:
        title = metadata_title
    else:
        title = layout_title or metadata_title
    if not title:
        # Last-resort fallback: skip common author and affiliation lines rather
        # than treating the first long pre-abstract line as the title.
        title_lines = lines[:abstract_index] if abstract_index is not None else lines[:8]
        title = next((line for line in title_lines
                      if len(line) > 12 and not _looks_like_author_metadata(line)), "")
    if abstract_index is not None:
        end = next(
            (i for i in range(abstract_index + 1, len(lines))
             if lines[i].casefold().strip(" :") in {"1 introduction", "introduction", "1. introduction"}),
            min(abstract_index + 16, len(lines)),
        )
        abstract = " ".join(lines[abstract_index + 1:end]).strip()
    else:
        abstract = ""
    author_boundary = abstract_index if abstract_index is not None else min(len(lines), 18)
    author_lines = lines[max(0, author_boundary - 14):author_boundary]
    authors = _extract_authors(author_lines, title)
    if not full_text:
        raise ValueError("Could not extract readable text from this PDF.")
    # Keep a readable upload ingestible even when the layout parser cannot
    # locate a title block or Abstract heading. The full text remains useful
    # for storage and local downstream processing.
    title = title or "Untitled paper"
    return UploadedPaper(title=title, abstract=abstract, authors=authors, full_text=full_text)
