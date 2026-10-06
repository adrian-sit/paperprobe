# PaperProbe

**A research paper review assistant that extracts key information from papers and generates critical, deeply-related discussion questions, using fine-tuned and API-based LLMs behind a full-stack, agentic pipeline.**

## Overview

PaperProbe takes a research paper (PDF or library entry), parses it, extracts structured information (claims, methods, datasets, baselines, limitations), retrieves related work and known reviewer critiques, and generates discussion questions similar to what a thoughtful peer reviewer would ask. Questions aren't limited to facts stated directly in the paper; they can raise broader implications, related work, or open problems, as long as they stay critical and closely tied to the paper's actual content.

The project has two purposes:
1. Build the best version I can of a tool for critically reading research papers.
2. Serve as a hands-on project for LLM application engineering, covering APIs, backends, databases, fine-tuning, orchestration, and deployment.

The system draws on prompting, retrieval, fine-tuning, and agentic workflows as complementary techniques, combined into a single pipeline aimed at the strongest overall result, rather than treated as separate options to benchmark against each other.

## Motivation

I took an LLM-paper seminar class, and whenever I tried to ask an LLM to think of discussion questions based on a paper, it always generated very superficial or strange questions. In class, however, many people asked genuinely insightful questions, and I learned a lot about how to critique different papers. I wanted to explore whether there are methods for an LLM to learn how to ask better questions, using an example dataset I have for fine-tuning and through agentic workflows. For example, learning from online peer reviews of other papers, or other methods.

## Goals
### Learning Goals
- Integrate LLM APIs, including structured outputs, tool use, and prompt versioning
- Design and build REST APIs with FastAPI
- Work with both relational (PostgreSQL) and NoSQL databases
- Fine-tune open models with LoRA and other PEFT methods
- Build agentic workflows and orchestration (LangChain, LangGraph)
- Serve models efficiently with vLLM, including LoRA-adapter serving
- Apply software engineering principles: testing, typing, modular design, version control, CI, containerization, observability

### Project Goals
- Generate discussion questions that are specific, answerable, critical, and closely tied to the paper
- Combine prompting, retrieval, fine-tuning, and agentic workflows into the strongest single pipeline, using evaluation to guide decisions rather than to produce a formal comparison
- Close the loop: user feedback and edits become future training and evaluation data

## Planned Features
### Core Pipeline
- PDF ingestion and section-aware parsing
- Structured extraction of claims, methods, datasets, baselines, and limitations
- Discussion question generation with a critic/revision loop
- Retrieval of related papers and reviewer critiques

### Agentic Workflow
The core pipeline isn't a single prompt, it's a small sequence of steps where the LLM's output at one step decides what happens next. Planned steps, roughly in build order:

1. Extract → Generate → Critique loop: extract the paper's claims/methods, generate discussion questions from them, then have a second pass judge each question (too vague? already answered in the paper? not actually critical?) and send weak ones back to be rewritten, up to a couple of tries.
2. Look-up step before generating: before writing questions, the pipeline can search for related papers or past reviewer comments on similar work, so questions can reference relevant context instead of only the paper's own text.
3. Human check-in: the person using the app can approve, edit, or reject questions before they're finalized; those edits are saved and become future training data.
4. (Later, optional) Tool using step for example pulling citation details, or other tasks to be added to the workflow

### Models and evaluation
- LoRA / QLoRA fine-tuning on filtered reviewer questions, aimed at improving the deployed pipeline rather than a standalone comparison
- Evaluation with embedding similarity, LLM-as-judge rubrics, and human ratings, used to guide iteration and catch regressions
- Latency/throughput benchmarks for the serving setup

### Platform
- FastAPI backend with async jobs and streaming responses
- PostgreSQL (with vector search) plus a NoSQL store for raw and semi-structured data
- Simple web UI for viewing papers, rating questions, and editing outputs
- Tracing and observability for LLM calls and agent runs
  
