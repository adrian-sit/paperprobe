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

The first API surface is intentionally small: `GET /api/v1/health`,
`GET /api/v1/readiness`, and `POST /api/v1/papers`. The paper endpoint validates
a PDF URL or OpenReview forum ID and returns an accepted job-shaped response. It
does not yet download, persist, or extract a paper; those belong behind a
database-backed background worker in the next increment.

## Setup

Requires Python 3.11 or later. Create a virtual environment, install the API
and development dependencies, then create local configuration:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
```

Add `GEMINI_API_KEY` to `.env` when you begin the Gemini extraction service.
The file is git-ignored; `.env.example` documents every expected setting without
containing any secrets. PostgreSQL, MongoDB, and OpenReview settings are reserved
for their corresponding adapters and are not required to start the skeleton. When
you add Gemini extraction, install its optional client with
`pip install -e ".[dev,gemini]"`.

## Usage

```powershell
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for the generated interactive API docs. For a
quick ingestion-contract check:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/papers `
  -ContentType "application/json" `
  -Body '{"pdf_url":"https://example.org/paper.pdf"}'
```

Run the initial test suite with `pytest`.
## License
Code in this repository is licensed under MIT see([LICENSE](LICENSE)). This covers the codebase only:

- Model weights: any fine-tuned LoRA adapters are derivatives of their base model and remain subject to that base model's license/acceptable-use terms, not MIT.
- Data: OpenReview-derived data is not redistributed in bulk from this repo; see the Dataset section above.
