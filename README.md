# PaperProbe

**A research paper review assistant that extracts key information from papers and generates critical, deeply related discussion questions with LLM-powered tools.**

## Overview

PaperProbe takes a research paper (PDF or library entry), parses it, extracts structured information (claims, methods, datasets, baselines, limitations), retrieves related work and known reviewer critiques, and generates discussion questions similar to what a thoughtful peer reviewer would ask. Questions aren't limited to facts stated directly in the paper; they can raise broader implications, related work, or open problems, as long as they stay critical and closely tied to the paper's actual content.

The project has two purposes:
1. Build a useful tool for critically reading research papers.
2. Explore LLM application engineering across APIs, backends, databases, and orchestration.

The application combines structured prompting, retrieval, and a LangGraph workflow. The existing question generator remains available alongside an agentic workflow that can be started for a prepared paper from the UI.

## Motivation

I took an LLM-paper seminar class, and whenever I asked an LLM to think of discussion questions based on a paper, it often generated superficial or strange questions. In class, people asked insightful questions, and I learned a lot from seeing how they critiqued papers. I want to help readers develop similarly useful questions grounded in a paper's content. The agentic workflow gathers available context, proposes questions, and uses model critiques to decide whether each candidate is ready, needs revision, or should be replaced.

## Goals
### Learning Goals
- Integrate LLM APIs, including structured outputs, tool use, and prompt versioning
- Design and build REST APIs with FastAPI
- Work with both relational (PostgreSQL) and NoSQL databases
- Build agentic workflows and orchestration (LangChain, LangGraph)
- Apply software engineering principles: testing, typing, modular design, version control, CI, containerization, observability

### Project Goals
- Generate discussion questions that are specific, answerable, critical, and closely tied to the paper
- Combine structured prompting, retrieval, and agentic workflows to support critical paper discussion
- Improve question quality through evaluation and iterative development

## Features and roadmap
### Core Pipeline
- PDF ingestion and section-aware parsing
- Structured extraction of claims, methods, datasets, baselines, and limitations
- Discussion question generation with a critic/revision loop
- Retrieval of related papers, reviewer critiques, and paper-conditioned question examples

