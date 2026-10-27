from __future__ import annotations

from coursedraft import store
from coursedraft.evaluate import run_eval
from coursedraft.seed import seed


def test_seed_leaves_the_demo_state(make_rt):
    rt = make_rt(fault_every=15)
    result = seed(rt)
    assert result == {"files": 12, "runs": 4}
    runs = {r["id"]: r for r in store.list_runs(rt.pool)}
    assert {k: v["status"] for k, v in runs.items()} == {
        "lease-accounting-update-r1": "exported",
        "lease-accounting-update-r2": "awaiting_approval",
        "revenue-recognition-refresher-r1": "awaiting_approval",
        "impairment-essentials-r1": "returned",
    }
    # The simulated near-miss citation is caught and redrafted in both lease runs.
    for run_id in ("lease-accounting-update-r1", "lease-accounting-update-r2"):
        v = runs[run_id]["validation"]
        assert (v["citation_caught"], v["redrafted"], v["flagged"]) == (1, 1, 0)
    stats = store.stats(rt.pool)
    assert stats["citation_faults_caught"] == 2 and stats["slides_flagged"] == 0
    with rt.pool.connection() as conn:
        versions = conn.execute(
            "SELECT path, max(version) AS v FROM sources GROUP BY path ORDER BY path"
        ).fetchall()
    bumped = [r["path"] for r in versions if r["v"] > 1]
    assert bumped == ["lease-accounting-update-standards-update.docx"]


def test_eval_passes_and_its_runs_stay_off_the_review_page(make_rt):
    rt = make_rt(fault_every=15)
    seed(rt, demo_runs=False)
    result = run_eval(rt)
    assert result["passed"] is True
    assert [r["course"] for r in result["courses"]] == [
        "impairment-essentials",
        "lease-accounting-update",
        "revenue-recognition-refresher",
    ]
    assert sum(r["citation"] for r in result["courses"]) == 1
    assert all(r["unresolved"] == 0 for r in result["courses"])
    assert store.list_runs(rt.pool) == []
    assert len(store.list_runs(rt.pool, kind="eval")) == 3
