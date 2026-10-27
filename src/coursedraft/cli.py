from __future__ import annotations

import json
from pathlib import Path

import typer
import uvicorn

from .config import Settings

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Source-grounded drafting of course decks, guides and case exercises.",
)


def _runtime(clock=None):
    from .runtime import Runtime

    return Runtime.build(Settings.from_env(), clock)


def _summary(run: dict) -> str:
    v = run.get("validation") or {}
    return (
        f"{run['id']}  {run['status']}  version {run.get('version')}  "
        f"{run.get('slides')} slides  {v.get('caught', 0)} issues caught before review, "
        f"{v.get('redrafted', 0)} redrafted, {v.get('flagged', 0)} flagged"
    )


@app.command("init-db")
def init_db() -> None:
    """Create the tables, the pgvector extension and the LangGraph checkpoint tables."""
    _runtime().close()
    typer.echo("schema ready")


@app.command()
def seed(
    reset: bool = typer.Option(False, help="Drop everything first."),
    if_empty: bool = typer.Option(False, help="Do nothing if courses already exist."),
    demo_runs: bool = typer.Option(True, help="Create the demo runs after ingesting."),
) -> None:
    """Write the synthetic course files, ingest them and create the demo runs."""
    from .db import reset_db
    from .seed import default_seed_clock, is_seeded
    from .seed import seed as do_seed

    settings = Settings.from_env()
    if reset:
        reset_db(settings.database_url)
    rt = _runtime(default_seed_clock())
    try:
        if if_empty and is_seeded(rt):
            typer.echo("already seeded")
            return
        result = do_seed(rt, demo_runs=demo_runs)
        typer.echo(f"ingested {result['files']} files, created {result['runs']} runs")
    finally:
        rt.close()


@app.command()
def ingest(
    path: Path,
    course: str = typer.Option(..., help="Course id."),
    source_type: str = typer.Option(
        ..., "--type", help="own_deck, publisher_deck, standard or client_notes"
    ),
    sensitivity: str = typer.Option(None, help="public, internal or restricted"),
) -> None:
    """Add one file to the source register (a changed file becomes a new version)."""
    from .ingest import ingest_file

    rt = _runtime()
    try:
        r = ingest_file(rt.pool, rt.embedder, rt.clock, course, path, source_type, sensitivity)
        state = "unchanged" if r["unchanged"] else f"{r['chunks']} chunks"
        typer.echo(f"source {r['source_id']} version {r['version']}: {state}")
    finally:
        rt.close()


@app.command()
def run(course: str) -> None:
    """Draft a course up to the approval gate."""
    rt = _runtime()
    try:
        typer.echo(_summary(rt.start_run(course)))
    finally:
        rt.close()


@app.command()
def approve(run_id: str, reviewer: str = typer.Option(...), notes: str = "") -> None:
    """Approve the run's draft. This resumes the paused workflow and exports the files."""
    rt = _runtime()
    try:
        result = rt.decide(run_id, "approved", reviewer, notes)
        typer.echo(_summary(result))
        for e in result.get("exports", []):
            typer.echo(f"  {e['kind']}: {e['path']}")
    finally:
        rt.close()


@app.command("return")
def return_(run_id: str, reviewer: str = typer.Option(...), notes: str = typer.Option(...)) -> None:
    """Return the run's draft with notes. Nothing is exported."""
    rt = _runtime()
    try:
        typer.echo(_summary(rt.decide(run_id, "returned", reviewer, notes)))
    finally:
        rt.close()


@app.command("eval")
def eval_() -> None:
    """Draft every course as a hidden eval run and fail if any check survives to review."""
    from .evaluate import run_eval

    rt = _runtime()
    try:
        result = run_eval(rt)
    finally:
        rt.close()
    head = (
        f"{'course':34} {'slides':>6} {'caught':>6} {'cite':>5} {'aud':>4} {'fact':>5} "
        f"{'redraft':>7} {'unresolved':>10}"
    )
    typer.echo(head)
    for r in result["courses"]:
        typer.echo(
            f"{r['course']:34} {r['slides']:>6} {r['caught']:>6} {r['citation']:>5} "
            f"{r['audience']:>4} {r['fact_record']:>5} {r['redrafted']:>7} "
            f"{r['unresolved']:>10}"
        )
    typer.echo("PASS" if result["passed"] else "FAIL: checks survived to review")
    raise typer.Exit(0 if result["passed"] else 1)


@app.command()
def api(host: str = "127.0.0.1", port: int = 8090) -> None:
    """Serve the HTTP API."""
    from .api import create_app

    uvicorn.run(create_app(_runtime()), host=host, port=port)


@app.command()
def review(
    host: str = "127.0.0.1",
    port: int = 8091,
    api_url: str = typer.Option("http://localhost:8090", envvar="COURSEDRAFT_API_URL"),
) -> None:
    """Serve the review page, which reads from and writes through the API."""
    import httpx

    from .review.app import create_review_app

    client = httpx.Client(base_url=api_url, timeout=60)
    uvicorn.run(create_review_app(client), host=host, port=port)


@app.command("make-templates")
def make_templates(out: Path = Path("templates")) -> None:
    """Write plain starting templates. Replace them with the provider's own slide master and
    Word template; the exporter only needs the six layout names to exist."""
    from datetime import UTC, datetime

    from docx import Document
    from pptx import Presentation

    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    prs, doc = Presentation(), Document()
    for props, title in (
        (prs.core_properties, "Course master"),
        (doc.core_properties, "Facilitator guide"),
    ):
        props.title, props.author, props.last_modified_by = title, "", ""
        props.comments, props.created, props.modified = "", now, now
    prs.save(str(out / "course-master.pptx"))
    doc.save(str(out / "facilitator-guide.docx"))
    typer.echo(json.dumps(sorted(p.name for p in out.iterdir())))


if __name__ == "__main__":
    app()
