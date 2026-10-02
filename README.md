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
- Gemini API (free tier) used for the structured extraction step: turning raw paper text into claims, methods, datasets, baselines, and limitations via a schema-constrained prompt (JSON output).
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

Docker Desktop is used only to run PostgreSQL locally; the application schema is
created by Alembic migrations. Ensure `.env` contains the `POSTGRES_*` values in
`.env.example`, then run:

```powershell
docker compose up -d postgres
.\.venv\Scripts\alembic upgrade head
docker compose exec postgres psql -U paperprobe -d paperprobe -c "\dt"
```

This creates `papers`, `paper_sections`, `extracted_fields`, and `questions`.
Future schema changes should be new Alembic revisions, generated after changing
the SQLAlchemy models with:

```powershell
.\.venv\Scripts\alembic revision --autogenerate -m "describe the change"
.\.venv\Scripts\alembic upgrade head
```

### Local MongoDB for raw OpenReview data

MongoDB stores the raw JSON Note objects returned for the selected forum before
the app maps the submission title and abstract or counts its reviews. Start both
local databases from the repository root:

```powershell
docker compose up -d postgres mongodb
docker compose ps
```

The default `.env.example` points the app at `mongodb://localhost:27017`, database
`paperprobe`, collection `openreview_raw_notes`. Change `MONGODB_URI` if MongoDB
runs elsewhere. Documents include the untouched note payload plus archive
metadata (`forum_id`, `note_id`, note role, fetch time, payload hash, and archive
schema version). Identical note payloads are idempotent; when a note changes, the
new hash produces a separate document so older snapshots remain available.
Indexes support reading snapshots by forum and note ID. To inspect the count:

```powershell
docker compose exec mongodb mongosh paperprobe --eval 'db.openreview_raw_notes.countDocuments()'
```

Mongo keeps the complete source Note JSON, including fields the current pipeline
does not interpret. The archive can be read with
`app.services.openreview_archive.load_archived_openreview_notes(forum_id)` for
future extraction or migration jobs. Existing PostgreSQL cache hits do not fetch
OpenReview again; use the database administration `reconcile` command to refresh
a stored paper and create a raw archive snapshot for it.

## Current data pipeline

1. **Receive a source.** The user enters an OpenReview forum ID or uploads a PDF.
   For a PDF, PaperProbe extracts its title, abstract, and likely author names.
2. **Find the OpenReview paper.** The upload path searches candidate titles and
   applies the title and author matching rules below. If no candidate qualifies,
   the uploaded title and abstract remain the source. A direct forum ID skips
   matching. Candidate title and author metadata is used in memory for this
   decision; the selected forum's full notes are fetched and archived next.
3. **Fetch and archive raw OpenReview data.** For a selected forum, PaperProbe
   fetches the submission and paginated forum notes as API JSON. It writes each
   raw Note object to MongoDB before mapping the selected submission into the
   relational/Gemini pipeline.
   Archive failure stops OpenReview ingestion. MongoDB is the source snapshot
   layer; it preserves unknown fields so a later process can extract them
   without relying on OpenReview to still return the same data.
4. **Normalize and enrich.** After the archive succeeds, the app reads the
   submission title and abstract, counts public review notes, and sends the
   abstract to Gemini for structured fields such as claims, methods, datasets,
   baselines, and limitations.
5. **Save relational results.** PostgreSQL stores the canonical paper record,
   abstract section, extracted fields, and generated questions in relational
   tables. Paper metadata records the raw archive count and fetch time.
6. **Generate questions.** The question endpoint loads the stored abstract and
   extracted fields from PostgreSQL, asks Gemini to draft questions, and saves
   them back to PostgreSQL. Future jobs can instead reload a Mongo raw snapshot
   and derive additional fields while keeping the original JSON intact.

The separation is intentional: MongoDB retains the source-shaped data for
reprocessing; PostgreSQL holds the smaller, validated schema used by the current
application and UI.

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

