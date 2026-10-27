"""Regression check over the synthetic courses.

Each course is drafted up to the approval gate as an `eval` run (hidden from the review page). The
gate is the production rule: a change ships only if no citation, audience or fact-record failure
survives to the reviewer, so any slide still flagged after its redraft fails the check.
"""

from __future__ import annotations

from . import store
from .runtime import Runtime


def run_eval(rt: Runtime) -> dict:
    rows = []
    for course in store.list_courses(rt.pool):
        run = rt.start_run(course["id"], kind="eval")
        v = run["validation"]
        by_check: dict[str, int] = {}
        for h in v["history"]:
            for i in h["issues"]:
                by_check[i["check"]] = by_check.get(i["check"], 0) + 1
        rows.append(
            {
                "course": course["id"],
                "run": run["id"],
                "slides": run["slides"],
                "caught": v["caught"],
                "citation": by_check.get("citation", 0),
                "audience": by_check.get("audience", 0),
                "fact_record": by_check.get("fact_record", 0),
                "layout": by_check.get("layout", 0),
                "redrafted": v["redrafted"],
                "unresolved": v["flagged"],
            }
        )
    return {"courses": rows, "passed": all(r["unresolved"] == 0 for r in rows)}
