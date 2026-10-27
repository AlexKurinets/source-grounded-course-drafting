from __future__ import annotations

from pathlib import Path

import pytest

from coursedraft import store
from coursedraft.config import Settings
from coursedraft.llm import ConfigError, Router
from coursedraft.runtime import Conflict
from coursedraft.synthetic import LEASES
from coursedraft.text import sha256_bytes

from .helpers import FaultyDrafter, ingest_course

COURSE = LEASES["id"]


def test_a_run_pauses_at_the_approval_gate_with_its_draft_saved(leases):
    run = leases.start_run(COURSE)
    assert run["status"] == "awaiting_approval"
    assert run["version"] == 1 and run["slides"] == 8
    assert run["decision"] is None and run["exports"] == []
    keys = [s["key"] for s in run["draft"]["slides"]]
    assert keys == [
        "title",
        "LO1-concept",
        "LO1-apply",
        "LO2-concept",
        "LO2-apply",
        "LO3-concept",
        "LO3-apply",
        "close",
    ]
    assert run["validation"] == {**run["validation"], "passes": 1, "caught": 0, "flagged": 0}
    assert run["draft"]["case"]["title"].startswith("Quillmoor Freight Services")
    assert [g["objective_id"] for g in run["draft"]["guide"]] == ["LO1", "LO2", "LO3"]


def test_another_process_resumes_the_paused_run_and_exports(make_rt, leases):
    run_id = leases.start_run(COURSE)["id"]
    later = make_rt()  # new pool, new compiled graph: only the Postgres checkpoint connects them
    run = later.decide(run_id, "approved", "Dana Whitfield", "Checked.")
    assert run["status"] == "exported"
    assert {e["kind"] for e in run["exports"]} == {"deck", "guide", "case"}
    for e in run["exports"]:
        assert sha256_bytes(Path(e["path"]).read_bytes()) == e["sha256"]
    approval = store.get_approval(later.pool, run["draft_version_id"])
    assert approval["reviewer"] == "Dana Whitfield"
    assert any(i.startswith("S") for i in approval["source_chunk_ids"])


def test_a_bad_citation_is_redrafted_once_before_review(make_rt):
    rt = make_rt(drafter=FaultyDrafter({"LO2-concept"}, attempts={1}))
    ingest_course(rt, LEASES)
    run = rt.start_run(COURSE)
    v = run["validation"]
    assert (v["passes"], v["caught"], v["citation_caught"], v["redrafted"], v["flagged"]) == (
        2,
        1,
        1,
        1,
        0,
    )
    [issue] = v["history"][0]["issues"]
    assert issue["slide_key"] == "LO2-concept" and "is not in" in issue["detail"]
    slide = next(s for s in run["draft"]["slides"] if s["key"] == "LO2-concept")
    assert slide["attempts"] == 2 and slide["flags"] == []


def test_a_slide_that_fails_twice_reaches_the_reviewer_flagged(make_rt):
    rt = make_rt(drafter=FaultyDrafter({"LO1-apply"}, attempts={1, 2}))
    ingest_course(rt, LEASES)
    run = rt.start_run(COURSE)
    assert run["status"] == "awaiting_approval"
    assert run["validation"]["flagged"] == 1
    slide = next(s for s in run["draft"]["slides"] if s["key"] == "LO1-apply")
    assert slide["attempts"] == 2 and "is not in" in slide["flags"][0]


def test_a_returned_draft_exports_nothing(leases):
    run_id = leases.start_run(COURSE)["id"]
    run = leases.decide(run_id, "returned", "Marcus Lindqvist", "Swap the example.")
    assert run["status"] == "returned" and run["exports"] == []
    assert run["review_notes"] == "Swap the example."


def test_decisions_need_a_paused_run_a_known_decision_and_a_reviewer(leases):
    run_id = leases.start_run(COURSE)["id"]
    with pytest.raises(ValueError):
        leases.decide(run_id, "approved", "  ")
    with pytest.raises(ValueError):
        leases.decide(run_id, "maybe", "Dana")
    leases.decide(run_id, "approved", "Dana")
    with pytest.raises(Conflict):
        leases.decide(run_id, "approved", "Dana")


def test_restricted_material_only_ever_goes_to_the_local_route(leases):
    run_id = leases.start_run(COURSE)["id"]
    calls = store.list_model_calls(leases.pool, run_id)
    with leases.pool.connection() as conn:
        restricted = {
            r["id"]
            for r in conn.execute(
                """SELECT c.id FROM chunks c JOIN sources s ON s.id = c.source_id
               WHERE s.sensitivity = 'restricted'"""
            ).fetchall()
        }
    sent_restricted = [c for c in calls if set(c["chunk_ids"]) & restricted]
    assert sent_restricted, "the demo course should exercise the local route"
    assert {c["route"] for c in sent_restricted} == {"local"}
    assert {c["sensitivity"] for c in sent_restricted} == {"restricted"}
    assert {c["route"] for c in calls if c["sensitivity"] != "restricted"} == {"hosted"}
    assert any(
        c["step"] == "draft case" and "fact_record:" + COURSE in c["chunk_ids"] for c in calls
    )


def test_the_local_route_cannot_be_pointed_at_a_hosted_service(rt):
    with pytest.raises(ConfigError):
        Router.from_settings(Settings(local_adapter="anthropic"), rt.pool, rt.clock)


def test_an_unknown_course_cannot_start_a_run(rt):
    with pytest.raises(store.NotFound):
        rt.start_run("no-such-course")