The response contains the persisted paper ID. Retrieve it with
`GET /api/v1/papers/{paper_id}`. The initial flow fetches title and abstract,
uses Gemini to produce structured fields, and saves the abstract plus each
extraction field in PostgreSQL. It is synchronous for now; a background job
will replace this endpoint's long-running work as the pipeline grows.

Submitting the same forum ID again returns the stored record immediately with
`from_cache: true`; it does not call OpenReview or Gemini again. A new paper
returns HTTP 201, while a cached paper returns HTTP 200.

Open `http://127.0.0.1:8000/` for the browser interface. Enter a forum ID or upload
a text-based PDF (up to 20 MB). Uploads are searched against OpenReview using the
extracted title. The lookup flow is:

1. PaperProbe extracts the PDF title, abstract, and likely author names from the
   first-page author block, then checks PostgreSQL for the exact PDF (by SHA-256
   digest). Matching uses only the title and author names; the PDF abstract remains
   available for the PDF-only fallback. A previously matched PDF can be returned
   from cache; a PDF-only record is rechecked against OpenReview.
2. It searches OpenReview's title index using the full title and up to eight short,
   overlapping phrases sampled across the title. Results from these searches are
   combined by forum ID.
3. Titles are normalized for case, Unicode accents, punctuation, whitespace, and
   line-wrap hyphenation, then scored using character-sequence similarity and
   title-word overlap. A title score of at least 90% is required; an exact
   normalized title scores 100% and always passes the title gate. For a non-exact
   title, author-list similarity must also be at least 30%. This lower author
   threshold allows a partial author extraction to contribute evidence; an exact
   title can still match when author extraction is incomplete or unavailable. The
   uploaded abstract is not used to rank or approve candidates.
4. Among qualifying candidates, PaperProbe prefers exact-title candidates with
   author evidence when available, then selects the newest OpenReview true creation
   date (`tcdate`, falling back to `cdate`). It fetches that forum's current title
   and abstract and uses those OpenReview fields for Gemini extraction. If no
   candidate meets the matching rules, it keeps using the uploaded PDF's title and
   abstract. A failed OpenReview request is reported as a lookup problem and also
   falls back to the PDF.

Both matched and PDF-only papers continue through the same Gemini extraction and
question-generation flow. The UI renders extracted fields as cards and lets you
generate five questions from the stored context. Its live Activity log reports PDF
extraction, cache lookup, OpenReview matching, Gemini extraction, database saves,
and question generation. When matching is declined, it includes the number of
   title phrases searched, candidates returned, and the best candidate title and score
when available. The extraction step shows likely author names; a match reports the
author similarity and selected OpenReview version date to help diagnose its choice.

### Local database administration

Use the local CLI to inspect stored records and repair an uploaded record that
was initially stored without its OpenReview match:

```powershell
..venv\Scripts\python.exe scripts\db_admin.py status
..venv\Scripts\python.exe scripts\db_admin.py list
..venv\Scripts\python.exe scripts\db_admin.py show <paper-id>
..venv\Scripts\python.exe scripts\db_admin.py reconcile <paper-id>
```

`reconcile` searches by the stored title; if that title is poor or the search is
ambiguous, provide the known forum ID with `--forum-id <forum-id>`. It previews
the OpenReview title and asks for a paper-specific confirmation before changing
the stored source, abstract, and extracted fields. Existing questions are kept
but marked `stale`, since they were generated from the previous paper context.
Gemini must be configured for the refreshed extraction.

The CLI also supports `delete <paper-id>` and `reset`. Both require typing an
explicit confirmation phrase. Reset removes rows from PaperProbe's four data
tables while preserving PostgreSQL itself and the Alembic schema.

Run the initial test suite with `pytest`.

### External API smoke test

The standalone smoke test fetches one public OpenReview forum and prints its
submission, abstract, and first review. With `--run-gemini`, it then sends only
the title and abstract to Gemini and prints a Pydantic-validated JSON extraction.
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
