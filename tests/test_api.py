from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from coursedraft.api import create_app
from coursedraft.ingest import ingest_file
from coursedraft.synthetic import LEASES, write_standards

COURSE = LEASES["id"]


@pytest.fixture
def api(leases) -> TestClient:
    return TestClient(create_app(leases))


def test_start_review_approve_and_download(api):
    r = api.post("/runs", json={"course_id": COURSE})
    assert r.status_code == 201
    run = r.json()
    assert run["status"] == "awaiting_approval"
    detail = api.get(f"/runs/{run['id']}").json()
    assert len(detail["draft"]["slides"]) == 8 and detail["validation"]["passes"] >= 1
    assert detail["last_approved"] is None and detail["source_changes"] == []

    r = api.post(
        f"/runs/{run['id']}/decision", json={"decision": "approved", "reviewer": "Dana Whitfield"}
    )
    assert r.status_code == 200 and r.json()["status"] == "exported"
    deck = next(e for e in r.json()["exports"] if e["kind"] == "deck")
    d = api.get(f"/exports/{deck['id']}/download")
    assert d.status_code == 200 and d.content[:2] == b"PK"
    assert d.headers["content-type"].endswith("presentationml.presentation")

    assert api.get("/runs", params={"status": "exported"}).json()[0]["id"] == run["id"]
    stats = api.get("/stats").json()
    assert stats["runs_by_status"] == {"exported": 1} and stats["slides_drafted"] == 8


def test_error_statuses(api):
    assert api.post("/runs", json={"course_id": "nope"}).status_code == 404
    assert api.get("/runs/nope").status_code == 404
    assert api.get("/chunks/S999.9").status_code == 404
    assert api.get("/exports/999/download").status_code == 404
    run_id = api.post("/runs", json={"course_id": COURSE}).json()["id"]
    bad = api.post(f"/runs/{run_id}/decision", json={"decision": "maybe", "reviewer": "D"})
    assert bad.status_code == 422
    ok = api.post(f"/runs/{run_id}/decision", json={"decision": "returned", "reviewer": "D"})
    assert ok.status_code == 200
    again = api.post(f"/runs/{run_id}/decision", json={"decision": "approved", "reviewer": "D"})
    assert again.status_code == 409


def test_a_redraft_shows_which_sources_changed_since_the_last_approval(api, leases):
    first = api.post("/runs", json={"course_id": COURSE}).json()["id"]
    api.post(f"/runs/{first}/decision", json={"decision": "approved", "reviewer": "Dana"})
    path = write_standards(LEASES, leases.settings.sources_dir, version=2)
    ingest_file(leases.pool, leases.embedder, leases.clock, COURSE, path, "standard")
    second = api.post("/runs", json={"course_id": COURSE}).json()["id"]
    detail = api.get(f"/runs/{second}").json()
    assert detail["last_approved"]["run_id"] == first
    assert detail["source_changes"] == [
        {"path": "lease-accounting-update-standards-update.docx", "before": 1, "now": 2}
    ]


def test_sources_chunks_and_model_calls(api):
    run_id = api.post("/runs", json={"course_id": COURSE}).json()["id"]
    sources = api.get("/sources").json()
    assert len(sources) == 4 and all(s["chunks"] > 0 for s in sources)
    chunk = api.get("/chunks/S3.5").json()
    assert chunk["paragraph_refs"] == ["NASB 116.42"] and chunk["source_current"] is True
    calls = api.get("/model-calls", params={"run_id": run_id}).json()
    assert {c["route"] for c in calls} == {"hosted", "local"}