## Dataset
### Planned sources:
- OpenReview: papers, reviews, and discussion threads collected through the official API, used to build a question-generation dataset from reviewer comments
- Personal annotations: my collected criticisms and questions on selected papers, as a small, high-quality set
- User feedback (later): ratings and edits collected through the app

### Planned handling:
- Filter reviewer questions for specificity and quality before fine-tuning
- Split train/validation/test by paper, not by review, to avoid leakage
- Raw data will not be committed to the repository unless permitted

## Initial Design Notes
Early, high-level thinking on the first two pieces of the pipeline. Details (schemas, retry logic, prompt formats) will be worked out during implementation.

### OpenReview ingestion
- Use the official openreview-py client (v2 API for recent venues, v1 for older ones) to pull accepted papers, PDFs, and their associated reviews/discussion threads for selected venues.
- Public data can be read without authentication; a free OpenReview account will be used regardless for clarity and higher rate-limit headroom.
- Ingested data is cached locally (Postgres/Mongo) rather than re-fetched on each run, both to respect API rate limits and to keep the pipeline reproducible offline.
- Raw review text is treated as source data for local processing only; it will not be committed to the repo or redistributed in bulk. Only derived artifacts (extracted fields, generated questions, aggregate stats) are shared publicly.

### Gemini-based extraction
- Gemini is called once per relevant parsed paper section, with a schema containing only that section's requested fields. This turns section text into summaries, claims, methods, datasets, baselines, and limitations.
- Model choice is swappable behind a thin interface so extraction and generation can move to the fine-tuned/vLLM-served model once it's ready, without rewriting the pipeline around it.
- Extraction prompts are versioned so quality can be tracked as they're iterated on, rather than silently changing.

## Roadmap (High Level)
1. Foundation: data collection, database schema, FastAPI backend, LLM API extraction
2. Retrieval and orchestration: vector search, first agentic workflow
3. Fine-tuning: dataset construction and LoRA/QLoRA experiments
4. Serving and evaluation: vLLM deployment, benchmarks, iteration based on evaluation results
5. Integration: frontend, feedback loop, observability

## Project Structure

```
app/
  api/routes/       # HTTP endpoints
  core/config.py    # environment-based settings
  schemas/          # validated API request/response models
  main.py           # FastAPI application factory
tests/              # API tests
.env.example        # safe configuration template
```

The API supports health/readiness checks, OpenReview ingestion, direct PDF upload,
paper retrieval, and question generation. Ingestion runs synchronously for now.

## Setup

