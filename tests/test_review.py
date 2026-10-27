"""Smoke tests for the review page, run against the real API app in-process."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from coursedraft.api import create_app
from coursedraft.review.app import create_review_app

from .helpers import FaultyDrafter, ingest_course


@pytest.fixture
def clients(make_rt):
    from coursedraft.synthetic import LEASES

    rt = make_rt(drafter=FaultyDrafter({"LO2-concept"}, attempts={1}))
    ingest_course(rt, LEASES)
    api = TestClient(create_app(rt))
    return api, TestClient(create_review_app(api))


def test_empty_state(make_rt):
    rt = make_rt()
    review = TestClient(create_review_app(TestClient(create_app(rt))))
    page = review.get("/")
    assert page.status_code == 200 and "No runs yet" in page.text
    assert "Nothing ingested yet" in review.get("/sources").text
    assert "No model calls recorded" in review.get("/audit").text


def test_runs_page_and_review_page_show_what_the_checks_caught(clients):
    api, review = clients
    start = review.post(
        "/start", data={"course_id": "lease-accounting-update"}, follow_redirects=False
    )
    assert start.status_code == 303
    run_page = review.get(start.headers["location"])
    html = run_page.text
    assert run_page.status_code == 200
    assert "Checks before review" in html and "redrafted, passed" in html
    assert "is not in" in html  # the near-miss citation from pass 1
    assert "Approve and export" in html and "Return with notes" in html
    assert "NASB 116.42 · S3.5" in html  # citation chip links to its chunk
    assert "Presenter notes, instructor only" in html
    assert "Facilitator note, instructor only" in html

    home = review.get("/").text
    assert "lease-accounting-update-r1" in home and "Awaiting approval" in home
    assert "Citation faults caught before review" in home


def test_approving_from_the_page_exports_and_offers_the_files(clients):
    api, review = clients
    run_id = api.post("/runs", json={"course_id": "lease-accounting-update"}).json()["id"]
    r = review.post(
        f"/runs/{run_id}/decision",
        data={"decision": "approved", "reviewer": "Dana Whitfield", "notes": "OK"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "error" not in r.headers["location"]
    html = review.get(f"/runs/{run_id}").text
    assert "Approved and exported" in html and "lease-accounting-update-deck.pptx" in html
    export_id = api.get(f"/runs/{run_id}").json()["exports"][0]["id"]
    f = review.get(f"/exports/{export_id}")
    assert f.status_code == 200 and f.content[:2] == b"PK"


def test_a_missing_reviewer_comes_back_as_a_message(clients):
    api, review = clients
    run_id = api.post("/runs", json={"course_id": "lease-accounting-update"}).json()["id"]
    r = review.post(f"/runs/{run_id}/decision", data={"decision": "approved", "reviewer": ""})
    assert r.status_code == 200 and "a reviewer name is required" in r.text


def test_sources_chunk_and_audit_pages(clients):
    api, review = clients
    run_id = api.post("/runs", json={"course_id": "lease-accounting-update"}).json()["id"]
    assert "restricted" in review.get("/sources").text
    chunk = review.get("/chunks/S3.5").text
    assert "NASB 116.42" in chunk and "lease-accounting-update-standards-update.docx" in chunk
    audit = review.get(f"/audit?run_id={run_id}").text
    assert "local" in audit and "hosted" in audit
    assert review.get("/runs/nope").status_code == 404
