from __future__ import annotations

from coursedraft.ingest import ingest_file
from coursedraft.retrieval import or_query, search
from coursedraft.synthetic import LEASES, write_standards


def test_own_deck_ranks_above_the_publisher_on_the_same_topic(leases):
    rows = search(
        leases.pool,
        leases.embedder,
        LEASES["id"],
        "Remeasure the lease liability when index-linked lease payments change",
        k=40,
    )
    types = [r["source_type"] for r in rows]
    assert "publisher_deck" in types
    assert types.index("own_deck") < types.index("publisher_deck")
    assert rows[0]["source_type"] in ("own_deck", "standard")
    assert "publisher_deck" not in types[:8]  # what a slide draft actually sees


def test_objective_bonus_brings_the_objectives_own_discussion_slide_forward(leases):
    rows = search(
        leases.pool,
        leases.embedder,
        LEASES["id"],
        "Remeasure the lease liability when index-linked lease payments change discuss",
        k=8,
        objective_id="LO2",
    )
    titles = [r["title"] for r in rows]
    assert "Discuss: the CPI uplift" in titles


def test_audience_filter(leases):
    rows = search(
        leases.pool,
        leases.embedder,
        LEASES["id"],
        "renewal option fit-out",
        k=20,
        audience="facilitator",
    )
    assert rows and {r["audience"] for r in rows} == {"facilitator"}
    assert any(r["source_type"] == "client_notes" for r in rows)


def test_superseded_versions_are_not_retrieved(leases):
    rt = leases
    path = write_standards(LEASES, rt.settings.sources_dir, version=2)
    v2 = ingest_file(rt.pool, rt.embedder, rt.clock, LEASES["id"], path, "standard")
    rows = search(rt.pool, rt.embedder, LEASES["id"], "lease term reassessment", k=30)
    standard_ids = {r["id"].split(".")[0] for r in rows if r["source_type"] == "standard"}
    assert standard_ids == {f"S{v2['source_id']}"}


def test_or_query_drops_stopwords_and_never_comes_back_empty():
    assert or_query("When do you remeasure the liability?") == "you | remeasure | liability"
    assert or_query("the of and") == "zzznomatch"