Requires Python 3.11 or later. Create a virtual environment, install the API,
development, OpenReview, and Gemini dependencies, then create local configuration:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,openreview,gemini]"
copy .env.example .env
```

The `.env` file is git-ignored; `.env.example` documents expected settings without
containing secrets.
Set `GEMINI_API_KEY` in `.env`. Set `POSTGRES_DSN` for PostgreSQL and
`MONGODB_URI`, `MONGODB_DATABASE`, and `MONGODB_RAW_COLLECTION` for MongoDB.
MongoDB is required for any OpenReview ingestion: the app stops that ingestion if
the raw API notes cannot be archived, rather than continue with an incomplete
source record. PDF-only processing can still use the uploaded document when no
OpenReview match is found. Do not store credentials in committed files.

### Local PostgreSQL

Docker Compose starts PostgreSQL with pgvector support. Configure `.env` from
`.env.example`, including the PostgreSQL DSN, MongoDB settings, and Gemini API
key, then run:

```powershell
docker compose up -d postgres mongodb
.\.venv\Scripts\alembic upgrade head
docker compose exec postgres psql -U paperprobe -d paperprobe -c "\dt"
```

Alembic creates the forum/revision tables and enables the PostgreSQL `vector`
extension. Existing pre-versioning rows are migrated into one `legacy` paper
version, including their abstract and any saved full text. New schema changes
should be added as migrations and applied with `alembic upgrade head`.
The section-extraction migration indexes the existing `extracted_fields.section_id`
relationship. Existing stored papers are not automatically re-extracted; submit
their forum IDs again or upload them again to parse sections and populate field provenance.

### Local MongoDB raw archive

MongoDB retains the original OpenReview API documents before they are normalized.
Each retained document has `forum_id`, `version_id`, `version_timestamp`, `note_id`,
`record_type` (`submission_edit`, `submission_note`, or `review`), fetch time,
and a payload hash. The archive key includes forum, version, record type, note,
and content hash, so edits do not overwrite each other and changed note payloads
remain recoverable for the retained current version. Review documents also carry
the version assignment used by the relational layer. Refreshing a forum removes
raw documents tagged to its superseded submission versions.

```powershell
docker compose exec mongodb mongosh paperprobe --eval 'db.openreview_raw_notes.countDocuments()'
```

Use `app.services.openreview_archive.load_archived_openreview_notes(forum_id,
version_id)` to read the preserved source records. MongoDB is the raw source
snapshot; PostgreSQL is the queryable application model.

## Current data pipeline

1. **Receive and identify the paper.** A user can submit a forum ID directly or
   upload a PDF. Upload processing extracts title, likely authors, abstract,
   and complete machine-readable PDF text. When title or abstract layout
   detection fails but readable text exists, the upload is still stored with a
   fallback title and its full text. The OpenReview search keeps the existing
   title/author matching rules: exact normalized title passes; otherwise a title
   match of at least 90% requires at least 30% author similarity. If no forum is
   safely matched, the upload is still stored as a local paper with one version
   keyed by its PDF digest and no OpenReview edit timestamp.
2. **Find related forums and fetch their newest versions.** OpenReview forum IDs are
     not assumed to contain every revision of a paper. PaperProbe searches the
     title index with overlapping title phrases, scores candidate titles and
     authors using the existing rules (exact normalized title, or title similarity
     of at least 90% plus author similarity of at least 30%), and fetches every
     qualifying forum. Each forum's current submission note, newest edit, and
     paginated forum notes are loaded. Only each forum's newest submission edit is retained
     as a revision, identified by its source forum ID, OpenReview edit ID, and
     timestamp. The current submission note is the authoritative content for that
     forum version. Its PDF is downloaded and parsed. For a referenced PDF, the
     downloader tries the PDF field ID and reference routes, OpenReview's
     note-attachment route, and the note-ID PDF route via `openreview-py`
     (`get_pdf(id=pdf_id,
   is_reference=True)`) for revision-specific references. If all fail or the PDF has
   no extractable text, forum-ID ingestion continues with that version's
   available title, abstract, and reviews. Its `paper_text` stays empty and the
   failure is recorded in version metadata. An uploaded PDF is an independent
   text source: its extracted full text is always retained and used for the
   newest matched forum version, even if OpenReview lookup or PDF retrieval
   fails. A lookup failure falls back to storing the upload as a local paper.
   Revision text is not collapsed into one forum-level field.
3. **Archive raw data first.** The latest submission edit and all review-note
   JSON for each matched forum are written to MongoDB with forum and version
   identifiers. Since only the latest version per forum is retained, all reviews
   from that forum are associated with that version. When a forum is refreshed,
   obsolete raw snapshots for its older submission versions are removed.
4. **Normalize into PostgreSQL.** `papers` has one row per paper identity (or a
   local upload without an OpenReview match), even when that identity spans
   multiple forum IDs. `paper_versions` stores one row per revision, with version
   key, timestamp, `is_latest`, title, authors, abstract, and complete `paper_text`.
   `source_forum_id` records which OpenReview forum supplied each revision, and
   the version key combines forum ID and edit ID so IDs from separate forums
   cannot collide. The paper root records all matched forum IDs in metadata.
   Re-ingesting any matched forum refreshes each qualifying forum's latest version
   and reviews, and removes older relational versions for those forums.
5. **Parse and store named sections.** Each version's readable full text is
   first cleaned of repeated three/four-digit line-number gutters when those
   labels form a clear sequence. Then it is split at recognized standalone
   headers such as Abstract, Introduction,
   Related Work, Method, Experiments, Limitations, Discussion, and Conclusion.
   Header matching is heuristic; unrecognized text is retained in a `Full Text`
   section, and OpenReview's abstract is added when the PDF has no Abstract
   heading. The resulting `paper_sections` rows retain the heading, order, and
   section text. They do not currently store source character offsets or page
   ranges.
6. **Extract fields by section.** Only the latest revision is sent to Gemini for
   structured extraction. PaperProbe makes one focused call for each section
   assigned fields, rather than sending the whole paper in one request. Summary
   and claims are sought in Abstract/Introduction; methods in Method; datasets
   and baselines in Experiments; limitations in Limitations, Conclusion, or
   Discussion. If a preferred section is missing, its fields fall back to the
   available section text. Each result is saved in `extracted_fields` with a
   `section_id` foreign key, plus the Gemini model and `section-v1` prompt
   version; API responses resolve the section ID to its heading. Multiple rows
   of the same field type can preserve results from different source sections.
   `reviews` stores review text read back from MongoDB, along with raw content and OpenReview note
   metadata; every review row references the assigned version. Revision authors,
   sections, text, chunks, and review notes are version-scoped as well. Questions also retain
   their version link.
7. **Prepare full text for retrieval.** Every version's `paper_text` is split
   into overlapping chunks in `paper_chunks` (about 3,000 characters per chunk
   with a 300-character overlap, preferring nearby paragraph/sentence boundaries).
   The table has a pgvector `embedding vector(768)` column and an embedding-model
   field. Chunk records are currently populated, while token counts and embeddings
   are left null until an embedding model/job is selected and wired into ingestion.
   This keeps the database ready for semantic retrieval without fabricating vectors.
8. **Generate questions.** The question endpoint uses the latest version's
   abstract and merged extracted fields, which are now derived from relevant
   full-text sections. If forum-ID PDF retrieval fails, extraction falls back to
   the available abstract or other parsed section text. Generated questions
   store both the forum-level paper ID and the exact version ID that supplied
   the context. The versioned body, reviews, and chunks remain available for
   future retrieval-based generation.

Uploaded PDFs and PDFs fetched from OpenReview both populate
`paper_versions.paper_text`. PDF extraction reads embedded text; scanned pages
that need OCR are not currently recognized. MongoDB preserves source-shaped
OpenReview JSON for future extraction, while PostgreSQL stores one latest version
per matched forum with its version-linked text, review records, structured fields,
and retrieval chunks.

## Usage

```powershell
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for the generated interactive API docs. For a
first end-to-end ingestion, after setting `GEMINI_API_KEY`:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/papers/openreview `
  -ContentType "application/json" `
  -Body '{"forum_id":"<public-openreview-forum-id>"}'
