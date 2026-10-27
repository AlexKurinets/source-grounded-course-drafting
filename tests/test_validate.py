from __future__ import annotations

from coursedraft.ingest import ingest_file
from coursedraft.models import CaseExercise, Citation, Draft, FactRecord, Slide
from coursedraft.retrieval import get_chunks
from coursedraft.synthetic import LEASES, write_standards
from coursedraft.validate import (
    check_audience,
    check_case,
    check_citations,
    check_layout,
    facilitator_shingles,
    ref_index,
    validate_draft,
)


def chunk_id(rt, ref: str) -> str:
    with rt.pool.connection() as conn:
        return conn.execute(
            """SELECT c.id FROM chunks c JOIN sources s ON s.id = c.source_id
               WHERE %s = ANY(c.paragraph_refs) AND s.current AND c.title LIKE 'NASB%%'""",
            (ref,),
        ).fetchone()["id"]


def slide(**kw) -> Slide:
    base = {"key": "LO2-concept", "layout": "Title and Content", "title": "Remeasurement"}
    return Slide(**{**base, **kw})


def cite_check(rt, s: Slide):
    ids = [c.chunk_id for c in s.citations]
    return check_citations(s, get_chunks(rt.pool, ids), ref_index(rt.pool, LEASES["id"]))


def test_a_correct_citation_passes(leases):
    cid = chunk_id(leases, "NASB 116.42")
    s = slide(
        body=["Remeasure with the revised payments (NASB 116.42)"],
        citations=[Citation(ref="NASB 116.42", chunk_id=cid)],
    )
    assert cite_check(leases, s) == []


def test_a_near_miss_paragraph_is_caught_and_the_right_chunk_named(leases):
    cid = chunk_id(leases, "NASB 116.42")
    right = chunk_id(leases, "NASB 116.43")
    s = slide(
        body=["Remeasure with the revised payments (NASB 116.43)"],
        citations=[Citation(ref="NASB 116.43", chunk_id=cid)],
    )
    [issue] = cite_check(leases, s)
    assert issue.check == "citation"
    assert f"NASB 116.43 is not in {cid}" in issue.detail and right in issue.detail


def test_a_paragraph_no_source_contains_is_caught(leases):
    cid = chunk_id(leases, "NASB 116.42")
    s = slide(
        body=["Remeasure (NASB 116.99)"], citations=[Citation(ref="NASB 116.99", chunk_id=cid)]
    )
    [issue] = cite_check(leases, s)
    assert "no registered source contains it" in issue.detail


def test_a_citation_to_an_unregistered_chunk_is_caught(leases):
    s = slide(
        body=["Remeasure (NASB 116.42)"], citations=[Citation(ref="NASB 116.42", chunk_id="S999.1")]
    )
    [issue] = cite_check(leases, s)
    assert "not in the source register" in issue.detail


def test_a_reference_written_on_the_slide_without_a_citation_is_caught(leases):
    s = slide(body=["See NASB 116.60 for the election"])
    [issue] = cite_check(leases, s)
    assert issue.detail == "NASB 116.60 appears on the slide without a citation"


def test_a_citation_to_a_superseded_version_is_caught(leases):
    rt = leases
    old = chunk_id(rt, "NASB 116.42")
    path = write_standards(LEASES, rt.settings.sources_dir, version=2)
    ingest_file(rt.pool, rt.embedder, rt.clock, LEASES["id"], path, "standard")
    s = slide(
        body=["Remeasure (NASB 116.42)"], citations=[Citation(ref="NASB 116.42", chunk_id=old)]
    )
    [issue] = cite_check(rt, s)
    assert "superseded version" in issue.detail


def test_facilitator_notes_copied_onto_a_participant_slide_are_caught(leases):
    fac = facilitator_shingles(leases.pool, LEASES["id"])
    leak = slide(
        body=[
            "A common mistake is to reassess at the board approval date rather than "
            "when the racking is installed."
        ]
    )
    [issue] = check_audience(leak, fac)
    assert issue.check == "audience"
    assert check_audience(slide(body=["Reassess when the racking is installed."]), fac) == []
    assert check_audience(slide(audience="facilitator"), fac)[0].detail.startswith("facilitator")


def test_layout_word_caps_and_unknown_layouts():
    long = slide(body=["word " * 40, "word " * 40])
    assert "cap 70" in check_layout(long)[0].detail
    assert "not one of the mapped layouts" in check_layout(slide(layout="Blank"))[0].detail
    assert check_layout(slide(body=["Short bullet"])) == []


def record() -> FactRecord:
    return FactRecord.model_validate(LEASES["fact_record"])


def case(**kw) -> Draft:
    base = {
        "title": "Quillmoor",
        "participant": ["Annual rent is $420,000."],
        "decision_point": "Reassess or not.",
        "questions": ["What should Quillmoor Freight Services decide?"],
        "facilitator_note": ["Figures: fit-out cost $1.1 million, rate 6%."],
    }
    return Draft(slides=[], case=CaseExercise(**{**base, **kw}))


def test_case_figures_and_names_must_come_from_the_fact_record():
    assert check_case(case(), record()) == []
    [issue] = check_case(case(facilitator_note=["The fit-out cost $1.4 million."]), record())
    assert issue.check == "fact_record" and "$1.4 million" in issue.detail
    [issue] = check_case(case(participant=["Northgate Logistics Group owns the site."]), record())
    assert "Northgate Logistics Group" in issue.detail
    assert check_case(case(questions=["Question 2: what changes in year 3?"]), record()) == []


def test_validate_draft_runs_every_check(leases):
    cid = chunk_id(leases, "NASB 116.42")
    d = Draft(
        slides=[
            slide(
                key="a",
                body=["Remeasure (NASB 116.43)"],
                citations=[Citation(ref="NASB 116.43", chunk_id=cid)],
            ),
            slide(key="b", layout="Blank"),
        ],
        case=case(facilitator_note=["Rent is $500,000."]).case,
    )
    checks = sorted(
        {(i.slide_key, i.check) for i in validate_draft(leases.pool, LEASES["id"], d, record())}
    )
    assert checks == [("a", "citation"), ("b", "layout"), ("case", "fact_record")]
