from __future__ import annotations

import pytest

from coursedraft.ingest import IngestError, ingest_file, parse_docx, parse_pptx, tag_objectives
from coursedraft.synthetic import LEASES, write_course_files, write_standards


def test_pptx_gives_one_chunk_per_slide_and_notes_as_facilitator_chunks(tmp_path):
    files = dict((t, p) for p, t in write_course_files(LEASES, tmp_path))
    chunks = parse_pptx(files["own_deck"])
    slides = [c for c in chunks if c.kind == "slide"]
    notes = [c for c in chunks if c.kind == "notes"]
    assert len(slides) == len(LEASES["own_deck"]) == len(notes)
    assert slides[1].title == "Discuss: the warehouse fit-out"
    assert slides[1].audience == "participant" and notes[1].audience == "facilitator"
    assert notes[1].locator == "slide 2 notes"
    assert "board approval date" in notes[1].body


def test_publisher_table_slide_is_kept_as_one_chunk(tmp_path):
    files = dict((t, p) for p, t in write_course_files(LEASES, tmp_path))
    table = parse_pptx(files["publisher_deck"])[-1]
    assert table.title == "Disclosure checklist"
    assert "Short-term leases | Expense for the period" in table.body
    assert "Variable payments | Expense not in the liability" in table.body


def test_docx_chunks_follow_headings_and_keep_tables_inside_their_section(tmp_path):
    chunks = parse_docx(write_standards(LEASES, tmp_path))
    assert [c.title for c in chunks][:2] == [
        "NASB 116.18 Lease term at commencement",
        "NASB 116.20 Reassessment of the lease term",
    ]
    table = chunks[-1]
    assert table.title == "Transition summary"
    assert table.body.count("\n") == 2  # header plus two rows, not split across chunks
    assert "NASB 116.42 | Annual periods from 1 January 2026 | Prospective" in table.body


def test_ingest_records_hash_refs_audience_and_sensitivity(rt):
    from .helpers import ingest_course

    ingest_course(rt, LEASES)
    with rt.pool.connection() as conn:
        sources = conn.execute(
            "SELECT path, source_type, sensitivity, version, length(sha256) AS n FROM sources "
            "ORDER BY id"
        ).fetchall()
        assert {s["source_type"]: s["sensitivity"] for s in sources} == {
            "publisher_deck": "public",
            "own_deck": "internal",
            "standard": "public",
            "client_notes": "restricted",
        }
        assert all(s["n"] == 64 and s["version"] == 1 for s in sources)
        refs = conn.execute(
            "SELECT paragraph_refs FROM chunks WHERE title LIKE 'NASB 116.42%'"
        ).fetchone()
        assert refs["paragraph_refs"] == ["NASB 116.42"]
        aud = conn.execute(
            """SELECT DISTINCT c.audience FROM chunks c JOIN sources s ON s.id = c.source_id
               WHERE s.source_type = 'client_notes'"""
        ).fetchall()
        assert [a["audience"] for a in aud] == ["facilitator"]


def test_unchanged_file_is_a_no_op_and_a_changed_file_becomes_a_new_version(leases):
    rt = leases
    root = rt.settings.sources_dir
    path = root / LEASES["id"] / f"{LEASES['id']}-standards-update.docx"
    again = ingest_file(rt.pool, rt.embedder, rt.clock, LEASES["id"], path, "standard")
    assert again["unchanged"] is True

    write_standards(LEASES, root, version=2)
    v2 = ingest_file(rt.pool, rt.embedder, rt.clock, LEASES["id"], path, "standard")
    assert v2["version"] == 2 and v2["chunks"] == len(LEASES["standards"]) + 2
    with rt.pool.connection() as conn:
        rows = conn.execute(
            "SELECT version, current, "
            "(SELECT count(*) FROM chunks c WHERE c.source_id = s.id) AS n "
            "FROM sources s WHERE path = %s ORDER BY version",
            (path.name,),
        ).fetchall()
    assert [(r["version"], r["current"]) for r in rows] == [(1, False), (2, True)]
    assert rows[0]["n"] > 0  # the superseded version's chunks are kept for traceability


def test_objective_tags_ignore_words_every_objective_shares():
    objectives = LEASES["objectives"]
    cpi = "Discuss: the CPI uplift. Rent rises by the change in CPI. When do you remeasure?"
    fit_out = (
        "Discuss: the warehouse fit-out. A lessee installs racking in year 3 of a 5-year "
        "lease with a 5-year renewal option. Does the fit-out change the lease term?"
    )
    assert tag_objectives(cpi, objectives, LEASES["title"]) == ["LO2"]
    assert tag_objectives(fit_out, objectives, LEASES["title"]) == ["LO1"]


def test_unknown_source_type_and_course_are_rejected(leases, tmp_path):
    path = write_standards(LEASES, tmp_path)
    with pytest.raises(IngestError):
        ingest_file(leases.pool, leases.embedder, leases.clock, LEASES["id"], path, "email")
    with pytest.raises(IngestError):
        ingest_file(leases.pool, leases.embedder, leases.clock, "no-such-course", path, "standard")