```

The response contains the persisted paper ID. Retrieve the latest
revision with `GET /api/v1/papers/{paper_id}` or a particular revision, including
its full text, extracted fields, and reviews, with
`GET /api/v1/papers/{paper_id}/versions/{version_id}`.

Submitting a forum ID fetches its newest version, searches for other forums using
the same title/author rules, and combines the newest version and reviews from each
qualifying forum. Re-ingestion refreshes those versions and re-runs extraction on
the newest one overall.

Open `http://127.0.0.1:8000/` for the browser interface. Enter a forum ID or upload
a text-based PDF (up to 20 MB). Uploads are searched against OpenReview using the
extracted title and likely author names. A match stores the newest version from
each qualifying forum under one paper record; otherwise, the PDF is stored as a
one-version local paper.
After ingestion, use the revision selector to inspect each revision's source forum,
title, authors, parsed sections, extracted fields, and reviews. Review text is expandable
and the selected version key, timestamp, and review count stay visible in the UI.

1. PaperProbe extracts the PDF title, abstract, and likely author names from the
   first-page author block, then checks PostgreSQL for the exact PDF (by SHA-256
   digest). The PDF abstract is retained for local processing but does not rank
   OpenReview matches.
2. It searches OpenReview's title index using the full title and up to eight short,
   overlapping phrases sampled across the title. Results from these searches are
   combined by forum ID. Every forum that passes the matching rules contributes
   only its latest submission version and its reviews, stored together under one
   paper record with each version retaining its source forum ID.
