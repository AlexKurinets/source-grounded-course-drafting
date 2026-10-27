"""The review page: a separate service that reads and writes only through the API.

An instructor sees what the checks caught before review, the draft beside the last approved
version, every citation linked to the chunk it came from, and the approve or return decision that
resumes the paused workflow.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

HERE = Path(__file__).parent
STATUS_LABELS = {
    "drafting": "Drafting",
    "awaiting_approval": "Awaiting approval",
    "returned": "Returned",
    "exported": "Approved and exported",
    "failed": "Failed",
}
CHECK_LABELS = {
    "citation": "Citation",
    "audience": "Audience",
    "layout": "Layout",
    "fact_record": "Fact record",
}


def _dt(value: str | None, fmt: str = "%d %b %Y, %H:%M") -> str:
    if not value:
        return ""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime(fmt)


def create_review_app(api: httpx.Client) -> FastAPI:
    app = FastAPI(title="Course drafting review", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["dt"] = _dt
    templates.env.globals["STATUS"] = STATUS_LABELS
    templates.env.globals["CHECKS"] = CHECK_LABELS

    def get(path: str, **params):
        r = api.get(path, params=params or None)
        if r.status_code == 404:
            raise HTTPException(status_code=404, detail=r.json().get("detail", "not found"))
        r.raise_for_status()
        return r.json()

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx)

    @app.get("/health")
    def health() -> dict:
        get("/health")
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def runs(request: Request):
        return page(
            request,
            "runs.html",
            runs=get("/runs"),
            stats=get("/stats"),
            courses=get("/courses"),
            nav="runs",
        )

    @app.post("/start")
    def start(course_id: str = Form(...)):
        r = api.post("/runs", json={"course_id": course_id})
        r.raise_for_status()
        return RedirectResponse(f"/runs/{quote(r.json()['id'])}", status_code=303)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run(request: Request, run_id: str, error: str | None = None):
        r = get(f"/runs/{quote(run_id)}")
        prev = r.get("last_approved")
        prev_slides = {s["key"]: s for s in (prev["draft"]["slides"] if prev else [])}
        return page(
            request,
            "run.html",
            run=r,
            v=r.get("validation") or {},
            draft=r.get("draft") or {"slides": [], "guide": []},
            prev=prev,
            prev_slides=prev_slides,
            error=error,
            nav="runs",
        )

    @app.post("/runs/{run_id}/decision")
    def decide(
        run_id: str, decision: str = Form(...), reviewer: str = Form(""), notes: str = Form("")
    ):
        r = api.post(
            f"/runs/{quote(run_id)}/decision",
            json={"decision": decision, "reviewer": reviewer, "notes": notes},
        )
        target = f"/runs/{quote(run_id)}"
        if r.status_code >= 400:
            target += "?error=" + quote(r.json().get("detail", "decision failed"))
        return RedirectResponse(target, status_code=303)

    @app.get("/exports/{export_id}")
    def download(export_id: int):
        r = api.get(f"/exports/{export_id}/download")
        if r.status_code >= 400:
            raise HTTPException(status_code=r.status_code, detail="export not available")
        headers = {"content-disposition": r.headers.get("content-disposition", "attachment")}
        return Response(r.content, media_type=r.headers["content-type"], headers=headers)

    @app.get("/sources", response_class=HTMLResponse)
    def sources(request: Request):
        return page(request, "sources.html", sources=get("/sources"), nav="sources")

    @app.get("/chunks/{chunk_id}", response_class=HTMLResponse)
    def chunk(request: Request, chunk_id: str):
        return page(request, "chunk.html", c=get(f"/chunks/{quote(chunk_id)}"), nav="sources")

    @app.get("/audit", response_class=HTMLResponse)
    def audit(request: Request, run_id: str | None = None):
        calls = get("/model-calls", **({"run_id": run_id} if run_id else {}))
        return page(request, "audit.html", calls=calls, run_id=run_id, nav="audit")

    return app