### Agentic Workflow (current)
The agentic workflow is an option launched from the existing UI for a selected prepared paper, alongside the standard generator. A bounded LangGraph graph gathers context, generates candidates, critiques them, and routes each model verdict to acceptance, revision, or replacement. The workflow can grow incrementally as design adds more useful capabilities; its current design and extension points are described in [Agentic workflow design](#agentic-workflow-design).

### Agent example library
The UI has a separate example-library flow for uploading a source paper PDF and
one or more associated questions, with an optional rationale for each question.
Each question is stored as a paper-conditioned row in `example_pairs`; the paper
context is embedded once and shared by that paper's question rows, which also
share a `source_paper_id`. This flow does not create or modify a normal application
paper record.

### Models and evaluation
- Evaluation with embedding similarity, LLM-as-judge rubrics, and offline human ratings, used to guide iteration and catch regressions

### Platform
- FastAPI backend with async jobs and streaming responses
- PostgreSQL (with vector search) plus a NoSQL store for raw and semi-structured data
- Start the agentic workflow from the selected paper in the existing web UI and stream its activity
- Tracing and observability for LLM calls and agent runs
  
## Initial Design Notes
Early, high-level thinking on the first two pieces of the pipeline. Details (schemas, retry logic, prompt formats) will be worked out during implementation.

### OpenReview ingestion
- Use the official openreview-py client (v2 API for recent venues, v1 for older ones) to pull accepted papers, PDFs, and their associated reviews/discussion threads for selected venues.
- Public data can be read without authentication; a free OpenReview account will be used regardless for clarity and higher rate-limit headroom.
- Ingested data is cached locally (Postgres/Mongo) rather than re-fetched on each run, both to respect API rate limits and to keep the pipeline reproducible offline.
- Raw review text is treated as source data for local processing only; it will not be committed to the repo or redistributed in bulk. Only derived artifacts (extracted fields, generated questions, aggregate stats) are shared publicly.

### Gemini-based extraction
- Gemini is called once per relevant parsed paper section, with a schema containing only that section's requested fields. This turns section text into summaries, claims, methods, datasets, baselines, and limitations.
- Extraction prompts are versioned so quality can be tracked as they're iterated on, rather than silently changing.

## Project Structure

```
app/
  api/routes/       # HTTP endpoints and HTTP-specific error mapping
  services/         # reusable ingestion, parsing, extraction, and generation operations
  core/config.py    # environment-based settings
  schemas/          # validated API request/response models
  main.py           # FastAPI application factory
tests/              # API tests
.env.example        # safe configuration template
```

The API supports health/readiness checks, OpenReview ingestion, direct PDF upload,
paper retrieval, and question generation. Ingestion runs synchronously for now.
Routes delegate pipeline work to `app.services.paper_pipeline`; the current UI and
HTTP behavior still run the same end-to-end sequence.

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
   are left null until a dedicated chunk-embedding job is wired into ingestion.
   This keeps the database ready for chunk retrieval without fabricating vectors;
   example-pair embeddings are stored separately for paper-conditioned retrieval.
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

## Agentic workflow design

This section documents the implemented agent path and the boundaries that keep
it extensible. The standard UI pipeline remains available; the agentic run is a
separate action on a prepared paper.

### Current design

Pipeline operations live in `app.services.paper_pipeline`, independently of the
FastAPI route handlers. The current UI prepares papers through its existing
ingestion/parsing flow. The agent toolset reads that prepared context, extracts
fields only when needed, proposes and critiques questions, then saves the final
question list.

LangChain adapters live in `app.services.agent_tools`; the graph is in
`app.services.agent_graph`. Install the optional dependencies with
`pip install -e ".[agent,gemini,openreview]"`. `create_paper_tools(session_factory)`
combines pure and database-backed tools without exposing a session or database
credentials as model arguments. Each adapter has a descriptive name, a clear
tool description, an explicit Pydantic input schema, and a typed output model;
`PAPER_TOOL_OUTPUT_SCHEMAS` exposes output models by tool name. The LangGraph
invokes these tools from bounded nodes and passes the workflow system prompt to
the generation and critique models.

| Tool/service | Input | Output | Side effects |
| --- | --- | --- | --- |
| `get_stored_paper(db, paper_id)` | Session and prepared paper UUID | `PaperDetail` with latest text, sections, and existing extracted fields | Read-only database lookup. |
| `get_all_reviews_for_paper(db, paper_id)` | Session and prepared paper UUID | All stored reviews tagged with version key, source forum, and paper title | Read-only lookup across every stored version, not only the latest. |
| `retrieve_similar_example_questions(db, paper_context, k)` | Session, target paper abstract/claims, and result count | `SimilarExampleQuestions` paper/question pairs with source and similarity | Embeds target paper context and searches `example_pairs.paper_embedding`; read-only. |
| `extract_paper_fields(title, full_text, abstract="")` | Title, full text, optional abstract fallback | Section-level fields and merged values | Parses sections internally, calls Gemini; no database writes. |
| `propose_questions(...)` | Paper context, all-version reviews, similar-paper examples, and count | `QuestionGeneration` proposals with focus and rationale | Gemini call; no database writes. |
| `critique_question(...)` | Candidate question, paper context, and all-version review context | `QuestionCritique` verdict, rubric scores, strengths/issues, optional revision | Gemini call; no database writes. |
| `save_final_questions(db, paper_id, questions)` | Session, paper UUID, final questions paired with critique results | `QuestionGenerationResponse` | Saves questions with `keep` verdicts as final, linked to the latest paper version; stores focus, rationale, and critique details. |

### Graph flow and model decisions

The UI exposes **Run agentic workflow** after a paper has been prepared. It
starts `POST /api/v1/papers/{paper_id}/agentic-questions/progress` and streams
graph milestones into the existing Activity panel. The run starts with the
selected paper ID, a generated system prompt containing the question goal and
hard limits, and the available tool set.

The graph fixes operation order and safety limits while leaving quality
judgments to Gemini's structured outputs:

1. **Gather context:** `get_stored_paper` loads the latest prepared paper.
   `get_all_reviews_for_paper` adds reviews across every retained version. The
   graph reuses stored extracted fields, calling `extract_paper_fields` only
   when fields are absent. It also retrieves paper-conditioned examples; if
   example retrieval fails, the run can continue without examples.
2. **Generate:** `propose_questions` returns structured candidate questions,
   focus labels, and rationales using the paper, extracted fields, all-version
   reviews, similar-paper examples, and workflow system prompt.
3. **Critique:** `critique_question` scores each candidate and returns a
   model-generated `keep`, `revise`, or `reject` verdict plus an optional
   revision. A conditional graph edge reads this verdict: `keep` accepts the
   candidate; `revise` uses the model's suggested text and sends it back for
   critique; `reject` drops it.
4. **Replace or finalize:** Dropped candidates are replaced through the generate
   node when more questions are needed and budget remains. When the requested
   count is accepted or the budget is exhausted, the graph calls
   `save_final_questions` once for accepted questions. It saves nothing when no
   candidate passes critique.

There are at most two revisions (three critique calls) per candidate and 40
tool calls per run, including the terminal save. The graph reserves the final
call for saving accepted questions. Reaching the requested count ends the run
early. There is no human approval step. The UI displays context gathering,
extraction/example retrieval, proposal rounds, each critique verdict, revisions,
and final persistence in the Activity panel.

Ingestion, upload, and paper parsing remain UI-triggered preparation steps.
They are not available to the graph, which starts only after the UI has a
stored paper. The standard `generate_questions_from_context` and
`generate_and_store_questions` path remains the non-agentic fallback and is not
exposed as an agent tool.

`propose_questions` and `critique_question` are standalone functions in
`app.services.gemini`, with validated Pydantic output models. The proposal
operation returns question/focus/rationale records. The critique operation
assesses specificity, grounding, answerability, and critical value, and returns
a keep/revise/reject decision. The non-agentic application fallback remains
available through the `generate_questions_from_context` and
`generate_and_store_questions` services and the existing question-generation
route; neither is exported as an agent tool, avoiding overlapping generation
choices in the agent tool list.

### Extending the workflow

The LangGraph owns ordering, branching, and bounded retries. Keep tools focused
on one operation, with outputs that give graph nodes the information they need:

- Give each new tool an explicit Pydantic input and output model, a concise
  description that says when it is useful, and a clear statement of database or
  external-service side effects.
- Keep orchestration state separate from tool code. Carry the paper and version
  IDs, source provenance, existing extracted fields, all-version review context,
  retrieved paper/question pairs, candidate questions, critique results,
  per-question revision counts, and the run-wide tool-call count between graph steps.
- Keep context-dependent decisions in the graph. A tool should perform its
  named operation and return structured results rather than silently launching
  the rest of the pipeline.
- Add retrieval, model alternatives, or additional reviewer tools as separate
  capabilities so future workflows can opt into them per paper.
- Version prompts and keep proposal/critique outputs structured so workflow
  changes can be compared with evaluation data.
- Keep stopping limits in graph state and the system prompt: at
  most two revisions (three critique calls) per question and at most 40 tool
  calls per run, including the final save. Reserve a tool call for
  `save_final_questions` once any candidates are ready.

`app.services.agent_policy` defines these limits and builds the prompt with the
run goal, graph contract, revision policy, and terminal-save conditions. The
workflow graph maintains counters and enforces the same limits rather than
relying on the prompt alone.

The `example_pairs` table stores each example question with its source paper's
title and representative abstract/claims, a 768-dimensional paper embedding,
optional rationale, and source type (`own_criticism` or `openreview_review`).
`retrieve_similar_example_questions` embeds the current paper's abstract and
available claims with the configured Gemini embedding model, then ranks stored
examples by cosine similarity between paper embeddings. It returns the source
paper and its paired question together; question text is not used as the search
vector. Populate `paper_embedding` with the same embedding model named in
`embedding_model` for each row. The separate example-library UI flow accepts a
PDF and multiple questions, extracts the source title and abstract (or readable
text when no abstract is found), embeds the source once, and stores one paired
row per question. Retrieval filters to the configured embedding model and
returns an empty list until matching examples are stored.

### Incremental development

The agentic workflow will grow as design work surfaces useful new roles for the
agents. Extend its graph in small steps without treating the current skeleton as
the final design. Potential additions include related-work retrieval, citation
lookup, and other paper-analysis tools. Workflow execution does not require
human approval or collect user edits as training data.

The current UI ingestion convenience operations still run parsing and
extraction as part of the existing prepare-paper flow. If a future agent needs
to handle an under-prepared paper, add a source-only ingestion operation and
compose parsing and extraction explicitly; keep the UI path as it is today.

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

Use the separate **Add a paper and its questions** panel to add examples for
future agent runs. Upload a PDF, choose whether its questions are your own
criticism or from an OpenReview review, then add one or more question/rationale
pairs. This saves the example pairs without adding the source PDF to the regular
paper library. The **Saved papers and questions** list in the same panel lets you
read each stored paper context and its questions, edit question text or rationale,
remove individual questions, and add questions to a paper already in the library.
These normal example-library changes can all be made in the UI; the database CLI
remains available for broader inspection and administration.

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
.\.venv\Scripts\python.exe scripts\db_admin.py examples list
.\.venv\Scripts\python.exe scripts\db_admin.py examples show <source-paper-id>
.\.venv\Scripts\python.exe scripts\db_admin.py rows example_pairs --limit 50
```

`reconcile` uses the stored forum ID when present. Otherwise it searches by the
stored title; provide `--forum-id <forum-id>` when the title search is poor or
ambiguous. It asks for confirmation, then refreshes all versions and reviews.
Existing questions are kept but marked `stale` because the latest paper context
may have changed. Gemini must be configured for refreshed extraction.

The CLI's `status` command counts every table registered in the application
schema, including newly added tables. `rows <table>` inspects records from any
mapped table (embedding values are summarized by dimension); pass `--record-id`
to inspect one row. `delete-row <table> <record-id>` removes one row after an
explicit confirmation and follows database cascade rules.

Use `examples list [--query TEXT] [--source-type own_criticism|openreview_review]`
to find uploaded source papers, `examples show <source-paper-id>` to inspect its
paper context and all associated questions, and
`examples delete <source-paper-id>` to remove that source and its question pairs. The CLI also
supports `delete <paper-id>` and `reset`; destructive operations require typing
an explicit confirmation phrase. Deleting a normal paper cascades through its
version-level records. Reset clears every mapped PaperProbe table, including
example pairs, while preserving PostgreSQL itself and the Alembic schema.

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
Code in this repository is licensed under MIT (see [LICENSE](LICENSE)). OpenReview-derived source data is not redistributed in bulk from this repository; raw review text is retained locally for processing only.