3. Titles are normalized for case, Unicode accents, punctuation, whitespace, and
   line-wrap hyphenation, then scored using character-sequence similarity and
   title-word overlap. A title score of at least 90% is required; an exact
   normalized title scores 100% and always passes the title gate. For a non-exact
   title, author-list similarity must also be at least 30%. This lower author
   threshold allows a partial author extraction to contribute evidence; an exact
   title can still match when author extraction is incomplete or unavailable. The
   uploaded abstract is not used to rank or approve candidates.
4. Each qualifying forum contributes only its newest edit. These per-forum
   versions are ordered by timestamp, and the newest overall becomes the current
   version for section-scoped Gemini extraction. If no candidate qualifies, it
   uses the uploaded PDF's title and abstract.

Both matched and PDF-only papers continue through the same Gemini extraction and
question-generation flow. The UI renders extracted fields as cards and lets you
generate five questions from the stored context. Its live Activity log reports PDF
extraction, OpenReview matching, Gemini extraction, database saves, and question
generation. Forum ingestion reports whether a paper was already stored and then
refreshes its revision/review history. Upload activity shows the extracted title
and authors and reports the best candidate title and score when matching fails.

### Local database administration

Use the local CLI to inspect stored records and repair an uploaded record that
was initially stored without its OpenReview match:

```powershell
.\.venv\Scripts\python.exe scripts\db_admin.py status
.\.venv\Scripts\python.exe scripts\db_admin.py list
.\.venv\Scripts\python.exe scripts\db_admin.py show <paper-id>
.\.venv\Scripts\python.exe scripts\db_admin.py reconcile <paper-id>
```

`reconcile` uses the stored forum ID when present. Otherwise it searches by the
stored title; provide `--forum-id <forum-id>` when the title search is poor or
ambiguous. It asks for confirmation, then refreshes all versions and reviews.
Existing questions are kept but marked `stale` because the latest paper context
may have changed. Gemini must be configured for refreshed extraction.

The CLI also supports `delete <paper-id>` and `reset`. Both require typing an
explicit confirmation phrase. `show` reports revision keys, timestamps, text
sizes, review counts, and chunk counts. Deletes cascade through all version-level
records; reset preserves PostgreSQL itself and the Alembic schema.

Run the initial test suite with `pytest`.

### External API smoke test

The standalone smoke test fetches one public OpenReview forum and prints its
submission, abstract, and first review. With `--run-gemini`, it sends only the
title and abstract to Gemini as a lightweight connectivity/schema check; it does
not exercise the FastAPI pipeline's section parser or section-scoped extraction.
It does not call the FastAPI server, write to PostgreSQL, or persist source text.

Set a public forum ID in `.env` as `OPENREVIEW_FORUM_ID`, install the optional
clients, and run it:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[openreview,gemini]"
.\.venv\Scripts\python.exe scripts\smoke_external_apis.py
.\.venv\Scripts\python.exe scripts\smoke_external_apis.py --run-gemini
```

Alternatively, pass an ID for a one-off fetch:

```powershell
.\.venv\Scripts\python.exe scripts\smoke_external_apis.py --forum-id <public-forum-id>
```
## License
Code in this repository is licensed under MIT see([LICENSE](LICENSE)). This covers the codebase only:

- Model weights: any fine-tuned LoRA adapters are derivatives of their base model and remain subject to that base model's license/acceptable-use terms, not MIT.
- Data: OpenReview-derived data is not redistributed in bulk from this repo; see the Dataset section above.
