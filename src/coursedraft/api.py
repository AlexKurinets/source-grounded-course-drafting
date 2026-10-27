from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import store
from .export import NotApproved
from .models import Draft
from .retrieval import get_chunks
from .runtime import Conflict, Runtime

MEDIA = {
    "deck": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "guide": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "case": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class StartRun(BaseModel):
    course_id: str


class Decision(BaseModel):
    decision: str
    reviewer: str
    notes: str = ""


def source_versions(rt: Runtime, draft: dict | None) -> dict[str, int]:
    if not draft:
        return {}
    chunks = get_chunks(rt.pool, Draft.model_validate(draft).chunk_ids())
    out: dict[str, int] = {}
    for c in chunks.values():
        out[c["source_path"]] = max(out.get(c["source_path"], 0), c["source_version"])
    return out


def source_changes(now: dict[str, int], before: dict[str, int]) -> list[dict]:
    changes = []
    for path in sorted(set(now) | set(before)):
        if now.get(path) != before.get(path):
            changes.append({"path": path, "before": before.get(path), "now": now.get(path)})
    return changes


def create_app(rt: Runtime) -> FastAPI:
    app = FastAPI(title="Course drafting API", version="1.0.0")
    app.state.rt = rt

    def not_found(exc: Exception):
        return HTTPException(status_code=404, detail=str(exc))

    @app.get("/health")
    def health() -> dict:
        with rt.pool.connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok"}

    @app.get("/courses")
    def courses() -> list[dict]:
        return store.list_courses(rt.pool)

    @app.get("/runs")
    def runs(status: str | None = None, kind: str = "course") -> list[dict]:
        return store.list_runs(rt.pool, kind=kind, status=status)

    @app.post("/runs", status_code=201)
    def start(body: StartRun) -> dict:
        try:
            return rt.start_run(body.course_id)
        except store.NotFound as exc:
            raise not_found(exc) from exc

    @app.get("/runs/{run_id}")
    def run(run_id: str) -> dict:
        try:
            r = store.get_run(rt.pool, run_id)
        except store.NotFound as exc:
            raise not_found(exc) from exc
        prev = store.last_approved(rt.pool, r["course_id"], run_id)
        r["last_approved"] = prev
        r["source_changes"] = (
            source_changes(source_versions(rt, r.get("draft")), source_versions(rt, prev["draft"]))
            if prev
            else []
        )
        return r

    @app.post("/runs/{run_id}/decision")
    def decide(run_id: str, body: Decision) -> dict:
        try:
            return rt.decide(run_id, body.decision, body.reviewer, body.notes)
        except store.NotFound as exc:
            raise not_found(exc) from exc
        except Conflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, NotApproved) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/exports/{export_id}/download")
    def download(export_id: int):
        try:
            e = store.get_export(rt.pool, export_id)
        except store.NotFound as exc:
            raise not_found(exc) from exc
        path = Path(e["path"])
        if not path.exists():
            raise HTTPException(status_code=410, detail="file no longer on disk")
        return FileResponse(path, media_type=MEDIA[e["kind"]], filename=path.name)

    @app.get("/sources")
    def sources() -> list[dict]:
        return store.list_sources(rt.pool)

    @app.get("/chunks/{chunk_id}")
    def chunk(chunk_id: str) -> dict:
        found = get_chunks(rt.pool, [chunk_id])
        if chunk_id not in found:
            raise HTTPException(status_code=404, detail=f"chunk {chunk_id}")
        return found[chunk_id]

    @app.get("/model-calls")
    def model_calls(run_id: str | None = None, limit: int = 200) -> list[dict]:
        return store.list_model_calls(rt.pool, run_id, limit)

    @app.get("/stats")
    def stats() -> dict:
        return store.stats(rt.pool)

    return app
