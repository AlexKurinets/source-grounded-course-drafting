# source-grounded-course-drafting

A drafting system for course material that has to cite its sources correctly. It ingests publisher slide decks, standards updates, an instructor's own prior decks and notes, and drafts an editable PowerPoint deck in the provider's slide master, a Word facilitator guide and a case exercise. Every slide carries its audience and the source chunks behind it, every standards citation is checked against a register of the ingested files before a person sees the draft, and nothing is exported until an instructor approves that exact version.

This is a reference implementation of the architecture behind a drafting system I built for a mid-sized continuing-education provider that trains accountants and finance staff. The production build is under NDA, so a few integrations are abstracted behind adapters: the hosted and local models sit behind one drafter interface, with a deterministic offline drafter as the default so the whole pipeline runs without an API key, PDF parsing through Docling is an optional extra, and the provider's slide master and Word template are replaced by plain ones. It runs on synthetic data, and every company, place, person, standard, paragraph number and figure in it is made up.

[![CI](https://github.com/AlexKurinets/source-grounded-course-drafting/actions/workflows/ci.yml/badge.svg)](https://github.com/AlexKurinets/source-grounded-course-drafting/actions/workflows/ci.yml)

## Architecture

![Architecture: sources are parsed into Postgres with pgvector, a LangGraph workflow drafts and validates against that store, pauses at an approval gate for the review page, and exports only approved versions](docs/architecture.png)

The workflow itself is a LangGraph state graph checkpointed in Postgres (`src/coursedraft/workflow.py`):

```mermaid
flowchart LR
    plan --> retrieve --> draft --> validate
    validate -- "failing items, first failure" --> draft
    validate -- "clean, or failed twice and flagged" --> save --> gate["approval gate (interrupt)"]
    gate -- approved --> export
    gate -- returned --> stop([end])
```

A run starts from a course and its learning objectives. The plan step lays out a title slide, a concept slide and an apply slide per objective, and a closing slide. Retrieval runs once per outline item against the current version of every source for the course. Each slide is drafted into a typed schema (`src/coursedraft/models.py`) that holds the participant-facing text, the presenter notes, an audience field, the citations and the IDs of every chunk it used. The facilitator guide is drafted from facilitator-only chunks, and the case exercise from the course's fact record. Validation then runs the deterministic checks in `src/coursedraft/validate.py`. Anything that fails gets one redraft with the failure attached; if it fails again it goes to the reviewer flagged, not patched. The draft version is saved, the graph hits an `interrupt`, and the run waits, for days if needed, until someone approves or returns it from the review page or the API. Approval resumes the graph from its Postgres checkpoint and writes the files.

## The review page

The review page (`src/coursedraft/review/`) is its own service and only talks to the API. For each run it shows what the checks caught before review and which items were redrafted, which source files changed version since the last approved deck of the same course, every slide beside its counterpart in that approved deck, citation chips that open the exact chunk they point at, the presenter notes and facilitator note marked instructor-only, and the approve or return form. The runs page totals what is awaiting approval, what was caught before review and how many model calls stayed on the local route; the source register and the model-call log each have a page.

## Design decisions

**Chunks follow the document, not a token count.** A slide is one chunk and its speaker notes are a second chunk with the facilitator audience; a Word file is chunked at its headings, and a table stays inside its section (`src/coursedraft/ingest.py`). The first production version used fixed 512-token windows, which cut tables and slides in half, so retrieval returned fragments the model then completed by guessing.

**Retrieval ranks the instructor's material first.** Search fuses Postgres full-text rank (`ts_rank_cd`) and pgvector cosine distance with reciprocal rank fusion (k = 60), then weights by source type: the instructor's own decks 1.6, standards 1.4, client-engagement notes 1.1, publisher decks 1.0, with a 1.5 bonus for chunks tagged with the slide's objective (`src/coursedraft/retrieval.py`). Objective tags ignore words that several objectives share or that appear in the course title, because "lease" in a lease course says nothing about which objective a chunk serves. In the demo courses the instructor's slides come out above the publisher's on every outline item but one, which is the point: their sequencing and examples are what participants rated.

**Every slide carries its audience and its sources.** Participant text and presenter notes are separate fields, not a convention in the prompt. The exporter writes participant text into the layout's placeholders and puts the notes, followed by a list of every source chunk with its file, version and location, into the PowerPoint notes pane (`src/coursedraft/export.py`). A participant slide that repeats any eight-word run from a facilitator-only chunk fails validation.

**Citations are checked against the register, not by a model.** In production, early drafts cited standards paragraphs that did not exist about once in every 15 slides, usually a near miss on the paragraph number. A second model acting as a judge missed too many of those. What stuck is deterministic: every citation has to name a chunk that exists, belongs to the current version of its file, and contains the cited paragraph number, and any paragraph number written on a slide has to be cited. A failure names the chunk that does contain the reference when there is one, e.g. `NASB 116.43 is not in S3.5 (lease-accounting-update-standards-update.docx, section 5); it appears in S3.6`. The offline drafter reproduces that near miss on roughly one slide in 15, chosen deterministically, so the check and the redraft path run in the tests and in the demo.

**One redraft, then a flag.** A failing item is redrafted once with the failure text attached (`MAX_ATTEMPTS = 2` in `workflow.py`). A second failure is shown to the reviewer as a flag on that slide, because a third attempt hid problems more often than it fixed them.

**Case exercises come from one fact record.** The participant case and the facilitator note are both generated from a structured record of the company, decision point, timeline and figures. Generating them separately produced facilitator notes that quoted figures the case never gave, so every figure and company name in either part is now checked against the record.

**Approval is a pause in the workflow, and export checks for it anyway.** The approval gate is a LangGraph `interrupt` with a `PostgresSaver` checkpointer, so a run survives restarts and can be resumed by a different process; one test resumes a run from a fresh runtime with a new pool and a newly compiled graph. `export_approved` independently refuses any draft version without an approval record, and every approval stores the reviewer, the time and the source chunk IDs behind the draft.

**Model calls are routed by sensitivity and logged before they are made.** Sources are public (publisher decks, standards), internal (the instructor's decks) or restricted (notes from client engagements). Public and internal material may go to the hosted model under zero-retention API terms; any call that includes a restricted chunk goes to the local model, and the router refuses to start if the local route is pointed at a hosted service (`src/coursedraft/llm/router.py`). Each call is written to `model_calls` with its route, model, sensitivity and the exact chunk IDs sent, before the call runs.

**The provider's own template, six named layouts.** The exporter looks up layouts by name in the slide master and fails if one is missing, and each layout has a word cap that validation enforces, because letting a model choose layouts freely gave cramped slides. Swapping in the real master is a file change in `templates/`, not a code change.

**Every file version is kept.** The source register stores a SHA-256 per file; an unchanged file is a no-op, a changed one becomes the next version and the current one, and the old version's chunks stay so an approved draft always traces to what it was built on. The seed reissues the lease course's standards update with a new paragraph, and the review page shows that change beside the approved deck.

## Running it

With Docker:

```bash
docker compose up -d --wait
```

That starts Postgres with pgvector, seeds three synthetic courses, and serves the API at http://localhost:8090 (OpenAPI docs at `/docs`) and the review page at http://localhost:8091. Host ports can be changed with `POSTGRES_PORT`, `API_PORT` and `REVIEW_PORT`. The seed leaves one deck approved and exported, the same course redrafted after its standards update changed, one draft awaiting review and one returned with notes.

Draft and approve a course from the command line:

```bash
docker compose exec api coursedraft run revenue-recognition-refresher
docker compose exec api coursedraft approve revenue-recognition-refresher-r1 --reviewer "Dana Whitfield"
docker compose exec api coursedraft return revenue-recognition-refresher-r2 --reviewer "Dana Whitfield" --notes "Use the Atlas contract in the LO1 discussion."
```

The same through the API:

```bash
curl -X POST localhost:8090/runs -H 'content-type: application/json' -d '{"course_id": "impairment-essentials"}'
curl -X POST localhost:8090/runs/impairment-essentials-r2/decision -H 'content-type: application/json' \
  -d '{"decision": "approved", "reviewer": "Dana Whitfield", "notes": "Checked the case figures."}'
curl localhost:8090/runs/impairment-essentials-r2   # exports, with download ids
```

Add or update a source file (an unchanged file is reported as such), and run the regression check:

```bash
docker compose exec api coursedraft ingest /data/sources/lease-accounting-update/lease-accounting-update-standards-update.docx --course lease-accounting-update --type standard
docker compose exec api coursedraft eval
```

`coursedraft eval` drafts every course as a hidden eval run and fails if any citation, audience, fact-record or layout failure survives its redraft. In production the same gate ran against 20 past decks, scored against the versions instructors had actually taught, and a prompt or retrieval change shipped only if no failure reached review; here it runs against the three synthetic courses.

Without Docker, with Python 3.12+ and a Postgres that has pgvector:

```bash
pip install -e ".[dev]"
export COURSEDRAFT_DATABASE_URL=postgresql://postgres:postgres@localhost:5452/coursedraft
coursedraft make-templates --out templates   # only if you want fresh plain templates
coursedraft seed --reset
coursedraft api --port 8090 &
coursedraft review --port 8091 --api-url http://localhost:8090
```

To use real models, set `COURSEDRAFT_HOSTED_ADAPTER=anthropic` (with `ANTHROPIC_API_KEY`; the default model is `claude-opus-5-5`) or `openai`, and `COURSEDRAFT_LOCAL_ADAPTER=ollama` with `COURSEDRAFT_OLLAMA_URL` pointing at a local Ollama server. See `.env.example`.

## Tests

```bash
docker compose up -d postgres --wait
docker compose exec postgres psql -U postgres -c "CREATE DATABASE coursedraft_test"
pytest
```

46 tests run against real Postgres with pgvector and a frozen clock: parsing and chunking, source versioning, hybrid retrieval and the source-type ranking, every validator, the workflow from plan to export including a resume from a different process, the redraft and flag paths, sensitivity routing, the exporter's layouts and notes, the API, the review page, the seed state and the eval gate. `scripts/smoke.sh` brings the Compose stack up and runs every command sequence in this README against it, plus every review page. CI runs lint, format, the tests and the smoke script.

## Layout

```
src/coursedraft/
  ingest.py          parse .pptx/.docx (.pdf via Docling), chunk, tag, version, embed
  retrieval.py       hybrid full-text + pgvector search with source-type weights
  workflow.py        LangGraph graph: plan, retrieve, draft, validate, save, approval gate, export
  validate.py        citation, audience, layout and fact-record checks
  export.py          PowerPoint and Word output into the templates, approved versions only
  llm/               offline drafter, hosted adapters (Anthropic, OpenAI-compatible, Ollama), router
  api.py             FastAPI
  review/            review page (separate service, reads and writes through the API)
  synthetic.py       the three synthetic courses; seed.py writes and ingests them
  evaluate.py        the regression gate behind `coursedraft eval`
templates/           plain slide master and Word template (replace with your own)
tests/               pytest suite
scripts/smoke.sh     Compose smoke test used by CI
```

## What is deliberately left out

- Live model calls. The Anthropic and OpenAI-compatible adapters are the shape of the production ones without the provider-specific retry and logging code, and nothing in the test suite calls them.
- Authentication. Production sat behind the provider's single sign-on and restricted the review page by course; here it is open on localhost.
- Editing inside the review page. Instructors approved here and edited the exported deck in PowerPoint; production then re-ingested the edited deck as the next year's own deck.
- Docling in the Docker image. It is an optional extra (`pip install '.[docling]'`) because it pulls in layout models; the synthetic sources are PowerPoint and Word.
- Image and chart handling. Production carried publisher charts across as placeholders naming their source slide.

## License

MIT, Alex Kurinets.
